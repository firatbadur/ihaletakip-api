r"""
`Contract`'a OKAS + bakanlık ingest-kopyaları (mevcut `idare_id`/`il_id`/`ihale_tip`
ile aynı gerekçe).

⚠️ **Neden:** fiyat istihbaratı (benchmark) benzerlik merdiveni `tender__okas_ana_kod`
üzerinden JOIN yapıyordu. Ölçüm 2026-08-13, prod (1.031.670 ihale / 11 GB,
1.407.319 sözleşme / 1 GB):

    Nested Loop
      -> Parallel Seq Scan on ekap_tender t  (Filter: okas_ana_kod = $1)
           Rows Removed by Filter: 487244
           Buffers: shared hit=221110          ← 1,7 GB
    Execution Time: 4559 ms

Yani her benchmark çağrısı 11 GB'lık tabloyu baştan sona tarıyordu. Kopyayla sorgu
tamamen `ekap_contract` içinde kalır.

⚠️ Bu migration **yalnızca boş kolon ekler** (nullable/blank → PG11+'da metadata-only,
1,4M satır yeniden yazılmaz). Doldurma ayrı adımdır:
    python manage.py backfill_contract_fields
İndeksler ise **doldurmadan SONRA** (`0015`) gelir — boş kolona indeks kurup ardından
1,4M satır UPDATE etmek indeksi şişirir.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ekap", "0013_recurring_series"),
    ]

    operations = [
        migrations.AddField(
            model_name="contract",
            name="okas_ana_kod",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="contract",
            name="okas_bucket",
            field=models.CharField(blank=True, max_length=4),
        ),
        migrations.AddField(
            model_name="contract",
            name="en_ust_idare_kod",
            field=models.CharField(blank=True, max_length=16),
        ),
    ]
