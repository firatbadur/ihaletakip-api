"""Web → mobil geçiş kodu — /api/v1/auth/handoff/...

İş mantığı `accounts/services/web_handoff.py`'de. İstemci sözleşmesi (web + mobil):
`docs/web-mobil-gecis.md`.
"""
from django.conf import settings
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.response import api_response
from core.throttling import _gecerli_ip, portal_dogrulandi

from .models import WebHandoff
from .serializers import TokenPairSerializer, issue_tokens
from .services import web_handoff as hs


def _istemci_ip(request):
    """Gerçek kullanıcı IP'si: portal doğrulandıysa `X-IT-Client-IP`, değilse `X-Real-IP`."""
    ip = None
    if portal_dogrulandi(request):
        ip = _gecerli_ip(request.META.get("HTTP_X_IT_CLIENT_IP"))
    return ip or _gecerli_ip(request.META.get("HTTP_X_REAL_IP")) or _gecerli_ip(
        request.META.get("REMOTE_ADDR"))


# ── Serializer'lar ──────────────────────────────────────

class HandoffCreateSerializer(serializers.Serializer):
    baglam = serializers.CharField(
        required=False, allow_blank=True, max_length=40,
        help_text="Web'de tıklanan Pro özelliği (`PRO_CONTEXTS` anahtarı, ör. `market`). "
                  "Biçim `[a-z0-9_-]`, değilse boş sayılır.",
    )


class HandoffCreatedSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    kod = serializers.CharField(help_text="QR'a gömülen uzun kod (43 karakter, URL güvenli).")
    kisa_kod = serializers.CharField(
        help_text="Elle yazılan 7 karakterlik kod. Web gösterirken bölebilir (`K7M-4QXA`); "
                  "bozdurma ucu tire/boşluk/küçük harfi kabul eder.")
    baglam = serializers.CharField()
    gecerlilik = serializers.DateTimeField()
    gecerlilik_sn = serializers.IntegerField()


class HandoffStatusSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    durum = serializers.ChoiceField(
        choices=["bekliyor", "okutuldu", "kullanildi", "iptal", "suresi_doldu"])
    baglam = serializers.CharField()
    gecerlilik = serializers.DateTimeField()
    is_premium = serializers.BooleanField(help_text="Kodun sahibi şu an Pro mu?")


class HandoffKodSerializer(serializers.Serializer):
    kod = serializers.CharField(max_length=100)


class HandoffScannedSerializer(serializers.Serializer):
    gecerli = serializers.BooleanField(
        help_text="Kod hâlâ bozdurulabilir mi? `false` → web genel OneLink'e yönlendirir.")
    baglam = serializers.CharField(help_text="Kayıt bulunduysa bağlam (süresi dolmuş olsa da).")


class HandoffRedeemSerializer(serializers.Serializer):
    kod = serializers.CharField(required=False, allow_blank=True, max_length=100,
                                help_text="QR/derin bağlantıdan gelen uzun kod.")
    kisa_kod = serializers.CharField(required=False, allow_blank=True, max_length=20,
                                     help_text="Elle yazılan 7 karakterlik kod.")

    def validate(self, attrs):
        kod = (attrs.get("kod") or "").strip()
        kisa = hs.kisa_normalize(attrs.get("kisa_kod") or "")
        if bool(kod) == bool(kisa):
            raise serializers.ValidationError("`kod` ya da `kisa_kod` alanlarından tam olarak "
                                              "biri gönderilmeli.")
        return {"kod": kod, "kisa_kod": kisa}


class HandoffRedeemedSerializer(TokenPairSerializer):
    baglam = serializers.CharField(help_text="Ödeme ekranında açılacak Pro özelliği.")
    maskeli_eposta = serializers.CharField(
        help_text="`a***@gmail.com` — \"bu hesapla devam ediliyor\" satırı için.")


def _durum_yaniti(kayit: WebHandoff) -> dict:
    return {
        "id": kayit.pk,
        "durum": kayit.etkin_durum,
        "baglam": kayit.baglam,
        "gecerlilik": kayit.gecerlilik,
        "is_premium": kayit.user.is_premium,
    }


def _hata_ornegi(ad, kod, mesaj, durum="400"):
    return OpenApiExample(ad, response_only=True, status_codes=[durum], value={
        "success": False, "message": mesaj, "data": None,
        "errors": {"code": kod, "detail": mesaj}})


# ── Uçlar ───────────────────────────────────────────────

@extend_schema(
    tags=["auth"],
    summary="Web → mobil geçiş kodu oluştur",
    description=(
        "Web'de **girişli** kullanıcı Pro'ya tıklayınca çağrılır. Dönen `kod` QR'a gömülür "
        "(`https://ihaletakip.com/q/<kod>`), `kisa_kod` QR'ın altında gösterilir (uygulama "
        "yüklendikten sonra derin bağlantı eşleşmezse elle yazılır).\n\n"
        "Kod **tek kullanımlık**, `gecerlilik_sn` (15 dk) geçerli ve uygulamada bozdurulunca "
        "**bu hesabın oturumunu açar** — kişiye özel bir giriş anahtarıdır, önbelleğe "
        "alınmamalı ve loglanmamalı. Süre dolunca web yeni kod ister; sayfa kapanınca "
        "`DELETE` ile iptal eder. Kullanıcı başına aynı anda en çok 5 canlı kod tutulur, "
        "fazlası (en eskiler) iptal edilir.\n\n"
        "Hız sınırı: kullanıcı başına 10 dk'da 30, günde 300 (aşılırsa 429)."
    ),
    request=HandoffCreateSerializer,
    responses={201: HandoffCreatedSerializer},
    examples=[
        OpenApiExample("İstek", request_only=True, value={"baglam": "market"}),
        OpenApiExample("Yanıt", response_only=True, status_codes=["201"], value={
            "success": True, "message": "", "data": {
                "id": 812, "kod": "Zr3m…(43 karakter)", "kisa_kod": "K7M4QXA",
                "baglam": "market", "gecerlilik": "2026-10-29T12:15:00+03:00",
                "gecerlilik_sn": 900}}),
    ],
)
class HandoffCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [hs.HandoffThrottle]
    eposta_kapsam = "handoff_olustur"

    def post(self, request):
        s = HandoffCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        kayit, kod, kisa = hs.olustur(request.user, s.validated_data.get("baglam", ""),
                                      ip=_istemci_ip(request))
        return Response({
            "id": kayit.pk,
            "kod": kod,
            "kisa_kod": kisa,
            "baglam": kayit.baglam,
            "gecerlilik": kayit.gecerlilik,
            "gecerlilik_sn": settings.WEB_HANDOFF_SURE_DK * 60,
        }, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=["auth"],
        summary="Geçiş kodu durumu (web yoklaması)",
        description=(
            "Web QR ekranı bunu birkaç saniyede bir yoklar. `durum`: `bekliyor` → "
            "`okutuldu` (telefon QR'ı açtı) → `kullanildi` (uygulamada oturum açıldı); "
            "ayrıca `iptal`, `suresi_doldu`. `is_premium` satın alma tamamlanınca `true` "
            "olur (mobil `subscription/verify/` çağırdığında ya da RevenueCat webhook'u "
            "geldiğinde) → web sayfayı yeniler.\n\nYalnızca kodun sahibi görebilir (aksi 404)."
        ),
        responses={200: HandoffStatusSerializer},
        examples=[OpenApiExample("Yanıt", response_only=True, value={
            "success": True, "message": "", "data": {
                "id": 812, "durum": "kullanildi", "baglam": "market",
                "gecerlilik": "2026-10-29T12:15:00+03:00", "is_premium": False}})],
    ),
    delete=extend_schema(
        tags=["auth"],
        summary="Geçiş kodunu iptal et",
        description="Web sayfası kapanınca/QR yenilenince çağrılır. İdempotent (204); "
                    "kullanılmış kod etkilenmez.",
        responses={204: None},
    ),
)
class HandoffDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _kayit(self, request, pk):
        return get_object_or_404(WebHandoff.objects.select_related("user"),
                                 pk=pk, user=request.user)

    def get(self, request, pk):
        return Response(_durum_yaniti(self._kayit(request, pk)))

    def delete(self, request, pk):
        hs.iptal(self._kayit(request, pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=["auth"],
    summary="Geçiş kodu okutuldu (web /q yönlendirmesi)",
    auth=[],
    description=(
        "**Yalnızca web portalı** (`X-IT-Portal-Key` zorunlu, yoksa 403 `portal_required`). "
        "`https://ihaletakip.com/q/<kod>` açılınca web bunu çağırır, sonra OneLink'e "
        "yönlendirir. Kod `bekliyor` ise `okutuldu` yapılır (web QR ekranı "
        "\"Telefonunuzda açılıyor…\" der).\n\n"
        "Her zaman 200: `gecerli=false` (yok / süresi dolmuş / kullanılmış / iptal) ise web "
        "kodu OneLink'e **eklemez**, genel bağlantıya yönlendirir; `baglam` kayıt bulunduysa "
        "yine döner ki ödeme ekranı doğru özellikle açılsın.\n\n"
        "⚠️ Kimlik `id` ile değil **`kod`** ile: `/q/<kod>` rotası yalnızca kodu bilir."
    ),
    request=HandoffKodSerializer,
    responses={200: HandoffScannedSerializer},
    examples=[
        OpenApiExample("İstek", request_only=True, value={"kod": "Zr3m…"}),
        OpenApiExample("Yanıt", response_only=True, value={
            "success": True, "message": "", "data": {"gecerli": True, "baglam": "market"}}),
    ],
)
class HandoffScanView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        if not portal_dogrulandi(request):
            raise hs.HandoffHatasi("Bu uç yalnızca web portalı içindir.", "portal_required", 403)
        s = HandoffKodSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        kayit = hs.okutuldu_isaretle(s.validated_data["kod"])
        if kayit is None:
            return Response({"gecerli": False, "baglam": ""})
        return Response({
            "gecerli": kayit.etkin_durum in WebHandoff.AKTIF,
            "baglam": kayit.baglam,
        })


@extend_schema(
    tags=["auth"],
    summary="Geçiş kodunu bozdur (mobil — web hesabıyla oturum aç)",
    auth=[],
    description=(
        "Mobil uygulama derin bağlantıdan (`deep_link_value=web_pro`, `deep_link_sub2=<kod>`) "
        "gelen `kod`u ya da kullanıcının yazdığı `kisa_kod`u gönderir. Başarıda `login` ile "
        "aynı yanıt (`access`, `refresh`, `user`, `created:false`) + `baglam` (ödeme ekranı "
        "bağlamı) + `maskeli_eposta` döner. Uygulama ardından `Purchases.logIn(user.id)` "
        "çağırıp ödeme ekranını `baglam` ile açar.\n\n"
        "Kod tek kullanımlıktır. Yanlış / süresi dolmuş / kullanılmış kod → 400 "
        "`handoff_invalid`.\n\n"
        "Hız sınırı IP başına 10 dk'da 20, günde 100 (429, `Retry-After`). Kısa kod için "
        "ayrıca küresel sigorta var: çok sayıda hatalı deneme olursa kısa kod geçici "
        "olarak kapanır → 429 `handoff_busy` (QR yolu çalışmaya devam eder).\n\n"
        "⚠️ Uç kimlik doğrulaması YAPMAZ: istekte eski/geçersiz bir `Authorization` başlığı "
        "olsa da 401 dönmez (aksi hâlde mobil oturumu kapatma akışına düşerdi)."
    ),
    request=HandoffRedeemSerializer,
    responses={200: HandoffRedeemedSerializer},
    examples=[
        OpenApiExample("QR kodu", request_only=True, value={"kod": "Zr3m…"}),
        OpenApiExample("Kısa kod", request_only=True, value={"kisa_kod": "K7M-4QXA"}),
        _hata_ornegi("Geçersiz", "handoff_invalid", hs.MSG_GECERSIZ),
        _hata_ornegi("Kısa kod kapalı", "handoff_busy", hs.MSG_MESGUL, "429"),
    ],
)
class HandoffRedeemView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [hs.HandoffThrottle]
    eposta_kapsam = "handoff_bozdur"

    def post(self, request):
        s = HandoffRedeemSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        kayit = hs.bozdur(**s.validated_data, ip=_istemci_ip(request))
        user = kayit.user
        return api_response({
            **issue_tokens(user),
            "baglam": kayit.baglam,
            "maskeli_eposta": hs.maskele_eposta(user.email),
        }, message="Web hesabınızla oturum açıldı.")
