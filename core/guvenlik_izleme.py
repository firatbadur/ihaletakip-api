"""
İstek izleme — yönetim panelinin "bugün en yoğun kimlikler" tablosu (`/admin/guvenlik/`).

Her API isteğinde (`KapsamliThrottle.allow_request`, DRF kimlik doğrulamasından sonra) tek
Redis pipeline:
- `guv:iz:{gün}` ZSET: kimlik → istek sayısı,
- `guv:iz:{gün}:429` ZSET: kimlik → reddedilen istek,
- `guv:izm:{gün}:{kimlik}` HASH: son IP, son UA, istemci türü, son zaman,
- girişli istekte `guv:hip:{uid}` / `guv:hua:{uid}` ZSET: son 1 saatte görülen farklı IP /
  tarayıcı (üye → son görülme), hesap paylaşımı ya da dönen vekil işareti (web 8. not #28).
TTL 3 gün (paylaşım ZSET'leri 2 sa).
Ayrıca `guv:dolum:{tür}:{dakika}`: web önbellek dolumunun dakikalık sayacı (#27, 2 sa). ⚠️ Her isteğe DB yazımı YOKTUR — kalıcı kanıt `GuvenlikOlayi`'dır.
⚠️ Redis değilse (yerel geliştirme/test locmem) no-op; hata isteği asla düşürmez.
"""
from __future__ import annotations

import hashlib
import logging
import time

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

_TTL = 3 * 24 * 3600
_PAYLASIM_PENCERE = 3600
_DOLUM_TTL = 2 * 3600
# Panelin "önbellek dolumu" kartındaki sıra.
DOLUM_TURLERI = ("liste", "ikn_bulunan", "ikn_bos", "firma_liste", "detay", "red")


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
            if ist.uid and not ist.dolum:
                simdi = time.time()
                for ad, uye in (("hip", ist.ip),
                                ("hua", hashlib.sha1((ist.ua or "").encode()).hexdigest()[:12]
                                 if ist.ua else "")):
                    if not uye:
                        continue
                    k = cache.make_key(f"guv:{ad}:{ist.uid}")
                    p.zadd(k, {uye: simdi})
                    p.zremrangebyscore(k, 0, simdi - _PAYLASIM_PENCERE)
                    p.expire(k, 2 * _PAYLASIM_PENCERE)
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
            satir = {"kimlik": kimlik, "istek": int(sayi), "red": int(red), **meta}
            if kimlik.startswith("u:") and kimlik[2:].isdigit():
                alt = time.time() - _PAYLASIM_PENCERE
                satir["ip_1s"] = int(r.zcount(cache.make_key(f"guv:hip:{kimlik[2:]}"), alt, "+inf"))
                satir["ua_1s"] = int(r.zcount(cache.make_key(f"guv:hua:{kimlik[2:]}"), alt, "+inf"))
            out.append(satir)
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


def dolum_say(tur: str) -> None:
    """Web önbellek dolumunun dakikalık sayacı (panel grafiği). Hata isteği düşürmez."""
    r = _redis()
    if r is None:
        return
    try:
        k = cache.make_key(f"guv:dolum:{tur}:{int(time.time() // 60)}")
        p = r.pipeline(transaction=False)
        p.incr(k)
        p.expire(k, _DOLUM_TTL)
        p.execute()
    except Exception:                                       # noqa: BLE001
        logger.debug("dolum sayacı yazılamadı", exc_info=True)


def dolum_seri(dakika: int = 60) -> dict:
    """{tür: [eski → yeni dakikalık sayılar]} — son `dakika` dakika, bu dakika dahil."""
    r = _redis()
    if r is None:
        return {}
    try:
        simdi = int(time.time() // 60)
        dilimler = range(simdi - dakika + 1, simdi + 1)
        out = {}
        for tur in DOLUM_TURLERI:
            degerler = r.mget([cache.make_key(f"guv:dolum:{tur}:{d}") for d in dilimler])
            out[tur] = [int(v or 0) for v in degerler]
        return out
    except Exception:                                       # noqa: BLE001
        logger.warning("dolum sayaçları okunamadı", exc_info=True)
        return {}
