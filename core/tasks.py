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


@shared_task(name="core.tasks.erisim_engeli_suresi", ignore_result=True)
def erisim_engeli_suresi():
    """Süresi dolan erişim engellerini kaldırır (hesap engelinde `is_active` geri açılır)."""
    from .guvenlik import suresi_dolanlari_kaldir

    return {"kaldirilan": suresi_dolanlari_kaldir()}


@shared_task(name="core.tasks.guvenlik_olay_temizligi", ignore_result=True)
def guvenlik_olay_temizligi(gun: int = 90):
    """`gun` günden eski güvenlik olaylarını siler (`zaman` indeksli)."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import GuvenlikOlayi

    silinen, _ = GuvenlikOlayi.objects.filter(
        zaman__lt=timezone.now() - timedelta(days=gun)).delete()
    return {"silinen": silinen}
