"""
`Tender.detay_kaynak` — `detail_raw`ın hangi kaynaktan geldiğini işaretler.

EKAP Mobil API birincil kaynak olduğunda `detail_raw` artık ham EKAP yanıtı değil,
mobil payload'dan çevrilmiş **sentetik** bir gövdedir (`ekap/mobil/adapt.py`).
Bu kolon olmadan "bu detail_raw neden farklı görünüyor?" sorusu ancak gövdeyi açarak
cevaplanabilirdi.

⚠️ **DB seviyesinde default ŞART** — 2026-08-28'de yaşanan 3 günlük veri kaybının
dersi (bkz. `0023_keyword_column_defaults`): deploy sırasında yalnızca `web` yeni
imaja geçirilirse eski worker'lar bu kolonu `INSERT`'e koymaz; DB'de default yoksa
NULL gider ve **her ihale düşer**. Django `AddField` default'u kalıcı yazmaz, bu
yüzden ayrıca `SET DEFAULT` verilir.
"""
from django.db import migrations, models

from ._pg_ops import PgRunSQL, lock_timeout


class Migration(migrations.Migration):

    dependencies = [
        ("ekap", "0023_keyword_column_defaults"),
    ]

    operations = [
        # ⚠️ 1M+ satırlık tabloda ALTER: kilidi uzun bir transaction'ın arkasında
        # beklemek TÜM okuyucuları arkasına dizer (bkz. `_pg_ops.lock_timeout`).
        lock_timeout("5s"),
        migrations.AddField(
            model_name="tender",
            name="detay_kaynak",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        PgRunSQL(
            "ALTER TABLE ekap_tender ALTER COLUMN detay_kaynak SET DEFAULT '';",
            "ALTER TABLE ekap_tender ALTER COLUMN detay_kaynak DROP DEFAULT;",
        ),
    ]
