"""
EKAP istekleri için hız sınırlama (rate limiting).

Redis tabanlı **atomik slot rezervasyonu**: zaman `EKAP_MIN_INTERVAL_MS` uzunluğunda
pencerelere bölünür ve her pencereyi yalnızca **bir** çağrı alabilir. Rezervasyon
`cache.add` (Redis `SETNX`) ile yapılır — bu tek bir atomik işlemdir, dolayısıyla
worker'lar arası yarış yoktur.

⚠️ **Neden atomik olmak zorunda:** eski sürüm `cache.get` ile son çağrı zamanını okuyup
`cache.set` ile yazıyordu. Tek concurrency'de sorun değildi, ama `ekap-worker`
concurrency > 1 olunca birden çok worker aynı değeri okuyup **hepsi birden** geçebilirdi
— EKAP'a saniyede N istek gider ve WAF engeli riski doğardı.

## Concurrency neden gerekli

EKAP yanıt süresi ölçüldü (2026-08): istek başına ~1,8 sn. Tek concurrency'de bu süre
throttle'ın 1 sn'siyle **seri** toplanıyordu → istek başına ~2,8 sn → 0,36 istek/sn, yani
1 istek/sn bütçesinin yalnızca %36'sı kullanılıyordu. Concurrency > 1 ile EKAP beklemeleri
**örtüşür**; throttle yine saniyede bir isteğe izin verdiği için **EKAP üzerindeki yük
değişmez**, yalnızca bizim boşa bekleyişimiz biter.

Redis yoksa (yerel/senkron mod) tek-süreç kilidine yumuşakça düşer.
"""
import threading
import time

from django.conf import settings
from django.core.cache import cache

_LOCAL_LOCK = threading.Lock()
_LAST_CALL = {"t": 0.0}

# Pencere anahtarı; TTL kısa tutulur (birkaç pencere yeter, Redis şişmesin).
_SLOT_PREFIX = "ekap:slot:"
# Emniyet tavanı: bu kadar pencere denendikten sonra beklemeden geçilir. Kilitlenmeyi
# önler — throttle bir güvenlik katmanıdır, işi sonsuza dek durdurmamalı.
_MAX_DENEME = 300


def _local_wait(min_interval: float) -> None:
    """Redis yokken tek-süreç kilidi (yerel geliştirme / senkron çağrılar)."""
    with _LOCAL_LOCK:
        elapsed = time.time() - _LAST_CALL["t"]
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _LAST_CALL["t"] = time.time()


def wait_for_slot() -> None:
    """Bir sonraki EKAP çağrısı için uygun zaman penceresini **atomik** olarak alır."""
    min_interval = settings.EKAP_MIN_INTERVAL_MS / 1000.0
    if min_interval <= 0:
        return

    ttl = max(2, int(min_interval * 3))
    try:
        # ⚠️ **Her zaman GELECEKTEKİ bir pencere** rezerve edilir (`+ 1`), içinde
        # bulunduğumuz pencere değil. Aksi hâlde pencerenin sonunda slot alan bir çağrı
        # ile bir sonraki pencerenin başında slot alan çağrı arasında neredeyse sıfır
        # süre kalabiliyor (ölçüldü: 0,09 sn). Gelecekteki pencereyi alıp **başlangıcına
        # kadar bekleyince** ardışık çağrılar tam olarak `min_interval` aralıklı olur.
        # Kararlı durumda verim aynıdır (1/interval); yalnızca ilk çağrı pencere
        # sınırına kadar bekler.
        slot = int(time.time() / min_interval) + 1
        for _ in range(_MAX_DENEME):
            # `cache.add` == Redis SETNX: yalnızca ilk çağıran başarılı olur.
            if cache.add(f"{_SLOT_PREFIX}{slot}", 1, timeout=ttl):
                bekle = slot * min_interval - time.time()
                if bekle > 0:
                    time.sleep(bekle)
                return
            slot += 1  # bu pencere alınmış → sıradakini dene
        # Tavan aşıldı: sıra dışı bir yoğunluk var, işi durdurmaktansa geç.
        return
    except Exception:
        _local_wait(min_interval)
