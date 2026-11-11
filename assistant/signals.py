"""
Asistan eki silinince yerel ham dosya ve Files API dosyası da silinir.

Kapsadığı yollar: sohbet silme (CASCADE), admin'den kullanıcı/ek silme (CASCADE),
`DeactivateView`, bağlanmamış ekin DELETE'i, `ek_temizligi`. Silme `on_commit` ile
kuyruğa atılır — işlem geri alınırsa dosya silinmez.
"""
from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import AssistantAttachment


@receiver(post_delete, sender=AssistantAttachment)
def ek_silindi(sender, instance, **kwargs):
    file_ids = [instance.anthropic_file_id] if instance.anthropic_file_id else []
    yollar = [instance.depo_yolu] if instance.depo_yolu else []
    if not file_ids and not yollar:
        return

    def _kuyrukla():
        from .tasks import ek_dosyalarini_sil_task

        ek_dosyalarini_sil_task.delay(file_ids, yollar)

    transaction.on_commit(_kuyrukla)
