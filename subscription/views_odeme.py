"""
Web'den Pro satışı (iyzico) + fatura profili uçları — web 6. not #16-#17.

- `GET/PUT /api/v1/auth/billing-profile/`
- `GET /api/v1/payments/plan/` (herkese açık)
- `POST /api/v1/payments/checkout/` (JWT)
- `POST /api/v1/payments/checkout/complete/` (JWT'SİZ, yalnız `X-IT-Portal-Key`)
- `GET /api/v1/payments/`, `GET payments/<id>/invoice/`, `POST payments/<id>/cancel-request/`

⚠️ Web BFF'dir: bu uçları tarayıcı değil web sunucusu çağırır; kart verisi ne web'e ne
API'ye değer (iyzico'nun kendi sayfası).
"""
import logging

from django.http import FileResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView

from core.response import api_response

from .models import BillingProfile, Payment
from .serializers import (
    BillingProfileSerializer,
    CheckoutCompleteSerializer,
    CheckoutSerializer,
    PaymentListSerializer,
)
from .services import odeme as odeme_svc

logger = logging.getLogger("ihaletakip")

# (pencere sn, sınır) — para akışında sınır HER ZAMAN uygulanır (gözlem modu yok).
ODEME_ORANLARI = {
    "checkout": [(3600, 10)],
    "complete": [(60, 30), (3600, 300)],
    "fatura": [(3600, 60)],
}


class OdemeThrottle(BaseThrottle):
    """`odeme_kapsam` özniteliği olan görünümler: girişliyse kullanıcı, değilse IP
    (portal anahtarı doğruysa `X-IT-Client-IP`) başına. Redis hatasında istek geçer."""

    def __init__(self):
        self._bekle = None

    def allow_request(self, request, view):
        kapsam = getattr(view, "odeme_kapsam", None)
        if not kapsam:
            return True
        try:
            from core.throttling import _say, istemci

            kimlik = istemci(request).kimlik
            simdi = timezone.now().timestamp()
            for pencere, sinir in ODEME_ORANLARI.get(kapsam, []):
                n = _say(f"thr:od:{kapsam}:{kimlik}:{pencere}:{int(simdi // pencere)}",
                         pencere + 5)
                if n > sinir:
                    self._bekle = max(self._bekle or 0, int(pencere - simdi % pencere) + 1)
            return self._bekle is None
        except Exception as e:  # noqa: BLE001 — koruma hatası ödemeyi kırmamalı
            logger.warning("ödeme throttle hatası (istek geçirildi): %s", str(e)[:200])
            return True

    def wait(self):
        return self._bekle


def istemci_ip(request) -> str:
    """iyzico `buyer.ip`: portal anahtarı doğruysa `X-IT-Client-IP`, değilse `X-Real-IP`."""
    from core.throttling import _gecerli_ip, portal_dogrulandi

    ip = None
    if portal_dogrulandi(request):
        ip = _gecerli_ip(request.META.get("HTTP_X_IT_CLIENT_IP"))
    return (ip or _gecerli_ip(request.META.get("HTTP_X_REAL_IP"))
            or _gecerli_ip(request.META.get("REMOTE_ADDR")) or "127.0.0.1")


def _hata(e: odeme_svc.OdemeHatasi):
    resp = api_response(message=e.mesaj, success=False, status=e.status)
    resp.data["errors"] = {"code": e.kod, "detail": e.mesaj}
    return resp


_PROFIL_ORNEK = {
    "type": "kurumsal",
    "full_name": "Ayşe Yılmaz",
    "company_name": "Örnek İnşaat Ltd. Şti.",
    "tax_office": "Kadıköy",
    "tax_number": "1234567890",
    "identity_number": "",
    "address": "Caferağa Mah. Moda Cad. No:1",
    "district": "Kadıköy",
    "city": "İstanbul",
    "phone": "+905321234567",
}


@extend_schema(
    tags=["payments"],
    summary="Fatura profili",
    description=(
        "Kullanıcının fatura profili (web'den Pro satın alma için zorunlu). Yoksa `data: null`.\n\n"
        "`PUT` tam profili yazar (upsert). `type=bireysel` → `full_name` + 11 haneli "
        "`identity_number` (TCKN sağlama algoritmasıyla doğrulanır). `type=kurumsal` → "
        "`company_name` + `tax_office` + 10 haneli `tax_number`; `full_name` yetkili adıdır "
        "ve kurumsal profilde TCKN **tutulmaz**. Telefon `+90…` biçimine normallenir.\n\n"
        "Doğrulama hataları **400** + `errors.<alan>` (liste). ⚠️ KVKK: TCKN/VKN "
        "maskelenmeden yalnızca sahibine döner, loglara yazılmaz."
    ),
    request=BillingProfileSerializer,
    responses={200: BillingProfileSerializer},
    examples=[OpenApiExample("Kurumsal", value=_PROFIL_ORNEK, request_only=True)],
)
class BillingProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profil = BillingProfile.objects.filter(user=request.user).first()
        return api_response(data=BillingProfileSerializer(profil).data if profil else None)

    def put(self, request):
        profil = BillingProfile.objects.filter(user=request.user).first()
        ser = BillingProfileSerializer(profil, data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(user=request.user)
        return api_response(data=ser.data, message="Fatura bilgileri kaydedildi.")


@extend_schema(
    tags=["payments"],
    summary="Web Pro planı",
    auth=[],
    description=(
        "Web'de satılan tek plan: **Pro — 1 yıl**, tek seferlik ödeme (abonelik / otomatik "
        "yenileme yok). `amount` KDV dahil TRY (string). `available=false` → satış kapalı "
        "(`WEB_SATIS_ENABLED`/fiyat/iyzico anahtarı yok); web satın al düğmesini "
        "göstermemeli. Token istemez, web önbelleğe alabilir."
    ),
    request=None,
    responses={200: inline_serializer(name="WebPlan", fields={
        "code": serializers.CharField(), "name": serializers.CharField(),
        "amount": serializers.CharField(allow_null=True),
        "vat_rate": serializers.IntegerField(), "currency": serializers.CharField(),
        "months": serializers.IntegerField(), "available": serializers.BooleanField(),
    })},
    examples=[OpenApiExample("Plan", response_only=True, value={
        "code": "pro_yearly", "name": "Pro — 1 yıl", "amount": "2400.00", "vat_rate": 20,
        "currency": "TRY", "months": 12, "available": True,
    })],
)
class PlanView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return api_response(data=odeme_svc.plan())


@extend_schema(
    tags=["payments"],
    summary="Ödemeyi başlat (iyzico Checkout Form)",
    description=(
        "Bekleyen bir ödeme açar ve iyzico ödeme sayfası adresini döner; web kullanıcıyı "
        "oraya yönlendirir. Tek çekim, taksitsiz, 3D Secure.\n\n"
        "Üç onay da `true` olmalı + `consent_version` (onaylanan metin sürümü) zorunlu → "
        "aksi hâlde **400** `consents_required`. Fatura profili yoksa / eksikse **400** "
        "`billing_profile_required`. Satış kapalıysa **503** `sales_closed`, iyzico "
        "hatasında **502** `payment_provider_error`. `buyer.ip` = `X-IT-Client-IP` "
        "(portal anahtarıyla). Kullanıcı başına saatte 10 istek."
    ),
    request=CheckoutSerializer,
    responses={200: inline_serializer(name="CheckoutStarted", fields={
        "payment_page_url": serializers.URLField()})},
    examples=[OpenApiExample("Onaylar", request_only=True, value={
        "consents": {"on_bilgilendirme": True, "mesafeli_satis": True,
                     "cayma_istisnasi": True},
        "consent_version": "2026-10-28",
    })],
)
class CheckoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [OdemeThrottle]
    odeme_kapsam = "checkout"

    def post(self, request):
        ser = CheckoutSerializer(data=request.data)
        if not ser.is_valid():
            e = odeme_svc.OdemeHatasi(
                "Satın almak için üç onayın da verilmesi gerekir.", "consents_required")
            return _hata(e)
        try:
            url = odeme_svc.checkout_ac(
                request.user,
                consents=ser.validated_data["consents"],
                consent_version=ser.validated_data["consent_version"],
                ip=istemci_ip(request),
            )
        except odeme_svc.OdemeHatasi as e:
            return _hata(e)
        return api_response(data={"payment_page_url": url})


@extend_schema(
    tags=["payments"],
    summary="Ödeme dönüşünü tamamla",
    auth=[],
    description=(
        "iyzico ödeme sonunda tarayıcıyı web'in `callbackUrl`'ine **çapraz site POST** ile "
        "gönderir (oturum çerezi gelmez) → web gelen `token`'ı buraya iletir. **JWT "
        "istemez**; yalnızca doğru `X-IT-Portal-Key` ile kabul edilir (yoksa **403**). "
        "Güven iyzico'ya bizim yaptığımız sorgudadır: token iyzico'dan sorgulanır, "
        "`conversationId`, tutar ve para birimi doğrulanır.\n\n"
        "**İdempotent**: aynı token ikinci kez gelirse aynı sonuç döner, ikinci hak açılmaz. "
        "Pro'yken alınan ikinci yıl mevcut web süresinin **üstüne eklenir**.\n\n"
        "`status`: `success` (+ `pro_until`) · `failure` (`message` kullanıcıya "
        "gösterilebilir) · `pending` (iyzico henüz karar vermedi; mutabakat 15 dk içinde "
        "kapatır). Bilinmeyen/sahte token → `failure` (404 değil)."
    ),
    request=CheckoutCompleteSerializer,
    responses={200: inline_serializer(name="CheckoutResult", fields={
        "status": serializers.ChoiceField(choices=["success", "failure", "pending"]),
        "message": serializers.CharField(),
        "pro_until": serializers.DateTimeField(required=False, allow_null=True),
    })},
    examples=[
        OpenApiExample("Token", request_only=True, value={"token": "a1b2c3d4-…"}),
        OpenApiExample("Başarılı", response_only=True, value={
            "status": "success",
            "message": "Ödemeniz alındı. Pro üyeliğiniz 28.10.2027 tarihine kadar açık.",
            "pro_until": "2027-10-28T12:00:00+03:00"}),
    ],
)
class CheckoutCompleteView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [OdemeThrottle]
    odeme_kapsam = "complete"

    def post(self, request):
        from core.throttling import portal_dogrulandi

        if not portal_dogrulandi(request):
            return api_response(message="Yetkisiz.", success=False, status=403)
        ser = CheckoutCompleteSerializer(data=request.data)
        if not ser.is_valid():
            return api_response(data={"status": "failure",
                                      "message": odeme_svc.MSG_BASARISIZ})
        return api_response(data=odeme_svc.tamamla(ser.validated_data["token"]))


@extend_schema(
    tags=["payments"],
    summary="Web ödemelerim",
    description=(
        "Kullanıcının web ödemeleri (yeniden eskiye). Yarıda bırakılan (`pending`) "
        "denemeler listelenmez. `pro_from`/`pro_until` o ödemenin açtığı dönemdir."
    ),
    request=None,
    responses={200: PaymentListSerializer(many=True)},
)
class PaymentListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = (Payment.objects.filter(user=request.user)
              .exclude(status=Payment.Status.PENDING)
              .select_related("grant").order_by("-created_at"))
        return api_response(data=PaymentListSerializer(qs, many=True).data)


def _sahibinin(request, pk):
    return Payment.objects.filter(user=request.user, pk=pk).select_related("grant").first()


@extend_schema(
    tags=["payments"],
    summary="Fatura PDF'i",
    description=(
        "Ödemenin faturası (`application/pdf`), yalnızca sahibine. Fatura henüz kesilmediyse "
        "**404** `invoice_not_ready`. Web bunu kendi vekilinden indirir (API adresi "
        "HTML'e yazılmaz)."
    ),
    request=None,
    responses={(200, "application/pdf"): bytes},
)
class PaymentInvoiceView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [OdemeThrottle]
    odeme_kapsam = "fatura"

    def get(self, request, pk):
        odeme = _sahibinin(request, pk)
        if odeme is None:
            return api_response(message="Ödeme bulunamadı.", success=False, status=404)
        if not odeme.invoice_pdf:
            return _hata(odeme_svc.OdemeHatasi("Fatura henüz hazır değil.",
                                               "invoice_not_ready", 404))
        ad = f"ihaletakip-fatura-{odeme.invoice_number or odeme.pk}.pdf"
        return FileResponse(odeme.invoice_pdf.open("rb"), as_attachment=True,
                            filename=ad, content_type="application/pdf")


@extend_schema(
    tags=["payments"],
    summary="Fesih talebi",
    description=(
        "**İade yoktur** (dijital hizmet anında ifa edilir; satın almada cayma hakkı "
        "istisnası onaylanır). Talep kaydedilir, Pro **süre sonuna kadar açık kalır** "
        "(otomatik yenileme zaten yok) ve onay e-postası gider. Tekrar çağrı aynı cevabı "
        "döner. Yalnızca başarılı ödemede; aksi **400** `not_cancellable`."
    ),
    request=None,
    responses={200: inline_serializer(name="CancelRequested", fields={
        "cancel_requested_at": serializers.DateTimeField(),
        "pro_until": serializers.DateTimeField(allow_null=True),
        "refund": serializers.BooleanField(),
    })},
)
class PaymentCancelRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        odeme = _sahibinin(request, pk)
        if odeme is None:
            return api_response(message="Ödeme bulunamadı.", success=False, status=404)
        try:
            data = odeme_svc.fesih_talebi(odeme)
        except odeme_svc.OdemeHatasi as e:
            return _hata(e)
        return api_response(
            data=data,
            message="Fesih talebiniz alındı. Pro üyeliğiniz süre sonuna kadar açık kalacak.",
        )
