"""Core Celery görevleri."""
from celery import shared_task


@shared_task(name="core.tasks.destek_ekibine_eposta", ignore_result=True)
def destek_ekibine_eposta(ticket_id: int, body: str, yeni: bool):
    """Destek ekibine e-posta — SMTP (15 sn zaman aşımı) kullanıcının isteğini bekletmesin."""
    from .models import SupportTicket
    from .support import ekibe_eposta_gonder

    ticket = SupportTicket.objects.filter(pk=ticket_id).first()
    if ticket:
        ekibe_eposta_gonder(ticket, body, yeni=yeni)
