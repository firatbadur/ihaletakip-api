"""
Favori idare (idare takibi + alarmı) yeniden **Pro'ya özel** (ürün kararı 2026-10-14).

2026-09-29'da Free'ye açılmıştı; "fazla oldu" kararıyla geri alındı ve **mevcut Free
kayıtları korunmaz**: Pro OLMAYAN kullanıcıların tüm favori idareleri (alarmlarıyla
birlikte) silinir. Bundan sonra yalnızca Pro ekleyebilir (`actions.idare_favori_ekle`
→ 403 `premium_required`).

⚠️ GERİ ALINAMAZ — reverse no-op'tur; silinen satırlar geri gelmez.
⚠️ "Pro" tanımı `accounts.User.is_premium` property'sinin BİREBİR kopyası (migration
geçmiş modelle çalışır, property'ye erişemez): superuser **veya** katman `pro` ve
bitişi boş/gelecekte. Property değişirse bu dosya değişmez — tek seferlik bir temizliktir.
⚠️ Tek DELETE, tablo küçük → entrypoint migrate'i healthcheck penceresini zorlamaz.
"""
from django.db import migrations
from django.db.models import Q
from django.utils import timezone


def free_favorileri_sil(apps, schema_editor):
    FavoriteAuthority = apps.get_model("tenders", "FavoriteAuthority")
    User = apps.get_model("accounts", "User")

    now = timezone.now()
    pro_idler = User.objects.filter(
        Q(is_superuser=True)
        | (
            Q(subscription_tier="pro")
            & (Q(subscription_expires_at__isnull=True) | Q(subscription_expires_at__gt=now))
        )
    ).values_list("pk", flat=True)

    silinecek = FavoriteAuthority.objects.exclude(user_id__in=list(pro_idler))
    adet = silinecek.count()
    kullanici = silinecek.values("user_id").distinct().count()
    silinecek.delete()
    print(f"\n  favori idare temizliği: {kullanici} Free kullanıcının {adet} kaydı silindi")


class Migration(migrations.Migration):

    dependencies = [
        ("tenders", "0013_notification_defaults"),
        ("accounts", "0009_trial_reminder_sent_at"),
    ]

    operations = [
        migrations.RunPython(free_favorileri_sil, migrations.RunPython.noop),
    ]
