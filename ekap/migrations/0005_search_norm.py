"""
Türkçe+aksan-duyarsız arama sütunları (`*_norm`) + mevcut satırların backfill'i.

DB `icontains`/ILIKE Türkçe İ↔i, ş↔s katlamasını yapmadığı için kullanıcı küçük
harf Türkçe yazınca arama boş dönüyordu. `normalize_tr` ile normalize edilmiş
sütunlar eklenir ve mevcut kayıtlar doldurulur.

⚠️ Migration **atomic**'tir (bilerek): önceki sürüm `atomic = False` idi; migrate
büyük tabloda backfill sürerken kesilirse (ör. konteyner restart) sütunlar EKLENMİŞ
ama migration KAYDEDİLMEMİŞ kalıyordu → tekrar çalışınca "column already exists".
Atomic ile ya hepsi uygulanır ya temiz rollback → yeniden çalıştırma daima güvenli.
Taze deploy'da tablolar boş olduğundan backfill anlık; dolu tabloda (yükseltme)
migrate elle çalıştırılır (healthcheck baskısı olmadan) — bkz. CLAUDE.md deploy notu.
"""
from django.db import migrations, models


def backfill_norm(apps, schema_editor):
    from ekap.utils import normalize_tr

    Authority = apps.get_model("ekap", "Authority")
    OkasCode = apps.get_model("ekap", "OkasCode")
    Tender = apps.get_model("ekap", "Tender")

    def run(model, mapping, batch_size=2000):
        # mapping: [(kaynak_alan, hedef_norm_alan), ...]
        dst_fields = [dst for _, dst in mapping]
        batch = []
        for obj in model.objects.all().only("pk", *[s for s, _ in mapping]).iterator(chunk_size=batch_size):
            for src, dst in mapping:
                setattr(obj, dst, normalize_tr(getattr(obj, src)))
            batch.append(obj)
            if len(batch) >= batch_size:
                model.objects.bulk_update(batch, dst_fields)
                batch = []
        if batch:
            model.objects.bulk_update(batch, dst_fields)

    run(Authority, [("ad", "ad_norm")])
    run(OkasCode, [("adi", "adi_norm")])
    run(Tender, [("ihale_adi", "ihale_adi_norm"), ("idare_adi", "idare_adi_norm")])


class Migration(migrations.Migration):
    # atomic=True (varsayılan) — kesilirse temiz rollback; yeniden çalıştırma güvenli.
    dependencies = [
        ("ekap", "0004_authority_tree"),
    ]

    operations = [
        migrations.AddField(
            model_name="authority",
            name="ad_norm",
            field=models.CharField(blank=True, db_index=True, max_length=500),
        ),
        migrations.AddField(
            model_name="okascode",
            name="adi_norm",
            field=models.CharField(blank=True, db_index=True, max_length=500),
        ),
        migrations.AddField(
            model_name="tender",
            name="ihale_adi_norm",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="tender",
            name="idare_adi_norm",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.RunPython(backfill_norm, migrations.RunPython.noop),
    ]
