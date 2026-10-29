"""
Web → mobil geçiş kodu (handoff) — web'de girişli kullanıcı QR'ı telefonla okutur,
uygulama AYNI hesapla, giriş ekranı görmeden ödeme ekranında açılır.

Akış (istemci sözleşmesi: `docs/web-mobil-gecis.md`):
1. Web `olustur()` → uzun kod (QR: `https://ihaletakip.com/q/<kod>`) + 7 karakterlik
   kısa kod (QR'ın altında, elle yazmak için).
2. Telefon `/q/<kod>`'u açar → web `okutuldu_isaretle()` → OneLink'e yönlendirir
   (`deep_link_sub2=<kod>`).
3. Uygulama `bozdur()` → kullanıcının token çifti.
4. Web `GET` ile durumu yoklar: bekliyor → okutuldu → kullanildi (+ `is_premium`).

**Güvenlik** — kod kısa süreli bir giriş anahtarıdır:
- Uzun kod 32 bayt rastgele (`token_urlsafe`), kısa kod 7 karakter / 31 harfli alfabe
  (~2,75 × 10¹⁰). İkisi de yalnızca HMAC özetiyle saklanır.
- Tek kullanımlık, `WEB_HANDOFF_SURE_DK` (15) dk geçerli; kullanıcı başına aynı anda en
  çok `WEB_HANDOFF_AKTIF_AZAMI` canlı kod (fazlası iptal edilir).
- Kısa kod tahmini: IP başına sınır (`HandoffThrottle`) + KÜRESEL hatalı deneme sigortası
  (`WEB_HANDOFF_KISA_KURESEL_HATA`): dağıtık bir tahmin denemesi (çok IP) eşiği aşarsa
  kısa kodla bozdurma 10 dk kapanır. QR yolu (uzun kod) etkilenmez.
- ⚠️ Kodlar ASLA loglanmaz.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import WebHandoff

from .eposta_kod import EpostaHatasi, EpostaIpThrottle

logger = logging.getLogger("ihaletakip")

# Karışan karakterler yok: 0/O, 1/I/L.
KISA_ALFABE = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
KISA_UZUNLUK = 7
_BAGLAM_RE = re.compile(r"^[a-z0-9_\-]{0,40}$")
_KURESEL_PENCERE_SN = 600

MSG_GECERSIZ = (
    "Bu kodun süresi dolmuş ya da kod zaten kullanılmış. "
    "Web'deki Pro sayfasından yeni bir kod alın."
)
MSG_MESGUL = "Kod doğrulaması şu an yapılamıyor. Lütfen QR kodu okutarak devam edin."

# IP (anonim) / kullanıcı (girişli) başına (pencere sn, sınır).
# ⚠️ Oluşturma: web QR'ı süre dolunca yeniler ve birden çok sekme açık olabilir.
VARSAYILAN_IP_ORANLARI = {
    "handoff_olustur": [(600, 30), (86400, 300)],
    "handoff_bozdur": [(600, 20), (86400, 100)],
}


class HandoffHatasi(EpostaHatasi):
    """Zarf `errors = {code, detail}` — e-posta kodu hatalarıyla aynı sözleşme."""


class HandoffThrottle(EpostaIpThrottle):
    """`EpostaIpThrottle` ile aynı mekanizma (her zaman UYGULANIR), ayrı oran tablosu."""

    def oran_tablosu(self) -> dict:
        return {**VARSAYILAN_IP_ORANLARI, **(settings.WEB_HANDOFF_IP_ORANLARI or {})}


def _ozet(tur: str, deger: str) -> str:
    anahtar = settings.SECRET_KEY.encode()
    return hmac.new(anahtar, f"handoff:{tur}:{deger}".encode(), hashlib.sha256).hexdigest()


def kisa_normalize(deger: str) -> str:
    """Kullanıcının yazdığı kısa kod → kanonik biçim (tire/boşluk atılır, büyük harf)."""
    return "".join(c for c in (deger or "").upper() if c.isalnum())


def baglam_temizle(deger) -> str:
    deger = (deger or "").strip().lower()
    return deger if _BAGLAM_RE.match(deger) else ""


def maskele_eposta(email: str) -> str:
    """`ahmet@firma.com.tr` → `a***@firma.com.tr` (yanlış hesap fark edilsin diye)."""
    if not email or "@" not in email:
        return ""
    yerel, alan = email.split("@", 1)
    return f"{yerel[:1]}***@{alan}"


def _aktif_q():
    return {"durum__in": WebHandoff.AKTIF, "gecerlilik__gt": timezone.now()}


def olustur(user, baglam: str = "", ip: str | None = None) -> tuple[WebHandoff, str, str]:
    """Yeni geçiş kodu → (satır, uzun kod, kısa kod). Kodlar yalnızca burada görünür."""
    simdi = timezone.now()
    kod = secrets.token_urlsafe(32)
    with transaction.atomic():
        # Kullanıcı başına canlı kod tavanı: en yeni (AZAMI-1) kalır, gerisi iptal.
        azami = max(1, settings.WEB_HANDOFF_AKTIF_AZAMI)
        eski = list(WebHandoff.objects.filter(user=user, **_aktif_q())
                    .order_by("-created_at").values_list("pk", flat=True)[azami - 1:])
        if eski:
            WebHandoff.objects.filter(pk__in=eski).update(
                durum=WebHandoff.Durum.IPTAL, iptal_at=simdi)

        # Kısa kod canlı kodlar arasında tekil olmalı (çakışma ~10⁻⁹, yine de bakılır).
        for _ in range(5):
            kisa = "".join(secrets.choice(KISA_ALFABE) for _ in range(KISA_UZUNLUK))
            kisa_hash = _ozet("kisa", kisa)
            if not WebHandoff.objects.filter(kisa_hash=kisa_hash, **_aktif_q()).exists():
                break
        kayit = WebHandoff.objects.create(
            user=user,
            kod_hash=_ozet("kod", kod),
            kisa_hash=kisa_hash,
            baglam=baglam_temizle(baglam),
            gecerlilik=simdi + timedelta(minutes=settings.WEB_HANDOFF_SURE_DK),
            olusturan_ip=ip,
        )
    logger.info("web handoff oluşturuldu: id=%s uid=%s baglam=%s", kayit.pk, user.pk, kayit.baglam)
    return kayit, kod, kisa


def okutuldu_isaretle(kod: str) -> WebHandoff | None:
    """`/q/<kod>` açıldı. Kayıt yoksa None; varsa (süresi dolmuş olsa bile) satır.

    Yalnızca `bekliyor` → `okutuldu` geçişi yapılır; diğer durumlar korunur.
    """
    kayit = WebHandoff.objects.filter(kod_hash=_ozet("kod", kod or "")).first()
    if kayit is None:
        return None
    if kayit.etkin_durum == WebHandoff.Durum.BEKLIYOR:
        simdi = timezone.now()
        n = WebHandoff.objects.filter(pk=kayit.pk, durum=WebHandoff.Durum.BEKLIYOR).update(
            durum=WebHandoff.Durum.OKUTULDU, okutuldu_at=simdi)
        if n:
            kayit.durum, kayit.okutuldu_at = WebHandoff.Durum.OKUTULDU, simdi
    return kayit


def iptal(kayit: WebHandoff) -> None:
    if kayit.durum in WebHandoff.AKTIF:
        WebHandoff.objects.filter(pk=kayit.pk, durum__in=WebHandoff.AKTIF).update(
            durum=WebHandoff.Durum.IPTAL, iptal_at=timezone.now())


def _kuresel_hata_say() -> int:
    from core.throttling import _say

    try:
        dilim = int(timezone.now().timestamp() // _KURESEL_PENCERE_SN)
        return _say(f"handoff:kisa_hata:{dilim}", _KURESEL_PENCERE_SN + 5)
    except Exception:  # noqa: BLE001 — Redis yoksa sigorta devre dışı, IP sınırı sürer
        return 0


def _kuresel_kilitli() -> bool:
    from django.core.cache import cache

    try:
        dilim = int(timezone.now().timestamp() // _KURESEL_PENCERE_SN)
        n = cache.get(f"handoff:kisa_hata:{dilim}") or 0
    except Exception:  # noqa: BLE001
        return False
    return n >= settings.WEB_HANDOFF_KISA_KURESEL_HATA


def bozdur(*, kod: str = "", kisa_kod: str = "", ip: str | None = None) -> WebHandoff:
    """Kodu tüketir; başarıda satırı (user dahil) döner, aksi hâlde `HandoffHatasi`.

    ⚠️ Hata transaction'ın DIŞINDA fırlatılır (`eposta_kod.dogrula` ile aynı kural):
    içeride atılsaydı sayaç/işaret de geri alınırdı.
    """
    if kod:
        filtre = {"kod_hash": _ozet("kod", kod)}
    else:
        kisa = kisa_normalize(kisa_kod)
        if _kuresel_kilitli():
            logger.error("web handoff: kısa kod küresel hata eşiği aşıldı — kısa kod kapalı")
            raise HandoffHatasi(MSG_MESGUL, "handoff_busy", 429, retry_after=_KURESEL_PENCERE_SN)
        filtre = {"kisa_hash": _ozet("kisa", kisa), **_aktif_q()}

    simdi = timezone.now()
    hata = None
    with transaction.atomic():
        kayit = (WebHandoff.objects.select_for_update().select_related("user")
                 .filter(**filtre).order_by("-created_at").first())
        if (kayit is None or kayit.durum not in WebHandoff.AKTIF
                or kayit.gecerlilik <= simdi or not kayit.user.is_active):
            hata = HandoffHatasi(MSG_GECERSIZ, "handoff_invalid")
        else:
            kayit.durum = WebHandoff.Durum.KULLANILDI
            kayit.kullanildi_at = simdi
            kayit.kullanan_ip = ip
            kayit.save(update_fields=["durum", "kullanildi_at", "kullanan_ip"])
    if hata:
        if not kod:
            _kuresel_hata_say()
        raise hata
    logger.info("web handoff bozduruldu: id=%s uid=%s yol=%s",
                kayit.pk, kayit.user_id, "qr" if kod else "kisa")
    return kayit
