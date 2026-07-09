"""
EKAP istekleri için hız sınırlama (rate limiting).

Redis tabanlı basit bir "minimum aralık" kilidi: iki EKAP çağrısı arasında en az
`EKAP_MIN_INTERVAL_MS` geçmesini garanti eder. Workerlar arası koordineli çalışır
(hepsi aynı Redis anahtarını kullanır). Redis yoksa (yerel/senkron mod) yumuşakça
tek-süreç kilidine düşer.

Not: EKAP çağrıları ayrıca ayrı Celery kuyruğunda (`-Q ekap`) tek concurrency ile
serileştirilir; bu throttle ikinci bir güvenlik katmanıdır.
"""
import time
import threading

from django.conf import settings
from django.core.cache import cache

_LOCAL_LOCK = threading.Lock()
_LAST_CALL = {"t": 0.0}
_CACHE_KEY = "ekap:last_call_ms"


def wait_for_slot():
    """Bir sonraki EKAP çağrısı için uygun zamana kadar bekler."""
    min_interval = settings.EKAP_MIN_INTERVAL_MS / 1000.0
    if min_interval <= 0:
        return

    try:
        # Redis üzerinden koordineli aralık
        now = time.time()
        last = cache.get(_CACHE_KEY)
        if last is not None:
            elapsed = now - float(last)
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
        cache.set(_CACHE_KEY, time.time(), timeout=60)
        return
    except Exception:
        # Cache yoksa tek-süreç kilidi
        with _LOCAL_LOCK:
            elapsed = time.time() - _LAST_CALL["t"]
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            _LAST_CALL["t"] = time.time()
