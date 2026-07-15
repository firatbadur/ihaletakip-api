"""
Authority modelini düz lookup'tan **DETSIS ağaç** düğümüne dönüştürür.

Eski alanlar (`detsis_id`, `ust_idare`, `idare_kod`) EKAP `id`/isim string'i tutuyordu;
ağaç için `detsis_no` (anahtar), `parent_detsis` (üst bağ), `idare_id` (ihale filtre
anahtarı), `has_items`, `seviye` gerekli. Kayıtlar yeniden senkronlanabilir lookup
olduğundan önce mevcut satırlar temizlenir, sonra şema değişir. Deploy sonrası:
`python manage.py run_ingest --task authorities` ile ağaç yeniden doldurulur.
"""
from django.db import migrations, models


def clear_authorities(apps, schema_editor):
    apps.get_model("ekap", "Authority").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ekap", "0003_tender_ozellikler_tender_yasa_kapsami"),
    ]

    operations = [
        # 1) Yeniden senkronlanabilir olduğu için eski satırları temizle
        #    (yeni unique `detsis_no` alanını boş tabloya güvenle eklemek için).
        migrations.RunPython(clear_authorities, migrations.RunPython.noop),

        # 2) Eski alanları kaldır
        migrations.RemoveField(model_name="authority", name="detsis_id"),
        migrations.RemoveField(model_name="authority", name="ust_idare"),
        migrations.RemoveField(model_name="authority", name="idare_kod"),

        # 3) Ağaç alanları
        migrations.AddField(
            model_name="authority",
            name="detsis_no",
            field=models.CharField(db_index=True, default="", max_length=64, unique=True),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="authority",
            name="parent_detsis",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name="authority",
            name="idare_id",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name="authority",
            name="has_items",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="authority",
            name="seviye",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
