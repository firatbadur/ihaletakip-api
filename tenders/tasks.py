"""tenders Celery görevleri — alarm kontrolü, bildirim temizliği."""
import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("ihaletakip")


@shared_task(name="tenders.tasks.check_tender_alarms")
def check_tender_alarms():
    """
    Aktif ihale alarmlarını kontrol eder ve gerekirse push bildirimi gönderir.

    NOT: EKAP sorgu entegrasyonu ileride eklenecek. Şu an tamamlanmamış
    (completed=False) alarm sayısını loglar ve iskeleti hazır tutar.
    """
    from .models import TenderAlarm

    active = TenderAlarm.objects.filter(completed=False).select_related("user")
    count = active.count()
    logger.info("check_tender_alarms: %s aktif alarm kontrol edildi", count)

    # TODO: her alarm için EKAP'tan durum çek → değişiklik varsa
    # Notification oluştur + FCM push gönder (ai.services yerine
    # tenders.services.push kullanılabilir).
    return {"checked": count}


@shared_task(name="tenders.tasks.cleanup_old_notifications")
def cleanup_old_notifications(days: int = 30):
    """Belirtilen günden eski OKUNMUŞ bildirimleri siler."""
    from .models import Notification

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = Notification.objects.filter(
        read=True, created_at__lt=cutoff
    ).delete()
    logger.info("cleanup_old_notifications: %s bildirim silindi", deleted)
    return {"deleted": deleted}
