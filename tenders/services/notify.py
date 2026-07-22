"""
Bildirim dağıtıcı + pacing limitleyici.

Eski `ihaletakip-scheduler` servisinin `app/notifications/dispatcher.py` mantığının
Postgres karşılığı. İki sorumluluk **ayrıştırılmıştır**:

- `record_notification(...)` → yalnızca uygulama-içi `Notification` satırı (push YOK).
  Her olay için çağrılır; kullanıcı bildirim listesinde her şeyi görür.
- `push_to_user(...)` → pacing kapılarından geçen **tek** FCM push. Kategori/kullanıcı
  başına bir kez çağrılır → kullanıcı bombardımana tutulmaz.

Pacing kapıları (sırayla): fcm_token var mı → kullanıcı aktif mi → tercih push açık mı →
sessiz saat → idempotency → günlük limit → minimum aralık. Herhangi biri engellerse push
atılmaz ama uygulama-içi satır zaten yazılmıştır.
"""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

# Cache anahtar önekleri
_CAP_PREFIX = "notif:pushcount:"   # notif:pushcount:{uid}:{date}
_LAST_PREFIX = "notif:pushlast:"   # notif:pushlast:{uid}
_IDEM_TTL = 7 * 24 * 3600          # idempotency anahtarı 7 gün
_CAP_TTL = 36 * 3600              # günlük sayaç ~1.5 gün
_LAST_TTL = 24 * 3600


def record_notification(
    user,
    *,
    type: str,
    title: str,
    body: str = "",
    tender_id: str | None = None,
    tender_ikn: str | None = None,
    tender_title: str | None = None,
    institution: str | None = None,
    conversation_id: int | None = None,
    filter_id: int | None = None,
):
    """Uygulama-içi bildirim satırı oluşturur (push göndermez). Notification döner."""
    from tenders.models import Notification

    return Notification.objects.create(
        user=user,
        type=type,
        title=title[:255],
        body=body or "",
        tender_id=tender_id,
        tender_ikn=tender_ikn,
        tender_title=(tender_title[:500] if tender_title else None),
        institution=(institution[:500] if institution else None),
        conversation_id=conversation_id,
        filter_id=filter_id,
    )


def _push_pref_enabled(user) -> bool:
    """Kullanıcı push tercihini açık bırakmış mı? (varsayılan açık — opt-out)"""
    prefs = getattr(user, "preferences", None)
    if not isinstance(prefs, dict):
        return True
    notif = prefs.get("notifications")
    if not isinstance(notif, dict):
        return True
    return bool(notif.get("push", True))


def _in_quiet_hours(now=None) -> bool:
    """Şu an (yerel saat) sessiz saat aralığında mı?"""
    start = int(getattr(settings, "NOTIF_QUIET_START_HOUR", 22))
    end = int(getattr(settings, "NOTIF_QUIET_END_HOUR", 7))
    if start == end:
        return False  # aralık yok
    hour = timezone.localtime(now).hour
    if start < end:
        # gün içi aralık (ör. 1..6)
        return start <= hour < end
    # gece yarısını saran aralık (ör. 22..7)
    return hour >= start or hour < end


def push_to_user(
    user,
    *,
    title: str,
    body: str = "",
    data: dict[str, Any] | None = None,
    idem_key: str | None = None,
) -> bool:
    """
    Pacing kapılarından geçerse kullanıcıya tek FCM push atar. Gerçekten gönderildiyse
    True döner. Ölü token → `user.fcm_token` temizlenir.
    """
    from . import push as push_mod

    token = (getattr(user, "fcm_token", "") or "").strip()
    if not token:
        return False
    if not getattr(user, "is_active", True):
        return False
    if not _push_pref_enabled(user):
        logger.debug("push atlandı (tercih kapalı) uid=%s", user.pk)
        return False
    if _in_quiet_hours():
        logger.info("push atlandı (sessiz saat) uid=%s", user.pk)
        return False

    # İdempotency: aynı mantıksal bildirim daha önce GÖNDERİLDİYSE atla.
    if idem_key and cache.get(idem_key) is not None:
        logger.debug("push atlandı (idempotent) key=%s", idem_key)
        return False

    today = timezone.localdate().isoformat()
    cap = int(getattr(settings, "NOTIF_DAILY_CAP", 4))
    cap_key = f"{_CAP_PREFIX}{user.pk}:{today}"
    sent_today = cache.get(cap_key, 0)
    if sent_today >= cap:
        logger.info("push atlandı (günlük limit %s) uid=%s", cap, user.pk)
        return False

    # Minimum aralık
    gap_min = int(getattr(settings, "NOTIF_MIN_GAP_MINUTES", 30))
    if gap_min > 0:
        last_ts = cache.get(f"{_LAST_PREFIX}{user.pk}")
        now_ts = timezone.now().timestamp()
        if last_ts and (now_ts - float(last_ts)) < gap_min * 60:
            logger.info("push atlandı (min aralık %sdk) uid=%s", gap_min, user.pk)
            return False

    status = push_mod.send_fcm(token, title, body, data)

    if status == push_mod.INVALID_TOKEN:
        user.fcm_token = ""
        try:
            user.save(update_fields=["fcm_token"])
        except Exception:
            logger.exception("ölü fcm_token temizlenemedi uid=%s", user.pk)
        return False

    if status != push_mod.SENT:
        # DISABLED (kimlik yok) veya ERROR → sayaç/idem güncellenmez, satır zaten yazıldı.
        return False

    # Başarılı → sayaç, son-zaman ve idem güncelle.
    if idem_key:
        cache.set(idem_key, 1, _IDEM_TTL)
    cache.set(f"{_LAST_PREFIX}{user.pk}", timezone.now().timestamp(), _LAST_TTL)
    cache.set(cap_key, int(sent_today) + 1, _CAP_TTL)
    return True


def notify_and_push(
    user,
    *,
    type: str,
    title: str,
    body: str = "",
    tender_id: str | None = None,
    tender_ikn: str | None = None,
    tender_title: str | None = None,
    institution: str | None = None,
    conversation_id: int | None = None,
    filter_id: int | None = None,
    idem_key: str | None = None,
    data: dict[str, Any] | None = None,
) -> bool:
    """Tek-olay kısayolu: uygulama-içi satır oluşturur + tek push atar. push sonucu döner."""
    record_notification(
        user,
        type=type,
        title=title,
        body=body,
        tender_id=tender_id,
        tender_ikn=tender_ikn,
        tender_title=tender_title,
        institution=institution,
        conversation_id=conversation_id,
        filter_id=filter_id,
    )

    payload = {
        "type": type,
        "tenderId": tender_id,
        "tenderIkn": tender_ikn,
        "tenderTitle": tender_title,
        "institution": institution,
        "conversationId": conversation_id,
        "filterId": filter_id,
    }
    if data:
        payload.update(data)

    return push_to_user(user, title=title, body=body, data=payload, idem_key=idem_key)
