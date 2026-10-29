"""
E-posta doğrulama kodu — kayıt ve şifre sıfırlama.

Akış: `kod_gonder()` 6 haneli kod üretir, özetini `EpostaKodu`'na yazar ve Google
Amazon SES (SMTP) ile gönderir; `dogrula()` kodu kontrol edip TEK KULLANIMLIK tüketir.

**Sınırlar** (hepsi her zaman UYGULANIR — `core/throttling.py`'nin gözlem modundan
bağımsız; e-posta göndermek, kodu tahmin etmek ve şifre denemek gerçek maliyeti olan
işlemlerdir):
- e-posta başına: yeniden gönderim beklemesi (`EPOSTA_KOD_TEKRAR_SN`), saatlik
  tavan (`EPOSTA_KOD_SAATLIK`), kod başına hatalı deneme (`EPOSTA_KOD_DENEME`);
- IP başına: `EpostaIpThrottle` (kod gönderme / doğrulama / giriş ayrı kovalar);
- küresel: günlük gönderim tavanı (`EPOSTA_GUNLUK_TAVAN`) — SES kotasını korur.

⚠️ Hata gövdesi `{code, detail, ...}` (bkz. `EpostaHatasi`); istemciler `errors.code`'a
bakar. Kodların listesi `docs/mobil-eposta-kayit.md`'de.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.throttling import BaseThrottle

from accounts.models import EpostaKodu

logger = logging.getLogger("ihaletakip")

MSG_GONDERILEMEDI = (
    "Doğrulama kodu şu an gönderilemedi. Lütfen birkaç dakika sonra tekrar deneyin."
)
MSG_KOD_HATALI = "Kod hatalı. Lütfen e-postanızdaki 6 haneli kodu kontrol edin."
MSG_KOD_YOK = "Kodun süresi dolmuş ya da kod geçersiz. Lütfen yeni bir kod isteyin."
MSG_KOD_DENEME = "Çok fazla hatalı deneme yapıldı. Lütfen yeni bir kod isteyin."

# IP başına (pencere sn, sınır). Kod gönderimi en sıkı kovadır: her istek gerçek bir
# e-posta demek. ⚠️ Mobil operatör NAT'ı (CGNAT) aynı IP'yi paylaştırabilir — kayıt
# seyrek bir olay olduğu için bu eşikler orada da rahat kalır.
VARSAYILAN_IP_ORANLARI = {
    "kod_gonder": [(600, 10), (86400, 40)],
    "kod_dogrula": [(600, 30), (86400, 150)],
    "giris": [(600, 30), (86400, 300)],
}

KONULAR = {
    EpostaKodu.Amac.KAYIT: "İhale Takip doğrulama kodunuz: {kod}",
    EpostaKodu.Amac.SIFRE: "İhale Takip şifre sıfırlama kodunuz: {kod}",
}


class EpostaHatasi(APIException):
    """Zarf: `errors = {code, detail, ...ek}` — `PremiumRequired` ile aynı desen.

    ⚠️ `detail` DRF'in `_get_error_details`'inden GEÇİRİLMEZ: o tüm değerleri
    `ErrorDetail` string'ine çevirir ve `retry_after` istemciye `"45"` olarak giderdi.
    """

    def __init__(self, mesaj: str, kod: str, status_code: int = 400, **ek):
        super().__init__(mesaj)
        self.status_code = status_code
        self.detail = {"code": kod, "detail": mesaj, **ek}
        if "retry_after" in ek:
            self.wait = ek["retry_after"]  # DRF `Retry-After` başlığını buradan yazar


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _ozet(email: str, amac: str, kod: str) -> str:
    anahtar = settings.SECRET_KEY.encode()
    return hmac.new(anahtar, f"{amac}:{email}:{kod}".encode(), hashlib.sha256).hexdigest()


def kod_ayarlari() -> dict:
    """İstemciye dönen süreler (geri sayım ekranları bunlarla kurulur)."""
    return {
        "gecerlilik_sn": settings.EPOSTA_KOD_SURE_DK * 60,
        "tekrar_gonderim_sn": settings.EPOSTA_KOD_TEKRAR_SN,
    }


def _gunluk_hak_var() -> bool:
    from core.throttling import _say

    try:
        gun = timezone.localdate().isoformat()
        return _say(f"eposta:gunluk:{gun}", 2 * 86400) <= settings.EPOSTA_GUNLUK_TAVAN
    except Exception:  # noqa: BLE001 — Redis yoksa gönderim engellenmesin
        return True


def _postala(email: str, amac: str, kod: str) -> None:
    baglam = {
        "kod": kod,
        "sure_dk": settings.EPOSTA_KOD_SURE_DK,
        "kayit": amac == EpostaKodu.Amac.KAYIT,
    }
    mesaj = EmailMultiAlternatives(
        subject=KONULAR[amac].format(kod=kod),
        body=render_to_string("eposta/kod.txt", baglam),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    mesaj.attach_alternative(render_to_string("eposta/kod.html", baglam), "text/html")
    mesaj.send(fail_silently=False)


def kod_gonder(email: str, amac: str, veri: dict | None = None) -> None:
    """Yeni kod üretip gönderir. Bekleme/tavan/gönderim hatasında `EpostaHatasi`.

    ⚠️ Gönderim transaction İÇİNDE yapılır: SMTP düşerse satır geri alınır, yani
    başarısız deneme ne bekleme süresini ne saatlik hakkı yer. Satır kilidi yalnızca
    o e-postanın satırını tutar (SMTP ~1-2 sn).
    """
    email = normalize_email(email)
    simdi = timezone.now()
    with transaction.atomic():
        kayit, _ = EpostaKodu.objects.get_or_create(email=email, amac=amac)
        kayit = EpostaKodu.objects.select_for_update().get(pk=kayit.pk)

        if kayit.son_gonderim:
            kalan = settings.EPOSTA_KOD_TEKRAR_SN - (simdi - kayit.son_gonderim).total_seconds()
            if kalan > 0:
                raise EpostaHatasi(
                    "Yeni kod istemeden önce biraz bekleyin.", "code_cooldown", 429,
                    retry_after=math.ceil(kalan))
        if kayit.pencere_bas is None or (simdi - kayit.pencere_bas).total_seconds() >= 3600:
            kayit.pencere_bas, kayit.gonderim = simdi, 0
        if kayit.gonderim >= settings.EPOSTA_KOD_SAATLIK:
            kalan = 3600 - (simdi - kayit.pencere_bas).total_seconds()
            raise EpostaHatasi(
                "Bu e-posta için çok fazla kod istendi. Lütfen daha sonra tekrar deneyin.",
                "code_limit", 429, retry_after=math.ceil(kalan))
        if not _gunluk_hak_var():
            logger.error("e-posta günlük gönderim tavanı doldu (%s)", settings.EPOSTA_GUNLUK_TAVAN)
            raise EpostaHatasi(MSG_GONDERILEMEDI, "send_failed", 503)

        kod = f"{secrets.randbelow(10**6):06d}"
        kayit.kod_hash = _ozet(email, amac, kod)
        kayit.gecerlilik = simdi + timedelta(minutes=settings.EPOSTA_KOD_SURE_DK)
        kayit.deneme = 0
        kayit.kullanildi_at = None
        kayit.son_gonderim = simdi
        kayit.gonderim += 1
        kayit.veri = veri or {}
        kayit.save()
        try:
            _postala(email, amac, kod)
        except Exception as e:  # noqa: BLE001 — SMTP/ağ hatası kullanıcıya 503 olur
            # ⚠️ Alıcı adresi loglanır ama kod ASLA loglanmaz.
            logger.warning("doğrulama kodu gönderilemedi (%s, %s): %s",
                           amac, email, str(e)[:200])
            raise EpostaHatasi(MSG_GONDERILEMEDI, "send_failed", 503) from e
    logger.info("doğrulama kodu gönderildi (%s, %s)", amac, email)


def dogrula(email: str, amac: str, kod: str) -> EpostaKodu:
    """Kodu kontrol edip tüketir; başarıda satırı döner, aksi hâlde `EpostaHatasi`.

    ⚠️ Hatalı deneme sayacı transaction'ın DIŞINDA fırlatılan hatayla korunur:
    istisna `atomic` içinde atılsaydı artırılan sayaç da geri alınır ve kod sınırsız
    tahmin edilebilirdi.
    """
    email = normalize_email(email)
    kod = "".join(c for c in (kod or "") if c.isdigit())
    simdi = timezone.now()
    hata = None
    with transaction.atomic():
        kayit = EpostaKodu.objects.select_for_update().filter(email=email, amac=amac).first()
        if (kayit is None or not kayit.kod_hash or kayit.kullanildi_at
                or kayit.gecerlilik is None or kayit.gecerlilik <= simdi):
            hata = EpostaHatasi(MSG_KOD_YOK, "code_expired")
        elif kayit.deneme >= settings.EPOSTA_KOD_DENEME:
            hata = EpostaHatasi(MSG_KOD_DENEME, "code_attempts")
        elif not hmac.compare_digest(kayit.kod_hash, _ozet(email, amac, kod)):
            kayit.deneme += 1
            kayit.save(update_fields=["deneme"])
            kalan = settings.EPOSTA_KOD_DENEME - kayit.deneme
            hata = (EpostaHatasi(MSG_KOD_DENEME, "code_attempts") if kalan <= 0
                    else EpostaHatasi(MSG_KOD_HATALI, "code_invalid", kalan_deneme=kalan))
        else:
            kayit.kullanildi_at = simdi
            kayit.save(update_fields=["kullanildi_at"])
    if hata:
        raise hata
    return kayit


class EpostaIpThrottle(BaseThrottle):
    """IP başına sınır; görünüm `eposta_kapsam = "<kova>"` ile katılır.

    Kimlik `core.throttling.istemci()`'den gelir → web portalında (anahtar doğruysa)
    `X-IT-Client-IP`, diğerlerinde `X-Real-IP`. ⚠️ `PORTAL_KEY` boşken web'in TÜM
    kullanıcıları konteynerin tek IP'sini paylaşır — web e-posta girişini açmadan önce
    anahtar iki tarafa da yazılmalı.
    ⚠️ Redis/kod hatasında istek GEÇER (koruma API'yi kırmaz) — e-posta başına
    sınırlar DB'de olduğu için o durumda da çalışmaya devam eder.
    """

    def __init__(self):
        self._bekle = None

    def oran_tablosu(self) -> dict:
        return {**VARSAYILAN_IP_ORANLARI, **(settings.EPOSTA_IP_ORANLARI or {})}

    def allow_request(self, request, view):
        kapsam = getattr(view, "eposta_kapsam", None)
        if not kapsam:
            return True
        try:
            from core.throttling import _say, istemci

            tablo = self.oran_tablosu()
            kimlik = istemci(request).kimlik
            simdi = timezone.now().timestamp()
            for pencere, sinir in tablo.get(kapsam, []):
                n = _say(f"thr:ep:{kapsam}:{kimlik}:{pencere}:{int(simdi // pencere)}",
                         int(pencere) + 5)
                if n > sinir:
                    self._bekle = max(self._bekle or 0, int(pencere - simdi % pencere) + 1)
            if self._bekle:
                logger.info("e-posta IP sınırı: kapsam=%s kimlik=%s", kapsam, kimlik)
            return self._bekle is None
        except Exception as e:  # noqa: BLE001
            logger.warning("e-posta throttle hatası (istek geçirildi): %s", str(e)[:200])
            return True

    def wait(self):
        return self._bekle
