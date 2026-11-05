"""
İnsan doğrulaması (Cloudflare Turnstile) — **yalnızca web portalı**, yalnızca çok istekte.

Ürün kararı (2026-11-05): EKAP'ın kendi portalındaki gibi bir "robot değilim" kutusu; ama
herkese oturum başında DEĞİL, yalnızca **çok istek** atan kimliğe (+ web kaydında).

Akış:
1. `KapsamliThrottle` her kapsamlı portal çağrısında `kontrol()`'ü çağırır: kimlik (girişli →
   hesap, anonim → IP) başına `GUVENLIK_DOGRULAMA_PENCERE_SN` (600) sn'lik sayaç; girişli
   istekte IP'nin kendi sayacı da tutulur (aynı IP'den hesap çoğaltan kazıyıcı).
2. Eşik aşılınca **HTTP 428** `errors.code="human_verification_required"`.
3. Web Turnstile kutusunu gösterir, token'ı `POST /api/v1/guvenlik/insan-dogrulama/`'ya iletir;
   `siteverify` geçerse sayaçlar sıfırlanır → kazıyıcı her ~30 sayfada bir çözmek zorunda.

⚠️ Mobil ve doğrudan istemciler hiç etkilenmez (kutu gösteremezler); orada koruma kişi başı
EKAP kotası + hız sınırı + engel sistemidir. Doğrulanmış arama botu ve kullanıcısız web
önbellek dolumu da muaftır.
⚠️ Mod `GUVENLIK_DOGRULAMA_MODU` (kapali | gozlem | uygula); `TURNSTILE_SECRET_KEY` boşsa
`uygula` gözleme düşer — sırsız uygulamak doğrulanamayan bir kapı kurmak olurdu.
⚠️ Turnstile token'ı **tek kullanımlık** ve ~300 sn ömürlüdür; aynı token iki kez verilirse
Cloudflare `timeout-or-duplicate` döner.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.exceptions import APIException

logger = logging.getLogger("ihaletakip")

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
HATA_KODU = "human_verification_required"
MESAJ = ("Çok sayıda istek algılandı. Devam etmek için lütfen robot olmadığınızı "
         "doğrulayın.")
MESAJ_GECERSIZ = "Doğrulama tamamlanamadı. Lütfen tekrar deneyin."
_PREFIX = "guv:dg:"
VARSAYILAN_ESIK = {"anonim": 100, "girisli": 150, "ip": 400}


class DogrulamaGerekli(APIException):
    """HTTP 428 — EKAP'ın `HUMAN_VERIFICATION_REQUIRED` cevabının bizdeki karşılığı."""

    status_code = status.HTTP_428_PRECONDITION_REQUIRED
    default_detail = MESAJ
    default_code = HATA_KODU

    def __init__(self, mesaj: str = MESAJ):
        super().__init__({"code": HATA_KODU, "detail": mesaj})


def mod() -> str:
    m = getattr(settings, "GUVENLIK_DOGRULAMA_MODU", "gozlem")
    if m not in ("kapali", "gozlem", "uygula"):
        m = "gozlem"
    if m == "uygula" and not getattr(settings, "TURNSTILE_SECRET_KEY", ""):
        return "gozlem"
    return m


def _esik() -> dict:
    e = dict(VARSAYILAN_ESIK)
    e.update(getattr(settings, "GUVENLIK_DOGRULAMA_ESIK", None) or {})
    return e


def _anahtarlar(ist) -> list[tuple[str, int]]:
    esik = _esik()
    if ist.girisli:
        out = [(f"{_PREFIX}{ist.kimlik}", int(esik["girisli"]))]
        if ist.ip:
            out.append((f"{_PREFIX}ipg:{ist.ip}", int(esik["ip"])))
        return out
    return [(f"{_PREFIX}{ist.kimlik}", int(esik["anonim"]))]


def kontrol(ist, kapsam, request) -> None:
    """Eşik aşıldıysa (uygula) `DogrulamaGerekli` fırlatır; gözlemde yalnızca sayar."""
    if not ist.portal or ist.bot or ist.dolum:
        return
    m = mod()
    if m == "kapali":
        return
    from .throttling import _say, gozlem_kaydet

    pencere = max(60, int(getattr(settings, "GUVENLIK_DOGRULAMA_PENCERE_SN", 600)))
    asim = False
    for anahtar, esik in _anahtarlar(ist):
        if _say(anahtar, pencere) > esik:
            asim = True
    if not asim:
        return
    if m == "gozlem":
        gozlem_kaydet("dogrulama", kapsam, ist)
        return
    from .guvenlik import olay_yaz

    olay_yaz("dogrulama_istendi", ist=ist, request=request, ayrinti={"kapsam": kapsam})
    raise DogrulamaGerekli()


def sifirla(ist) -> None:
    for anahtar, _ in _anahtarlar(ist):
        cache.delete(anahtar)


def turnstile_dogrula(token: str, ip: str = "") -> tuple[bool, list[str]]:
    """Cloudflare `siteverify`. Dönen: (geçti mi, hata kodları)."""
    import requests

    sir = getattr(settings, "TURNSTILE_SECRET_KEY", "")
    if not sir:
        return False, ["sir-yok"]
    if not token or len(token) > 2048:
        return False, ["token-yok"]
    veri = {"secret": sir, "response": token}
    if ip:
        veri["remoteip"] = ip
    try:
        r = requests.post(SITEVERIFY_URL, data=veri, timeout=8)
        sonuc = r.json()
    except Exception as e:                                  # noqa: BLE001
        logger.warning("Turnstile siteverify erişilemedi: %s", str(e)[:150])
        return False, ["siteverify-erisilemedi"]
    if not sonuc.get("success"):
        return False, list(sonuc.get("error-codes") or ["basarisiz"])
    izinli = [h.lower() for h in (getattr(settings, "TURNSTILE_HOSTNAMES", None) or [])]
    if izinli and str(sonuc.get("hostname") or "").lower() not in izinli:
        return False, ["hostname-uyusmuyor"]
    return True, []
