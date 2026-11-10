"""Hesap Celery görevleri."""
from celery import shared_task


@shared_task(name="accounts.tasks.hosgeldin_eposta", ignore_result=True, bind=True,
             max_retries=3, default_retry_delay=300)
def hosgeldin_eposta(self, user_id: int):
    """Yeni kullanıcıya bir kez hoş geldin e-postası (`accounts/services/hosgeldin.py`).
    SMTP düşerse 5 dk arayla 3 kez yeniden denenir (damga geri alındığı için mükerrer olmaz)."""
    from .services.hosgeldin import gonder

    try:
        return gonder(user_id)
    except Exception as e:  # noqa: BLE001
        raise self.retry(exc=e)
