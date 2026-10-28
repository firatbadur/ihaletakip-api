"""
Web'den Pro satışı — iyzico Checkout Form akışı (web 6. not #17-#19).

Akış: `checkout_ac` (Payment pending + iyzico initialize → ödeme sayfası adresi) →
kullanıcı iyzico'da öder → iyzico tarayıcıyı web'in `callbackUrl`'ine POST'lar → web
token'ı `tamamla`'ya iletir → biz iyzico'ya SORARIZ, doğrularız, hakkı açarız.
Dönüş hiç gelmezse (sekme kapandı) `mutabakat` 15 dk'da bir aynı `tamamla` yolunu koşar.

⚠️⚠️ **Hak açmanın TEK yolu `_isle`'dir** — `complete/` ve mutabakat aynı fonksiyonu
çağırır. İki yol ayrı yazılsaydı biri tutar kontrolünü, diğeri idempotensliği unuturdu.
⚠️ İdempotenslik iki katmanlı: `Payment` satır kilidi (`select_for_update`) +
`ProGrant.payment` OneToOne (aynı ödemeye ikinci hak DB'de reddedilir).
⚠️⚠️ `failure` iyzico'ya karşı TERMİNAL DEĞİLDİR: iyzico sonradan SUCCESS derse (para
çekilmiş) hak yine açılır. Tersini yapmak "para alındı, Pro açılmadı" demekti.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import iyzico
from .iyzico import IyzicoError

logger = logging.getLogger("ihaletakip")

PLAN_KODU = "pro_yearly"
PLAN_ADI = "Pro — 1 yıl"
PLAN_AY = 12
SEPET_ADI = "İhale Takip Pro — 1 yıl"
ONAY_ALANLARI = ("on_bilgilendirme", "mesafeli_satis", "cayma_istisnasi")

# iyzico checkout token'ının ömrü ~30 dk (`tokenExpireTime`). Bu süre dolmadan
# "karar yok" yanıtı başarısızlık SAYILMAZ: kullanıcı hâlâ ödeme sayfasında olabilir.
TOKEN_OMRU = timedelta(minutes=60)

MSG_BASARI = "Ödemeniz alındı. Pro üyeliğiniz {tarih} tarihine kadar açık."
MSG_BEKLIYOR = "Ödemeniz doğrulanıyor; birkaç dakika içinde hesabınıza yansıyacak."
MSG_BASARISIZ = "Ödeme tamamlanamadı. Kartınızdan ücret alınmadı; tekrar deneyebilirsiniz."
MSG_IADE = "Bu ödeme iade edildi."


class OdemeHatasi(Exception):
    def __init__(self, mesaj: str, kod: str, status: int = 400):
        super().__init__(mesaj)
        self.mesaj, self.kod, self.status = mesaj, kod, status


def plan_tutari() -> Decimal | None:
    ham = (getattr(settings, "PRO_YEARLY_PRICE", "") or "").strip()
    if not ham:
        return None
    try:
        d = Decimal(ham).quantize(Decimal("0.01"))
    except InvalidOperation:
        logger.error("PRO_YEARLY_PRICE geçersiz: %r", ham)
        return None
    return d if d > 0 else None


def satis_acik() -> bool:
    return bool(
        settings.WEB_SATIS_ENABLED and plan_tutari()
        and settings.IYZICO_API_KEY and settings.IYZICO_SECRET_KEY
    )


def plan() -> dict:
    tutar = plan_tutari()
    return {
        "code": PLAN_KODU,
        "name": PLAN_ADI,
        "amount": iyzico.fiyat(tutar) if tutar else None,
        "vat_rate": settings.PRO_VAT_RATE,
        "currency": "TRY",
        "months": PLAN_AY,
        "available": satis_acik(),
    }


def _ad_soyad(tam: str) -> tuple[str, str]:
    parca = (tam or "").split()
    if len(parca) < 2:
        return (tam or "-"), (tam or "-")
    return " ".join(parca[:-1]), parca[-1]


def _iyzico_govdesi(odeme, user, profil: dict, ip: str) -> dict:
    ad, soyad = _ad_soyad(profil["full_name"])
    kurumsal = profil["type"] == "kurumsal"
    adres = ", ".join(x for x in (profil["address"], profil.get("district")) if x)
    tutar = iyzico.fiyat(odeme.amount)
    return {
        "locale": "tr",
        "conversationId": str(odeme.pk),
        "price": tutar,
        "paidPrice": tutar,
        "currency": odeme.currency,
        "basketId": str(odeme.pk),
        "paymentGroup": "PRODUCT",
        "callbackUrl": settings.IYZICO_CALLBACK_URL,
        "enabledInstallments": [1],
        "buyer": {
            "id": str(user.pk),
            "name": ad,
            "surname": soyad,
            "gsmNumber": profil["phone"],
            "email": user.email,
            # Kurumsal alıcıda VKN (10 hane) — iyzico kimlik alanı 10-11 hane kabul eder.
            "identityNumber": profil["tax_number"] if kurumsal else profil["identity_number"],
            "registrationAddress": adres,
            "ip": ip,
            "city": profil["city"],
            "country": "Turkey",
        },
        "billingAddress": {
            "contactName": profil["company_name"] if kurumsal else profil["full_name"],
            "city": profil["city"],
            "country": "Turkey",
            "address": adres,
        },
        "basketItems": [{
            "id": PLAN_KODU,
            "name": SEPET_ADI,
            "category1": "Dijital hizmet",
            "itemType": "VIRTUAL",
            "price": tutar,
        }],
    }


def checkout_ac(user, *, consents: dict, consent_version: str, ip: str) -> str:
    """Payment(pending) açar, iyzico ödeme sayfası adresini döner."""
    from subscription.models import BillingProfile, Payment
    from subscription.serializers import profil_eksikleri

    if not satis_acik():
        raise OdemeHatasi("Web'den satış şu an kapalı.", "sales_closed", 503)
    eksik_onay = [k for k in ONAY_ALANLARI if consents.get(k) is not True]
    if eksik_onay or not (consent_version or "").strip():
        raise OdemeHatasi(
            "Satın almak için ön bilgilendirme, mesafeli satış sözleşmesi ve cayma "
            "hakkı istisnası onaylarının üçü de gereklidir.", "consents_required")
    profil = BillingProfile.objects.filter(user=user).first()
    if profil is None or profil_eksikleri(profil):
        raise OdemeHatasi("Ödemeden önce fatura bilgilerinizi tamamlayın.",
                          "billing_profile_required")

    simdi = timezone.now()
    odeme = Payment.objects.create(
        user=user,
        plan_code=PLAN_KODU,
        months=PLAN_AY,
        amount=plan_tutari(),
        vat_rate=settings.PRO_VAT_RATE,
        currency="TRY",
        billing_snapshot=profil.anlik_goruntu(),
        consents={
            **{k: True for k in ONAY_ALANLARI},
            "version": consent_version.strip()[:64],
            "at": simdi.isoformat(),
            "ip": ip,
        },
        client_ip=ip or None,
    )
    try:
        veri = iyzico.checkout_baslat(_iyzico_govdesi(odeme, user, odeme.billing_snapshot, ip))
    except IyzicoError as e:
        logger.warning("checkout başlatılamadı odeme=%s: %s", odeme.pk, e)
        _basarisiz(odeme, "init_error", "Ödeme sayfası açılamadı.")
        raise OdemeHatasi("Ödeme sayfası şu an açılamadı, lütfen tekrar deneyin.",
                          "payment_provider_error", 502) from e
    if veri.get("status") != "success" or not veri.get("paymentPageUrl"):
        _basarisiz(odeme, veri.get("errorCode") or "init_failed",
                   veri.get("errorMessage") or "Ödeme sayfası açılamadı.")
        raise OdemeHatasi(veri.get("errorMessage") or "Ödeme sayfası açılamadı.",
                          "payment_provider_error", 502)
    odeme.iyzico_token = veri.get("token") or ""
    odeme.save(update_fields=["iyzico_token", "updated_at"])
    return veri["paymentPageUrl"]


def _basarisiz(odeme, kod: str, mesaj: str) -> None:
    odeme.status = odeme.Status.FAILURE
    odeme.error_code = (kod or "")[:64]
    odeme.error_message = (mesaj or "")[:500]
    odeme.save(update_fields=["status", "error_code", "error_message", "updated_at"])


def sonuc(odeme) -> dict:
    """`complete/` yanıtı — aynı ödeme için her çağrıda AYNI cevap (idempotens)."""
    from subscription.models import Payment

    if odeme.status == Payment.Status.SUCCESS:
        bitis = getattr(getattr(odeme, "grant", None), "ends_at", None)
        tarih = timezone.localtime(bitis).strftime("%d.%m.%Y") if bitis else ""
        return {"status": "success", "message": MSG_BASARI.format(tarih=tarih),
                "pro_until": bitis.isoformat() if bitis else None}
    if odeme.status in (Payment.Status.REFUNDED, Payment.Status.PARTIALLY_REFUNDED):
        return {"status": "failure", "message": MSG_IADE}
    if odeme.status == Payment.Status.FAILURE:
        return {"status": "failure", "message": odeme.error_message or MSG_BASARISIZ}
    return {"status": "pending", "message": MSG_BEKLIYOR}


def _tutar_esit(a, b) -> bool:
    try:
        return Decimal(str(a)).quantize(Decimal("0.01")) == Decimal(str(b)).quantize(
            Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return False


def _hak_ac(odeme, veri: dict) -> None:
    from dateutil.relativedelta import relativedelta

    from subscription.models import ProGrant

    from .pro_hak import recompute_pro, web_baslangic

    kalem = (veri.get("itemTransactions") or [{}])[0] or {}
    odeme.status = odeme.Status.SUCCESS
    odeme.paid_at = timezone.now()
    odeme.iyzico_payment_id = str(veri.get("paymentId") or "")[:64]
    odeme.iyzico_transaction_id = str(kalem.get("paymentTransactionId") or "")[:64]
    odeme.card_last4 = str(veri.get("lastFourDigits") or "")[:4]
    odeme.card_association = str(veri.get("cardAssociation") or "")[:32]
    odeme.error_code = odeme.error_message = ""
    odeme.save()
    bas = web_baslangic(odeme.user)
    ProGrant.objects.create(
        user=odeme.user, source=ProGrant.Source.IYZICO, payment=odeme,
        starts_at=bas, ends_at=bas + relativedelta(months=odeme.months),
    )
    recompute_pro(odeme.user)

    def _sonra():
        from subscription.tasks import fatura_kes_task, odeme_eposta_task

        odeme_eposta_task.delay(odeme.pk, "alindi")
        fatura_kes_task.delay(odeme.pk)

    transaction.on_commit(_sonra)


def _isle(odeme_pk: int, veri: dict, token: str, *, kesin: bool) -> dict:
    """iyzico detay yanıtını ödemeye uygular. `kesin=False` (mutabakat): token ömrü
    dolmadan "karar yok" başarısızlık sayılmaz."""
    from subscription.models import Payment

    with transaction.atomic():
        odeme = Payment.objects.select_for_update().select_related("user").get(pk=odeme_pk)
        if odeme.status not in (Payment.Status.PENDING, Payment.Status.FAILURE):
            return sonuc(odeme)
        if odeme.iyzico_token and token != odeme.iyzico_token:
            logger.warning("iyzico token ödemeyle eşleşmedi odeme=%s", odeme.pk)
            return {"status": "failure", "message": MSG_BASARISIZ}

        if iyzico.yanit_imzasi_gecerli(veri) is False:
            logger.error("iyzico yanıt imzası tutmadı odeme=%s", odeme.pk)
            if settings.IYZICO_YANIT_IMZA_ZORUNLU:
                return sonuc(odeme)

        eski_kayit = timezone.now() - odeme.created_at > TOKEN_OMRU
        durum = (veri.get("paymentStatus") or "").upper()
        istek_basarili = veri.get("status") == "success"

        if istek_basarili and durum == "SUCCESS":
            hatalar = []
            if str(veri.get("conversationId") or "") != str(odeme.pk):
                hatalar.append("conversationId")
            if not _tutar_esit(veri.get("paidPrice"), odeme.amount):
                hatalar.append("paidPrice")
            if (veri.get("currency") or "") != odeme.currency:
                hatalar.append("currency")
            if hatalar:
                # ⚠️ Para çekilmiş ama beklenen ödeme değil → hak AÇILMAZ, admin iade eder.
                logger.error("iyzico ödeme doğrulaması tutmadı odeme=%s alanlar=%s",
                             odeme.pk, hatalar)
                _basarisiz(odeme, "dogrulama_" + "_".join(hatalar),
                           "Ödeme doğrulanamadı. Destek ekibimiz sizinle iletişime geçecek.")
                return sonuc(odeme)
            fraud = veri.get("fraudStatus")
            if fraud == -1:
                _basarisiz(odeme, "fraud", MSG_BASARISIZ)
                return sonuc(odeme)
            if fraud == 0:  # iyzico incelemesinde → mutabakat sonra bakar
                return sonuc(odeme)
            _hak_ac(odeme, veri)
            return sonuc(odeme)

        if odeme.status == Payment.Status.FAILURE:
            return sonuc(odeme)  # iyzico SUCCESS demedikçe dokunulmaz
        if istek_basarili and durum == "FAILURE":
            _basarisiz(odeme, veri.get("errorCode") or "failure",
                       veri.get("errorMessage") or MSG_BASARISIZ)
        elif (kesin and not istek_basarili) or eski_kayit:
            _basarisiz(odeme, veri.get("errorCode") or ("zaman_asimi" if eski_kayit else
                                                          "failure"),
                       veri.get("errorMessage") or MSG_BASARISIZ)
        return sonuc(odeme)


def tamamla(token: str, *, kesin: bool = True) -> dict:
    """`complete/` ucu + mutabakat. Bilinmeyen/sahte token → `failure` (404 değil)."""
    from subscription.models import Payment

    token = (token or "").strip()
    if not token or len(token) > 128:
        return {"status": "failure", "message": MSG_BASARISIZ}
    odeme = Payment.objects.filter(iyzico_token=token).first()
    if odeme is None:
        logger.info("complete: bilinmeyen token")
        return {"status": "failure", "message": MSG_BASARISIZ}
    if odeme.status not in (Payment.Status.PENDING, Payment.Status.FAILURE):
        return sonuc(odeme)
    try:
        veri = iyzico.checkout_detay(token, str(odeme.pk))
    except IyzicoError as e:
        logger.warning("iyzico detay sorgusu başarısız odeme=%s: %s", odeme.pk, e)
        return sonuc(odeme)
    return _isle(odeme.pk, veri, token, kesin=kesin)


def mutabakat(sinir: int = 100) -> dict:
    """15 dk'dan eski `pending` ödemeleri iyzico'dan sorup kapatır."""
    from subscription.models import Payment

    simdi = timezone.now()
    adaylar = list(
        Payment.objects.filter(
            status=Payment.Status.PENDING, created_at__lte=simdi - timedelta(minutes=15)
        ).order_by("created_at")[:sinir]
    )
    sayac = {"success": 0, "failure": 0, "pending": 0}
    for odeme in adaylar:
        if not odeme.iyzico_token:
            _basarisiz(odeme, "token_yok", MSG_BASARISIZ)
            sayac["failure"] += 1
            continue
        sayac[tamamla(odeme.iyzico_token, kesin=False)["status"]] += 1
    if adaylar:
        logger.info("ödeme mutabakatı: %s", sayac)
    return sayac


def iade_et(odeme, *, ip: str = "") -> None:
    """İSTİSNAİ tam iade (admin): iyzico iade + hak iptali + e-posta."""
    from .pro_hak import recompute_pro

    if odeme.status != odeme.Status.SUCCESS:
        raise OdemeHatasi("Yalnızca başarılı ödeme iade edilebilir.", "not_refundable")
    if not odeme.iyzico_transaction_id:
        raise OdemeHatasi("Ödemenin iyzico işlem kimliği yok; iade iyzico panelinden "
                          "yapılmalı.", "no_transaction_id")
    veri = iyzico.iade(odeme.iyzico_transaction_id, odeme.amount, odeme.currency,
                       f"iade-{odeme.pk}", ip)
    if veri.get("status") != "success":
        raise OdemeHatasi(veri.get("errorMessage") or "iyzico iadeyi reddetti.",
                          veri.get("errorCode") or "refund_failed")
    simdi = timezone.now()
    with transaction.atomic():
        odeme.status = odeme.Status.REFUNDED
        odeme.refunded_at = simdi
        odeme.save(update_fields=["status", "refunded_at", "updated_at"])
        grant = getattr(odeme, "grant", None)
        if grant is not None and grant.revoked_at is None:
            grant.revoked_at = simdi
            grant.save(update_fields=["revoked_at", "updated_at"])
        recompute_pro(odeme.user)

        def _sonra():
            from subscription.tasks import odeme_eposta_task

            odeme_eposta_task.delay(odeme.pk, "iade")

        transaction.on_commit(_sonra)


def fesih_talebi(odeme) -> dict:
    """Fesih talebi — iade YOK, Pro süre sonuna kadar açık (ürün kararı 2026-10-28)."""
    from .pro_hak import etkin_hak

    if odeme.status != odeme.Status.SUCCESS:
        raise OdemeHatasi("Bu ödeme için fesih talebi oluşturulamaz.", "not_cancellable")
    ilk = odeme.cancel_requested_at is None
    if ilk:
        odeme.cancel_requested_at = timezone.now()
        odeme.save(update_fields=["cancel_requested_at", "updated_at"])
    hak = etkin_hak(odeme.user)
    bitis = hak.ends_at if hak.pro else getattr(getattr(odeme, "grant", None), "ends_at", None)
    if ilk:
        from subscription.tasks import odeme_eposta_task

        transaction.on_commit(lambda: odeme_eposta_task.delay(odeme.pk, "fesih"))
    return {
        "cancel_requested_at": odeme.cancel_requested_at.isoformat(),
        "pro_until": bitis.isoformat() if bitis else None,
        "refund": False,
    }
