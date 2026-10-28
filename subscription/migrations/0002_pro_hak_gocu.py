"""
Bugünkü Pro kullanıcılarını `ProGrant` satırlarına taşır (web 6. not #15.4).

Kural (migration RevenueCat'e GİDEMEZ — ağ yok, deploy'u da bekletmemeli):
- RC izi olan (`subscription_last_event` ya da `subscription_period_type` dolu —
  webhook ya da iptal backfill'i yazmış) → `store` satırı.
- İzi olmayan → `admin` satırı (elle verilmiş varsayılır), not alanında işaretli.
Bitiş, kullanıcının bugünkü `subscription_expires_at`'i ile AYNIDIR → göç hiçbir
kullanıcının etkin durumunu değiştirmez; kullanıcı alanlarına dokunulmaz.
⚠️ Yanlış sınıflanan bir mağaza müşterisi `admin` satırında kalırsa o tarihe kadar Pro
kalır (bugünkü durumla aynı). Denetim: `manage.py pro_hak_gocu_dogrula` (RC'ye sorar).
⚠️ Süresi geçmiş Pro'lar taşınmaz (zaten Free sayılıyorlar). Küçük tablo; CONCURRENTLY
ya da elle migrate gerekmez.
"""
from django.db import migrations
from django.db.models import Q
from django.utils import timezone

GOC_NOTU = "0002 göçü: elle verilmiş Pro varsayıldı (RevenueCat izi yok)"


def ileri(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    ProGrant = apps.get_model("subscription", "ProGrant")
    simdi = timezone.now()
    qs = User.objects.filter(subscription_tier="pro").filter(
        Q(subscription_expires_at__isnull=True) | Q(subscription_expires_at__gt=simdi)
    )
    satirlar = []
    for u in qs.iterator():
        rc = bool(u.subscription_last_event or u.subscription_period_type)
        satirlar.append(ProGrant(
            user_id=u.pk,
            source="store" if rc else "admin",
            starts_at=u.date_joined or simdi,
            ends_at=u.subscription_expires_at,
            note="" if rc else GOC_NOTU,
            hatirlatmalar=[],
        ))
    ProGrant.objects.bulk_create(satirlar, batch_size=500)


def geri(apps, schema_editor):
    apps.get_model("subscription", "ProGrant").objects.filter(
        Q(source="store") | Q(note=GOC_NOTU)
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("subscription", "0001_pro_hak_odeme"),
        ("accounts", "0012_eposta_geri_bildirim"),
    ]

    operations = [migrations.RunPython(ileri, geri)]
