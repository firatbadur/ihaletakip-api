"""Eski tek soru / tek yanıt talepleri yazışmaya (SupportMessage) taşır.

- `message` → ilk kullanıcı mesajı (talebin açılış zamanıyla)
- `reply`   → destek mesajı (talebin son güncellenme zamanıyla; gerçek yanıt anı
  tutulmuyordu, en yakın tahmin bu)
- Yanıtı olan talepler `user_unread=True` olur: bu yanıtlar kullanıcıya HİÇ
  ulaşmamıştı (bildirim yoktu, uygulamada gösterilmiyordu) → ilk açılışta görsün.
"""
from django.db import migrations


def ileri(apps, schema_editor):
    SupportTicket = apps.get_model("core", "SupportTicket")
    SupportMessage = apps.get_model("core", "SupportMessage")
    yeni = []
    for t in SupportTicket.objects.all().iterator():
        son = t.created_at
        if t.message:
            yeni.append(SupportMessage(ticket_id=t.pk, sender="user", body=t.message,
                                       created_at=t.created_at))
        yanit = (t.reply or "").strip()
        if yanit:
            son = max(t.updated_at or t.created_at, t.created_at)
            yeni.append(SupportMessage(ticket_id=t.pk, sender="staff", body=yanit, created_at=son))
        SupportTicket.objects.filter(pk=t.pk).update(last_message_at=son, user_unread=bool(yanit))
    SupportMessage.objects.bulk_create(yeni, batch_size=500)


def geri(apps, schema_editor):
    apps.get_model("core", "SupportMessage").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0004_destek_yazisma")]
    operations = [migrations.RunPython(ileri, geri)]
