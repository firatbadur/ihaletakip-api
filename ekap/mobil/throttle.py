"""
EKAP Mobil hız sınırlaması + günlük bütçe.

⚠️ **v2 throttle'ından AYRI olmak zorunda** (`ekap/throttle.py`): oradaki bütçe
~1 istek/**saniye**, buradaki ~1 istek/**2-3 dakika**. Aynı slot anahtarını
paylaşsalardı iki toplayıcı birbirinin penceresini yerdi.

Ölçülen davranış (docs/ekap-mobil-api.md): sınır IP tabanlı ve **yuvarlanan pencere**
gibi; 2,4 istek/dk sürdürülen tempoda 12/12 istek engellendi, 30 dk sessizlikten sonra
açıldı. Bu yüzden varsayılan aralık dakikalar mertebesinde ve üstüne **sapma** eklenir
— sabit aralık da bir bot imzasıdır.

⚠️ Rezervasyon `cache.add` (Redis SETNX) ile **atomiktir**; birden çok süreç aynı
pencereyi alamaz. `ekap/throttle.py`'deki "hep GELECEKTEKİ pencere" kuralı burada da
geçerli: aksi hâlde pencere sonu + sonraki pencere başı neredeyse aynı ana denk gelir.
"""
import logging
import random
import threading
import time

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

_SLOT_PREFIX = "ekap:mobil:slot:"
_BUTCE_PREFIX = "ekap:mobil:butce:"

_YEREL_KILIT = threading.Lock()
_SON_CAGRI = {"t": 0.0}


def _aralik() -> float:
    return getattr(settings, "EKAP_MOBIL_MIN_INTERVAL_MS", 150000) / 1000.0


def slot_al(*, bekle: bool = False) -> bool:
    """
    Bir sonraki mobil isteğin zaman penceresini atomik olarak alır.

    `bekle=False` (varsayılan) → pencere doluysa **hemen `False`** döner. Çekmeli
    (pull) tik görevi bunu kullanır: 150 sn boyunca worker'ı uyutmak, tek concurrency'li
    `ekap_mobil` kuyruğunu tümüyle bloke ederdi.
    `bekle=True` → pencerenin başına kadar uyur (elle komutlar için).
    """
    aralik = _aralik()
    if aralik <= 0:
        return True
    ttl = max(2, int(aralik * 3))
    try:
        slot = int(time.time() / aralik) + 1
        if not cache.add(f"{_SLOT_PREFIX}{slot}", 1, timeout=ttl):
            if not bekle:
                return False
            # Sıradaki boş pencereyi ara (uzun beklemede bile sonlu).
            for _ in range(10):
                slot += 1
                if cache.add(f"{_SLOT_PREFIX}{slot}", 1, timeout=ttl):
                    break
            else:
                return False
        kalan = slot * aralik - time.time()
        if kalan > 0:
            time.sleep(kalan)
        _sapma()
        return True
    except Exception:                                   # noqa: BLE001
        return _yerel_bekle(aralik, bekle)


def _sapma():
    """Sabit kadansı kırmak için küçük rastgele gecikme (bot imzası azaltma)."""
    oran = getattr(settings, "EKAP_MOBIL_SAPMA_ORANI", 0.25)
    if oran <= 0:
        return
    time.sleep(random.uniform(0, _aralik() * oran))


def _yerel_bekle(aralik: float, bekle: bool) -> bool:
    """Redis yokken tek süreç kilidi (yerel geliştirme)."""
    with _YEREL_KILIT:
        gecen = time.time() - _SON_CAGRI["t"]
        if gecen < aralik:
            if not bekle:
                return False
            time.sleep(aralik - gecen)
        _SON_CAGRI["t"] = time.time()
    return True


# ── Günlük bütçe ───────────────────────────────────────
def _butce_anahtar(ad: str) -> str:
    return f"{_BUTCE_PREFIX}{ad}:{timezone.localdate().isoformat()}"


def butce_kalan(ad: str = "arka_plan") -> int:
    """Bugün kalan istek hakkı. ⚠️ Tavan `<=0` ise sınırsız (çok büyük sayı döner)."""
    tavan = _tavan(ad)
    if tavan <= 0:
        return 10 ** 9
    try:
        kullanilan = int(cache.get(_butce_anahtar(ad)) or 0)
    except Exception:                                   # noqa: BLE001
        return tavan
    return max(0, tavan - kullanilan)


def _tavan(ad: str) -> int:
    if ad == "kullanici":
        return getattr(settings, "EKAP_MOBIL_KULLANICI_REZERV", 100)
    return getattr(settings, "EKAP_MOBIL_GUNLUK_TAVAN", 600)


def butce_harca(ad: str = "arka_plan", adet: int = 1) -> bool:
    """
    Günlük bütçeden düşer; tavan aşılacaksa `False` döner ve **istek atılmaz**.

    ⚠️ Amaç bir hata döngüsünün günlük hakkı bir saatte yakmasını engellemek:
    engellenmiş bir IP'ye ısrarla istek atmak cezayı büyütür (ölçülen soğuma
    4 dk ile 30 dk arasında).
    """
    tavan = _tavan(ad)
    if tavan <= 0:
        return True
    anahtar = _butce_anahtar(ad)
    try:
        # `add` + `incr`: gün başında anahtar yoksa kur, sonra atomik artır.
        cache.add(anahtar, 0, timeout=36 * 3600)
        yeni = cache.incr(anahtar, adet)
    except Exception:                                   # noqa: BLE001
        return True  # sayaç çalışmıyorsa işi durdurma; throttle zaten koruyor
    if yeni > tavan:
        logger.warning("EKAP mobil günlük bütçe doldu (%s: %s/%s)", ad, yeni, tavan)
        return False
    return True


def butce_ozet() -> dict:
    """Pano/sağlık raporu için: {ad: {kullanilan, tavan}}."""
    out = {}
    for ad in ("arka_plan", "kullanici"):
        tavan = _tavan(ad)
        try:
            kullanilan = int(cache.get(_butce_anahtar(ad)) or 0)
        except Exception:                               # noqa: BLE001
            kullanilan = 0
        out[ad] = {"kullanilan": kullanilan, "tavan": tavan}
    return out
