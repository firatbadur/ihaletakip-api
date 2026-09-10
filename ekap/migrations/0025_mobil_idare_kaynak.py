"""
`Tender.idare_kaynak` — `idare_id`nin hangi yolla dolduğunu işaretler.

Mobil API `idare_id` vermiyor; alan ad eşleştirmesiyle dolduruluyor
(`ekap/mobil/idare.py`) ve bu yöntem **kusurlu**: ölçülen kesinlik tam-ad
eşleşmesinde %97,9, trigram ≥0,90'da %92,2. Hangi satırın tahminle dolduğu
denetlenebilir olmalı — aksi hâlde yanlış bir `idare_id` sonsuza dek "gerçek"
sanılır ve geri alınamaz.

⚠️ **DB seviyesinde default ŞART** (2026-08-28 dersi, bkz. `0023`): deploy sırasında
eski imajlı worker'lar kolonu `INSERT`'e koymaz; DB default'u yoksa NULL gider ve
her ihale düşer.
"""
from django.db import migrations, models

from ._pg_ops import PgRunSQL, lock_timeout


class Migration(migrations.Migration):

    dependencies = [
        ("ekap", "0024_mobil_detay_kaynak"),
    ]

    operations = [
        lock_timeout("5s"),
        migrations.AddField(
            model_name="tender",
            name="idare_kaynak",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        PgRunSQL(
            "ALTER TABLE ekap_tender ALTER COLUMN idare_kaynak SET DEFAULT '';",
            "ALTER TABLE ekap_tender ALTER COLUMN idare_kaynak DROP DEFAULT;",
        ),
    ]
