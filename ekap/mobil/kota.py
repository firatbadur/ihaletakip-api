"""
Kişi başı EKAP isteği kotası — ortak kullanıcı bütçesini (`throttle`, `kullanici`) tek bir
hesabın tüketmesini engeller.

⚠️⚠️ **Neden var (2026-11-05 arızası).** Betikle açılmış tek bir hesap (mailinator, headless
tarayıcı) gece 3 saatte ~23 bin web sayfası gezdi; ortak 600'lük kullanıcı bütçesi 01:05'te
bitti ve gün boyu HERKES için DT ayrıntısı, doküman listesi ve indirme kapandı. O günkü
`belge` hız sınırı kişi başına 200 **çağrı**/gün idi — ama tek `documents/` çağrısı EKAP'a
**2 istek** atıyor. Kota bu yüzden çağrıyı değil **EKAP'a giden gerçek isteği** sayar;
önbellekten dönen cevap hiç sayılmaz.

Seviyeler (kullanıcı kararı, `EKAP_KIMLIK_KOTA` env JSON ile ezilir):

| seviye | gün | saat |
|---|---|---|
| anonim (girişsiz, web önbellek dolumu dahil) | 0 | 0 |
| yeni (hesap < 24 sa) | 10 | 5 |
| free | 30 | 15 |
| pro | 60 | 30 |

⚠️ Pro da sınırlıdır (ürün kararı): ortak bütçe 600 iken sınırsız bir hesap yine
herkesi kilitleyebilirdi. Superuser muaftır (destek/teşhis).
⚠️ Sayaç ortak bütçeyle AYNI yerde düşer (`client._ham_istek`, pencere alındıktan sonra) ve
ağ hatası tekrarında yeniden düşmez — aynı mantıksal istektir.
⚠️ Redis hatası isteği düşürmez (koruma API'yi kırmamalı); ortak bütçe yine korur.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

VARSAYILAN_KOTA = {
    "anonim": {"gun": 0, "saat": 0},
    "yeni": {"gun": 10, "saat": 5},
    "free": {"gun": 30, "saat": 15},
    "pro": {"gun": 60, "saat": 30},
}
YENI_HESAP_SAAT = 24
_PREFIX = "ekap:kota:"

MESAJ_ANONIM = "Bu bilgiyi görüntülemek için giriş yapmanız gerekiyor."
MESAJ_DOLDU = (
    "Bugünkü EKAP sorgu hakkınız doldu. Daha önce açılmış kayıtlar görüntülenmeye devam "
    "eder; yeni sorgular için lütfen yarın tekrar deneyin."
)
MESAJ_SAAT = (
    "Kısa sürede çok fazla EKAP sorgusu yapıldı. Lütfen bir süre sonra tekrar deneyin."
)
HATA_KODU = "ekap_quota"


def tablo() -> dict:
    t = {k: dict(v) for k, v in VARSAYILAN_KOTA.items()}
    for k, v in (getattr(settings, "EKAP_KIMLIK_KOTA", None) or {}).items():
        if k in t and isinstance(v, dict):
            t[k].update({a: int(b) for a, b in v.items() if a in ("gun", "saat")})
    return t


def _anahtar(kimlik: str, tur: str, an=None) -> str:
    an = timezone.localtime(an or timezone.now())
    dilim = an.date().isoformat() if tur == "g" else an.strftime("%Y-%m-%dT%H")
    return f"{_PREFIX}{kimlik}:{tur}:{dilim}"


@dataclass
class Kota:
    """Bir isteğin kota kimliği. `kimlik` hız sınırı kimliğiyle aynı biçimdedir."""

    kimlik: str
    seviye: str
    gun: int
    saat: int

    def kullanilan(self) -> tuple[int, int]:
        try:
            return (int(cache.get(_anahtar(self.kimlik, "g")) or 0),
                    int(cache.get(_anahtar(self.kimlik, "s")) or 0))
        except Exception:                                   # noqa: BLE001
            return 0, 0

    def mesaj(self) -> str:
        if self.gun <= 0:
            return MESAJ_ANONIM if self.seviye == "anonim" else MESAJ_DOLDU
        g, s = self.kullanilan()
        return MESAJ_DOLDU if g >= self.gun else MESAJ_SAAT

    def on_kontrol(self) -> bool:
        """İstek atılmadan ucuz kontrol: hak kaldı mı?"""
        if self.gun <= 0 or self.saat <= 0:
            return False
        g, s = self.kullanilan()
        return g < self.gun and s < self.saat

    def harca(self) -> bool:
        """Atomik düşüm. Tavan aşılırsa geri alınır ve `False` döner."""
        if self.gun <= 0 or self.saat <= 0:
            return False
        try:
            gk, sk = _anahtar(self.kimlik, "g"), _anahtar(self.kimlik, "s")
            cache.add(gk, 0, timeout=36 * 3600)
            cache.add(sk, 0, timeout=2 * 3600)
            g = cache.incr(gk)
            s = cache.incr(sk)
        except Exception:                                   # noqa: BLE001
            return True
        if g > self.gun or s > self.saat:
            self.iade()
            return False
        return True

    def iade(self) -> None:
        """Ortak bütçe dolduğu için atılmayan istek kişinin hakkından düşmesin."""
        for tur in ("g", "s"):
            try:
                cache.decr(_anahtar(self.kimlik, tur))
            except Exception:                               # noqa: BLE001
                pass


def _kullanici(uid):
    from django.contrib.auth import get_user_model

    if not uid:
        return None
    return get_user_model().objects.filter(pk=uid).only(
        "id", "is_superuser", "date_joined", "subscription_tier",
        "subscription_expires_at").first()


def seviye_bul(user) -> str | None:
    """`None` = muaf (superuser)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return "anonim"
    if user.is_superuser:
        return None
    if getattr(user, "is_premium", False):
        return "pro"
    katilim = getattr(user, "date_joined", None)
    if katilim and timezone.now() - katilim < timedelta(hours=YENI_HESAP_SAAT):
        return "yeni"
    return "free"


def hazirla(ist, user=None) -> Kota | None:
    """
    Hız sınırı kimliğinden (`core.throttling.istemci`) kota nesnesi.

    `user` verilmezse `ist.uid`'den okunur (belge indirmesi başlıksız, imzalı gelir).
    Dolum kimliği (kullanıcısız web önbelleği) anonimdir.
    """
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None   # AnonymousUser → imzadaki kullanıcıya bak
    if user is None and getattr(ist, "uid", None):
        user = _kullanici(ist.uid)
    sev = "anonim" if getattr(ist, "dolum", False) else seviye_bul(user)
    if sev is None:
        return None
    t = tablo()[sev]
    return Kota(kimlik=ist.kimlik, seviye=sev, gun=t["gun"], saat=t["saat"])


def ozet(kimlik: str) -> tuple[int, int]:
    """Admin paneli için bugünkü / bu saatteki kullanım."""
    try:
        return (int(cache.get(_anahtar(kimlik, "g")) or 0),
                int(cache.get(_anahtar(kimlik, "s")) or 0))
    except Exception:                                       # noqa: BLE001
        return 0, 0
