"""Core Celery görevleri."""
from celery import shared_task


@shared_task(name="core.tasks.destek_ekibine_eposta", bind=True, ignore_result=True,
             max_retries=4)
def destek_ekibine_eposta(self, ticket_id: int, body: str, yeni: bool):
    """Destek ekibine e-posta — SMTP (15 sn zaman aşımı) kullanıcının isteğini bekletmesin.

    ⚠️ Hata YUTULMAZ: eskiden `fail_silently=True` ile SMTP arızası iz bırakmadan
    kayboluyordu. Artık görev artan aralıkla yeniden dener (1/2/4/8 dk).
    """
    from .destek_eposta import ekibe_gonder

    try:
        return ekibe_gonder(ticket_id, body, yeni)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60 * 2 ** self.request.retries)


@shared_task(name="core.tasks.destek_yaniti_eposta", bind=True, ignore_result=True,
             max_retries=4)
def destek_yaniti_eposta(self, mesaj_id: int):
    """Destek yanıtını kullanıcıya e-postalar (mesaj başına bir kez, `destek_eposta`)."""
    from .destek_eposta import kullaniciya_gonder

    try:
        return kullaniciya_gonder(mesaj_id)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60 * 2 ** self.request.retries)


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


@shared_task(name="core.tasks.otomatik_aski", ignore_result=True)
def otomatik_aski():
    """Açık sinyallerde 24 sa otomatik hesap askısı (`core.guvenlik.otomatik_aski`)."""
    from .guvenlik import otomatik_aski as calistir

    return {"askiya_alinan": calistir()}


@shared_task(name="core.tasks.toplu_gonderim_isle", ignore_result=True)
def toplu_gonderim_isle(gonderim_id: int):
    """Toplu e-posta / push gönderiminin bir turu (`core/toplu.py`). İş kaldıysa kendini
    yeniden kuyruğa atar → `celery` kuyruğunu tek başına uzun süre meşgul etmez."""
    from .toplu import isle

    return isle(gonderim_id)
