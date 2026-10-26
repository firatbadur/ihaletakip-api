"""Blog Celery görevleri."""
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import BlogPost
from .services import web_revalidate

# Beat aralığından (10 dk) GENİŞ: bir tur kaçarsa (beat yeniden başlatıldı) yazı
# bir sonraki turda yine yakalanır. Çift bildirim zararsızdır (revalidate idempotent).
_PENCERE_DK = 25


@shared_task(name="blog.tasks.zamanlanmis_yazilari_bildir")
def zamanlanmis_yazilari_bildir():
    """`published_at`'i az önce gelen zamanlanmış yazılar için web'e haber verir.

    ⚠️ Bu görev hiç koşmasa da yazı en geç web'in 1 saatlik önbelleği dolunca görünür;
    görev yalnızca gecikmeyi kısaltır.
    """
    simdi = timezone.now()
    sluglar = list(
        BlogPost.objects.yayinda(simdi)
        .filter(published_at__gt=simdi - timedelta(minutes=_PENCERE_DK))
        .values_list("slug", flat=True)
    )
    gonderilen = sum(1 for s in sluglar if web_revalidate(s))
    return {"aday": len(sluglar), "gonderilen": gonderilen}
