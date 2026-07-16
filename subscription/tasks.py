"""
subscription Celery görevleri.

Webhook hızlıca 200 döner ve senkronu buraya atar (RC API çağrısı istek yolunu
bloklamaz). Görev RC'yi sorgular; ulaşılamazsa event verisine (yedek) düşer.
"""
import logging

from celery import shared_task

logger = logging.getLogger("ihaletakip")


@shared_task(name="subscription.tasks.sync_subscription_task")
def sync_subscription_task(user_id, event=None):
    """Kullanıcının aboneliğini RC'den senkronlar; RC hatasında event yedeğine düşer."""
    from accounts.models import User
    from .services.revenuecat import (
        RevenueCatError,
        apply_event_fallback,
        sync_user_subscription,
    )

    user = User.objects.filter(pk=user_id).first()
    if not user:
        logger.warning("sync_subscription_task: kullanıcı yok uid=%s", user_id)
        return {"ok": False, "reason": "user_not_found"}

    try:
        sync_user_subscription(user)
        return {"ok": True, "tier": user.subscription_tier}
    except RevenueCatError:
        logger.exception("sync_subscription_task: RC sorgusu başarısız, event yedeği uid=%s", user_id)
        if event:
            try:
                apply_event_fallback(user, event)
            except Exception:
                logger.exception("sync_subscription_task: event yedeği de başarısız uid=%s", user_id)
        return {"ok": False, "tier": user.subscription_tier}
