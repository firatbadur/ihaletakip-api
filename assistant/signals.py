"""
Asistan eki silinince yerel ham dosya ve Files API dosyası da silinir.

Kapsadığı yollar: sohbet silme (CASCADE), admin'den kullanıcı/ek silme (CASCADE),
`DeactivateView`, bağlanmamış ekin DELETE'i, `ek_temizligi`. Silme `on_commit` ile
kuyruğa atılır — işlem geri alınırsa dosya silinmez.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import AssistantAttachment

logger = logging.getLogger("ihaletakip")


@receiver(post_delete, sender=AssistantAttachment)
def ek_silindi(sender, instance, **kwargs):
    file_ids = [instance.anthropic_file_id] if instance.anthropic_file_id else []
    yollar = [instance.depo_yolu] if instance.depo_yolu else []
    if not file_ids and not yollar:
        return

    def _kuyrukla():
        from .tasks import ek_dosyalarini_sil_task, ham_dosyayi_sil

        try:
            ek_dosyalarini_sil_task.delay(file_ids, yollar)
        except Exception:
            # Kuyruk erişilemez: silme işlemi (sohbet silme, hesap kapatma) düşmesin.
            # Yerel ham dosya hemen silinir; Files API dosyası saklama süresi sonunda
            # kendiliğinden düşer (yüklemede `expires_in_seconds` verildi).
            logger.warning("ek dosya silme kuyruğa atılamadı; yerel dosya hemen siliniyor")
            for yol in yollar:
                ham_dosyayi_sil(yol)

    transaction.on_commit(_kuyrukla, robust=True)
