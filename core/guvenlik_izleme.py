"""
İstek izleme — yönetim panelinin "bugün en yoğun kimlikler" tablosu (`/admin/guvenlik/`).

Her API isteğinde (`KapsamliThrottle.allow_request`, DRF kimlik doğrulamasından sonra) tek
Redis pipeline:
- `guv:iz:{gün}` ZSET: kimlik → istek sayısı,
- `guv:iz:{gün}:429` ZSET: kimlik → reddedilen istek,
- `guv:izm:{gün}:{kimlik}` HASH: son IP, son UA, istemci türü, son zaman.
TTL 3 gün. ⚠️ Her isteğe DB yazımı YOKTUR — kalıcı kanıt `GuvenlikOlayi`'dır.
⚠️ Redis değilse (yerel geliştirme/test locmem) no-op; hata isteği asla düşürmez.
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

_TTL = 3 * 24 * 3600


def _redis():
    try:
        return cache._cache.get_client()   # django RedisCache
    except Exception:                                       # noqa: BLE001
        return None


def _anahtar(gun: str, ek: str = "") -> str:
    return cache.make_key(f"guv:iz:{gun}{ek}")


def kaydet(ist, request, *, reddedildi: bool = False) -> None:
    r = _redis()
    if r is None or ist is None:
        return
    try:
        gun = timezone.localdate().isoformat()
        kimlik = ist.kimlik
        p = r.pipeline(transaction=False)
        z = _anahtar(gun, ":429" if reddedildi else "")
        p.zincrby(z, 1, kimlik)
        p.expire(z, _TTL)
        if not reddedildi:
            m = cache.make_key(f"guv:izm:{gun}:{kimlik}")
            p.hset(m, mapping={
                "ip": ist.ip or "", "ua": (ist.ua or "")[:300], "tur": ist.tur,
                "zaman": str(int(time.time())), "yol": request.path[:200],
            })
            p.expire(m, _TTL)
        p.execute()
    except Exception:                                       # noqa: BLE001
        logger.debug("istek izleme yazılamadı", exc_info=True)


def _cozul(d: dict) -> dict:
    return {(k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v) for k, v in (d or {}).items()}


def en_yogun(gun: str | None = None, adet: int = 50) -> list[dict]:
    """[{kimlik, istek, red, ip, ua, tur, zaman}] — çok istekten aza."""
    r = _redis()
    if r is None:
        return []
    gun = gun or timezone.localdate().isoformat()
    try:
        satirlar = r.zrevrange(_anahtar(gun), 0, adet - 1, withscores=True)
        out = []
        for kimlik, sayi in satirlar:
            kimlik = kimlik.decode() if isinstance(kimlik, bytes) else kimlik
            meta = _cozul(r.hgetall(cache.make_key(f"guv:izm:{gun}:{kimlik}")))
            red = r.zscore(_anahtar(gun, ":429"), kimlik) or 0
            out.append({"kimlik": kimlik, "istek": int(sayi), "red": int(red), **meta})
        return out
    except Exception:                                       # noqa: BLE001
        logger.warning("istek izleme okunamadı", exc_info=True)
        return []


def toplamlar(gun: str | None = None) -> dict:
    r = _redis()
    if r is None:
        return {"kimlik": 0, "istek": 0, "red": 0}
    gun = gun or timezone.localdate().isoformat()
    try:
        def topla(z):
            return int(sum(s for _, s in r.zrange(z, 0, -1, withscores=True)))
        return {"kimlik": int(r.zcard(_anahtar(gun))), "istek": topla(_anahtar(gun)),
                "red": topla(_anahtar(gun, ":429"))}
    except Exception:                                       # noqa: BLE001
        return {"kimlik": 0, "istek": 0, "red": 0}
