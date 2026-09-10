"""
EKAP Mobil oturumu — F5 BIG-IP ASM çerezi (`TS015c8da3`) kavanozu.

⚠️ **Neden kalıcı oturum şart:** mobil uygulama her yanıtta gelen `Set-Cookie`'yi
taşıyor. Her istekte sıfırdan bağlanan bir istemci ASM gözünde bot imzası verir —
v2'de `curl_cffi` TLS taklidinin çözdüğü sorunun buradaki karşılığı.

Kavanoz **Redis'te** durur (`ekap:mobil:cerez`): web, worker ve beat aynı çerezi
paylaşmalı, aksi hâlde her süreç kendi oturumunu açar ve tek IP'den çok oturum
görünürüz. Redis yoksa süreç-içi sözlüğe yumuşakça düşer (yerel geliştirme).

⚠️ Çerez **sırdır** (oturumun tamamını taşır) → loga asla tam basılmaz (`maskele`).
"""
import logging
import threading

from django.core.cache import cache

logger = logging.getLogger("ihaletakip")

_CEREZ_CACHE = "ekap:mobil:cerez"
# ASM çerezleri saatler mertebesinde yaşıyor; TTL bir üst sınırdır, gerçek ömür
# EKAP'ın gönderdiği çerezin kendisindedir.
_CEREZ_TTL = 12 * 3600

# Redis yoksa (yerel/senkron) tek süreçlik yedek kavanoz.
_YEREL = {"cerezler": {}}
_KILIT = threading.Lock()

# Analitik/izleme çerezleri saklanmaz — oturuma katkısı yok, gereksiz iz tutar.
_ATILACAK = ("_ga", "_gid", "_gcl", "_hj", "_fb", "_uet", "_clck", "_clsk")


def maskele(cerezler: dict) -> str:
    """Log için güvenli özet — değerler asla basılmaz."""
    if not cerezler:
        return "(yok)"
    return f"{len(cerezler)} çerez: {', '.join(sorted(cerezler))}"


def cerezler() -> dict:
    """Kayıtlı çerez sözlüğü ({ad: değer}); yoksa boş sözlük."""
    try:
        val = cache.get(_CEREZ_CACHE)
        if isinstance(val, dict):
            return dict(val)
        return {}
    except Exception:                                   # noqa: BLE001
        with _KILIT:
            return dict(_YEREL["cerezler"])


def guncelle(yeni: dict) -> dict:
    """Yanıttan gelen çerezleri kavanoza işler (mevcutları ezer, diğerlerini korur)."""
    if not yeni:
        return cerezler()
    temiz = {
        k: v for k, v in yeni.items()
        if k and not any(k.startswith(x) for x in _ATILACAK)
    }
    if not temiz:
        return cerezler()
    mevcut = cerezler()
    mevcut.update(temiz)
    try:
        cache.set(_CEREZ_CACHE, mevcut, _CEREZ_TTL)
    except Exception:                                   # noqa: BLE001
        with _KILIT:
            _YEREL["cerezler"] = mevcut
    return mevcut


def sifirla(sebep: str = ""):
    """Kavanozu boşaltır — oturum bozulduğunda (kalıcı captcha, 4xx fırtınası)."""
    try:
        cache.delete(_CEREZ_CACHE)
    except Exception:                                   # noqa: BLE001
        pass
    with _KILIT:
        _YEREL["cerezler"] = {}
    logger.info("EKAP mobil oturumu sıfırlandı (%s)", sebep or "sebep verilmedi")
