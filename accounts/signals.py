"""Hesap sinyalleri — yeni kullanıcıya hoş geldin e-postası.

Tetik `post_save(created=True)`: e-posta kaydı, Google ve Apple girişinin hepsi `User`
oluşturur → tek kapı. Görev `on_commit`'te kuyruğa atılır (kayıt transaction'ı geri
alınırsa e-posta gitmez) ve kısa bir gecikmeyle çalışır (sosyal girişte ad/e-posta
oluşturmadan hemen sonra yazılabiliyor). ⚠️ Mevcut kullanıcılar etkilenmez: sinyal
yalnızca YENİ satırda tetiklenir.
"""
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="hosgeldin_eposta")
def yeni_kullanici(sender, instance, created, raw=False, **kwargs):
    if not created or raw or not getattr(settings, "HOSGELDIN_EPOSTA_ENABLED", True):
        return
    if instance.is_staff or instance.is_superuser or not instance.email:
        return
    uid = instance.pk

    def kuyruga_al():
        from .tasks import hosgeldin_eposta

        hosgeldin_eposta.apply_async((uid,), countdown=30)

    transaction.on_commit(kuyruga_al)


def _kimlik_esleme_degisti(sender, **kwargs):
    from .authentication import kurallari_unut

    kurallari_unut()


def _kimlik_esleme_sinyalleri():
    from django.db.models.signals import post_delete

    from .models import KimlikEsleme

    post_save.connect(_kimlik_esleme_degisti, sender=KimlikEsleme, dispatch_uid="kimlik_esleme_kaydet")
    post_delete.connect(_kimlik_esleme_degisti, sender=KimlikEsleme, dispatch_uid="kimlik_esleme_sil")


_kimlik_esleme_sinyalleri()
