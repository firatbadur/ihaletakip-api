"""
Free sayfa sınırı — Pro olmayan kullanıcı listelerde en çok `LISTE_FREE_AZAMI_SAYFA` (3) sayfa.

Ürün kararı (2026-11-05): ihale, doğrudan temin, firma ve kurul kararı listeleri; **bildirimden
açılan listeler dahil**. Sayfa ≥ 4 → 403 `premium_required` + `errors.sebep="page_limit"` +
`errors.azami_sayfa`. Pro olmayanda sayfa boyutu `LISTE_FREE_AZAMI_SAYFA_BOYUTU` (20) ile
kırpılır — aksi hâlde `page_size=100` ile 3 sayfa 300 kayıt ederdi.

Kim için uygulanır (mod kapali | gozlem | uygula):
- Web portalı (girişli/anonim) → `LISTE_SAYFA_SINIRI_PORTAL` (vars. uygula).
- Web önbellek dolumu (kullanıcısız) → `LISTE_SAYFA_SINIRI_DOLUM` (vars. **kapalı**): dolum
  isteği kimin için yapıldığını bilmez; açık olsaydı web'in Pro kullanıcısı da kesilirdi.
  Web derin sayfaları token'la istemeye başlayınca (web notu #21) açılır.
- Mobil/doğrudan → `X-IT-App-Build ≥ LISTE_SAYFA_SINIRI_MOBIL_MIN_BUILD` ise uygula, değilse
  `LISTE_SAYFA_SINIRI_DIGER` (vars. gözlem). ⚠️ Mağazadaki eski sürüm 4. sayfadaki 403'te
  listenin tamamını hata ekranına düşürüyor → yeni sürüm yayılmadan uygulanmaz.

⚠️ Sınır **kayıt** değil **sayfa** sayar (kullanıcının gördüğü "3 sayfa"): mobil 10'luk, web
20'lik sayfa kullanıyor.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger("ihaletakip")

_MODLAR = ("kapali", "gozlem", "uygula")


def _build(request) -> int:
    try:
        return int(str(request.META.get("HTTP_X_IT_APP_BUILD", "")).strip() or 0)
    except ValueError:
        return 0


def mod(request, ist) -> str:
    if ist.bot:
        return "kapali"
    if ist.dolum:
        m = getattr(settings, "LISTE_SAYFA_SINIRI_DOLUM", "kapali")
    elif ist.portal:
        m = getattr(settings, "LISTE_SAYFA_SINIRI_PORTAL", "uygula")
    else:
        asgari = getattr(settings, "LISTE_SAYFA_SINIRI_MOBIL_MIN_BUILD", 0) or 0
        if asgari > 0 and _build(request) >= asgari:
            m = "uygula"
        else:
            m = getattr(settings, "LISTE_SAYFA_SINIRI_DIGER", "gozlem")
    return m if m in _MODLAR else "gozlem"


def pro_sayfa_kontrolu(request, page: int, page_size: int, kapsam: str) -> int:
    """
    Sınırı uygular; (gerekirse kırpılmış) `page_size` döner.

    Pro / superuser hiç etkilenmez. Hata yalnızca `uygula` modunda fırlar; kontrolün
    kendi hatası isteği düşürmez.
    """
    from accounts.premium import MSG_SAYFA, PremiumRequired

    from .throttling import gozlem_kaydet, istemci

    user = getattr(request, "user", None)
    if getattr(user, "is_premium", False):
        return page_size
    try:
        ist = istemci(request)
        m = mod(request, ist)
    except Exception:                                       # noqa: BLE001
        logger.warning("sayfa sınırı kontrolü hatası (istek geçirildi)", exc_info=True)
        return page_size
    if m == "kapali":
        return page_size
    azami = max(1, getattr(settings, "LISTE_FREE_AZAMI_SAYFA", 3))
    if page <= azami:
        if m == "uygula":
            return min(page_size, max(1, getattr(settings, "LISTE_FREE_AZAMI_SAYFA_BOYUTU", 20)))
        return page_size
    if m == "gozlem":
        gozlem_kaydet("sayfa_pro", kapsam, ist)
        return page_size
    from .guvenlik import olay_yaz

    olay_yaz("sayfa_siniri", ist=ist, request=request,
             ayrinti={"kapsam": kapsam, "sayfa": page})
    raise PremiumRequired(MSG_SAYFA, ek={"sebep": "page_limit", "azami_sayfa": azami})
