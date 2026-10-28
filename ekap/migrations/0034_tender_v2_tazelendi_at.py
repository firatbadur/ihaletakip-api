# ⚠️ Nullable kolon, default yok → PG'de YALNIZCA KATALOG değişikliği (tablo yeniden
# yazılmaz); eski imajdaki worker'lar kolonu tanımadan INSERT etse de NULL geçerlidir.

from django.db import migrations, models

from ._pg_ops import lock_timeout


class Migration(migrations.Migration):

    dependencies = [
        ('ekap', '0033_tender_v2_id'),
    ]

    operations = [
        lock_timeout(),
        migrations.AddField(
            model_name='tender',
            name='v2_tazelendi_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
