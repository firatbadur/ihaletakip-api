"""
RevenueCat v2 REST entegrasyonu.

Sözleşme: `app_user_id = str(django_user.id)`. Backend kullanıcıyı JWT'den (veya
webhook event'inden) bilir → RC'yi bu id ile sorgular ve `accounts.User`'ın abonelik
katmanını (`subscription_tier` / `subscription_expires_at`) senkronlar.

Tek doğruluk kaynağı: RC v2 `active_entitlements`. Katmanı buradan set ederiz; kullanıcının
premium olup olmadığı `User.is_premium` üzerinden okunur (expiry hesabı orada). Webhook
event tipiyle elle işlem YAPMAYIZ — event'i yalnızca hangi kullanıcı olduğunu bulmak için
kullanırız, sonra `active_entitlements`'i tekrar sorgularız (tek kod yolu, her zaman doğru).
RC API'ye ulaşılamazsa `apply_event_fallback` ile event verisinden kaba senkron yapılır.

`requests` kullanılır (EKAP'ın aksine RC'de TLS parmak izi engeli yok).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

_BASE = "https://api.revenuecat.com/v2"
_TIMEOUT = 15

# Webhook event tipleri (yedek yol için). RC API tercih edilir; bunlar yalnızca
# RC sorgusu başarısız olursa event verisinden kaba senkron yapmak içindir.
_GRANT_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "NON_RENEWING_PURCHASE",
    "SUBSCRIPTION_EXTENDED",
    "PRODUCT_CHANGE",
    "TEMPORARY_ENTITLEMENT_GRANT",
    "TRANSFER",
}


class RevenueCatError(Exception):
    """RevenueCat isteği başarısız (kimlik yok, ağ hatası, 5xx...)."""


def _entitlement_key() -> str:
    return getattr(settings, "REVENUECAT_ENTITLEMENT", "pro") or "pro"


def _request(path: str):
    """RC v2 `projects/{proj}/{path}` GET. `path` proje köküne görelidir. 404 → None."""
    import requests

    key = getattr(settings, "REVENUECAT_SECRET_KEY", "")
    project = getattr(settings, "REVENUECAT_PROJECT_ID", "")
    if not key or not project:
        raise RevenueCatError("REVENUECAT_SECRET_KEY / REVENUECAT_PROJECT_ID tanımlı değil.")

    url = f"{_BASE}/projects/{project}/{path}"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as e:  # ağ/timeout
        raise RevenueCatError(f"RevenueCat isteği başarısız: {e}") from e

    if resp.status_code == 404:
        return None  # kaynak yok (ör. müşteri hiç satın alma yapmamış) → free
    if resp.status_code >= 400:
        raise RevenueCatError(f"RevenueCat HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise RevenueCatError(f"RevenueCat yanıtı JSON değil: {e}") from e


def _get(customer_id: str, resource: str):
    """Müşteri-kapsamlı kısayol: `customers/{id}/{resource}`."""
    return _request(f"customers/{customer_id}/{resource}")


def _entitlement_id_map(refresh: bool = False) -> dict:
    """
    RC iç entitlement kimliği (`entl...`) → `lookup_key` haritası.

    KRİTİK: v2 `active_entitlements` her item'da yalnızca iç `entitlement_id` döndürür —
    `lookup_key` YOKTUR. "pro" ile eşleştirmek için proje entitlement listesinden bu
    haritayı kurarız. Nadiren değiştiği için 1 saat cache'lenir (`refresh=True` atlar).
    """
    from django.core.cache import cache

    ckey = "rc:entitlement_map"
    if not refresh:
        cached = cache.get(ckey)
        if cached is not None:
            return cached
    data = _request("entitlements")
    mapping = {
        it["id"]: (it.get("lookup_key") or "")
        for it in (data or {}).get("items", []) or []
        if it.get("id")
    }
    cache.set(ckey, mapping, 3600)
    return mapping


def _parse_rc_ts(value):
    """RC zaman damgası → aware datetime. RC v2 ms-epoch (int) döner; ISO string de tolere edilir."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=dt_timezone.utc)
    if isinstance(value, str):
        if value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000, tz=dt_timezone.utc)
        from django.utils.dateparse import parse_datetime

        dt = parse_datetime(value)
        if dt and timezone.is_naive(dt):
            dt = timezone.make_aware(dt, dt_timezone.utc)
        return dt
    return None


def _latest_subscription_end(customer_id: str):
    """`subscriptions` içindeki en ileri `current_period_ends_at` (Pro bitişi tahmini)."""
    data = _get(customer_id, "subscriptions")
    items = (data or {}).get("items", []) or []
    best = None
    for sub in items:
        ends = _parse_rc_ts(sub.get("current_period_ends_at"))
        if ends and (best is None or ends > best):
            best = ends
    return best


def resolve_pro_status(customer_id: str) -> tuple[bool, datetime | None]:
    """
    RC v2 `active_entitlements`'i sorgular.

    Dönen: (is_pro, expiry). v2 `active_entitlements` her item'da yalnızca iç
    `entitlement_id` (`entl...`) döndürür — `lookup_key` YOKTUR. Bu yüzden iç kimliği
    `_entitlement_id_map` ile `lookup_key`'e çevirip entitlement (vars. "pro") aktif mi
    diye bakarız. Expiry önce entitlement'ın `expires_at`'inden, yoksa
    `subscriptions.current_period_ends_at`'ten alınır (yoksa None = süresiz).
    """
    ent_key = _entitlement_key()
    data = _get(customer_id, "active_entitlements")
    items = (data or {}).get("items", []) or []
    if not items:
        return False, None

    id_map = _entitlement_id_map()
    # Aktif bir entitlement_id haritada yoksa harita bayat olabilir → bir kez tazele.
    if any(it.get("entitlement_id") not in id_map for it in items):
        id_map = _entitlement_id_map(refresh=True)

    is_pro = False
    expiry = None
    for it in items:
        eid = it.get("entitlement_id")
        # id_map'ten lookup_key; bazı sürümler doğrudan lookup_key gömerse ona da bak.
        lookup = id_map.get(eid) or it.get("lookup_key")
        if lookup != ent_key:
            continue
        is_pro = True
        ts = _parse_rc_ts(it.get("expires_at"))
        if ts and (expiry is None or ts > expiry):
            expiry = ts

    if not is_pro:
        return False, None
    if expiry is None:
        try:
            expiry = _latest_subscription_end(customer_id)
        except RevenueCatError:
            logger.warning("resolve_pro_status: subscriptions sorgusu başarısız cust=%s", customer_id)
            expiry = None
    return True, expiry


def _apply(user, is_pro: bool, expiry):
    """Kullanıcının katmanını set eder (yalnızca değiştiyse yazar). Değişiklik döner."""
    from accounts.models import User

    tier = User.Tier.PRO if is_pro else User.Tier.FREE
    new_expiry = expiry if is_pro else None
    if user.subscription_tier == tier and user.subscription_expires_at == new_expiry:
        return False
    user.subscription_tier = tier
    user.subscription_expires_at = new_expiry
    user.save(update_fields=["subscription_tier", "subscription_expires_at"])
    logger.info("abonelik senkron: uid=%s tier=%s expiry=%s", user.pk, tier, new_expiry)
    return True


def sync_user_subscription(user):
    """
    RC'yi sorgular ve kullanıcının katmanını günceller. `app_user_id = str(user.id)`.
    RC hatasında RevenueCatError fırlatır (çağıran karar verir — DB'ye dokunulmaz).
    """
    is_pro, expiry = resolve_pro_status(str(user.id))
    _apply(user, is_pro, expiry)
    return user


def apply_event_fallback(user, event: dict) -> None:
    """
    RC API'ye ulaşılamazsa webhook event verisinden kaba senkron (YEDEK yol).

    `expiration_at_ms` gelecekteyse ve event Pro entitlement'ını ilgilendiriyorsa → Pro;
    EXPIRATION ya da geçmiş expiry → Free. CANCELLATION (otomatik yenileme kapandı ama
    süre sonuna kadar erişim var) → expiry gelecekteyse Pro kalır. Belirsiz event → dokunma.
    """
    ent_key = _entitlement_key()
    ent_ids = event.get("entitlement_ids") or []
    if ent_ids and ent_key not in ent_ids:
        return  # bu event Pro entitlement'ını ilgilendirmiyor

    etype = (event.get("type") or "").upper()
    exp = _parse_rc_ts(event.get("expiration_at_ms"))
    now = timezone.now()

    if etype == "EXPIRATION" or (exp is not None and exp <= now):
        _apply(user, False, None)
    elif etype in _GRANT_EVENTS or etype == "CANCELLATION":
        _apply(user, True, exp)
    else:
        logger.info("apply_event_fallback: belirsiz event '%s' uid=%s → dokunulmadı", etype, user.pk)


def resolve_user_from_event(event: dict):
    """
    Webhook event'inden Django kullanıcısını bulur. `app_user_id` ve `aliases` içinden
    sayısal olan ilk id (= str(user.id)) eşleşir. RC anonim id'leri ($RCAnonymousID:...)
    sayısal olmadığı için atlanır.
    """
    from accounts.models import User

    candidates = []
    if event.get("app_user_id"):
        candidates.append(event["app_user_id"])
    candidates.extend(event.get("aliases") or [])

    for cand in candidates:
        try:
            uid = int(str(cand))
        except (TypeError, ValueError):
            continue
        user = User.objects.filter(pk=uid).first()
        if user:
            return user
    return None
