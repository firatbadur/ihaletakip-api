"""BlogPost/BlogCategory değişince web'e revalidate (bkz. services.web_revalidate)."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import BlogCategory, BlogPost
from .services import web_revalidate


def _sonra(slug=None):
    # ⚠️ `on_commit`: kayıt geri alınırsa web'e "yeni yazı var" denmesin; ayrıca web
    # isteği ulaştığında sorgu yeni hâli görebilsin.
    transaction.on_commit(lambda: web_revalidate(slug))


@receiver(post_save, sender=BlogPost)
def yazi_kaydedildi(sender, instance, raw=False, **kwargs):
    if raw:  # loaddata
        return
    # Yalnızca kamuya açık hâli değişen yazılar: yayımlandı, düzeltildi ya da yayından
    # kaldırıldı. Taslağın taslak olarak kaydedilmesi web'i ilgilendirmez.
    # ⚠️ İleri tarihli yazı şimdi yayında değildir; o tarih gelince beat görevi
    # (`tasks.zamanlanmis_yazilari_bildir`) haber verir.
    if instance.yayinda_mi() or instance.onceden_yayindaydi():
        _sonra(instance.slug)
    instance._onceki = {"status": instance.status,
                        "published_at": instance.published_at, "slug": instance.slug}


@receiver(post_delete, sender=BlogPost)
def yazi_silindi(sender, instance, **kwargs):
    if instance.yayinda_mi() or instance.onceden_yayindaydi():
        _sonra(instance.slug)


@receiver(post_save, sender=BlogCategory)
@receiver(post_delete, sender=BlogCategory)
def kategori_degisti(sender, instance, raw=False, **kwargs):
    if raw:
        return
    _sonra(None)
