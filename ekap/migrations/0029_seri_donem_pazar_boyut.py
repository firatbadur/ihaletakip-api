"""
Tekrar eden ihale serilerine **dönem** alanları + pazar panosuna **eksen (boyut)**.

## 1) `RecurringTenderSeries.donem_sayisi` / `.sektor`

Periyot artık ihale sayısından değil **dönem** sayısından hesaplanıyor
(`tasks._donemler`): kısım lotları ve iptal-yeniden ihale tek dönemdir.
`sektor` seriyi pazar panosu/onboarding taksonomisiyle aynı eksende filtrelenebilir
kılar.

## 2) `MarketStat.boyut` + `okas_bucket` genişletmesi

Pano artık **sektör** ekseninde sunuluyor (OKAS ekseni geriye dönük uyumluluk için
yazılmaya devam eder). Aynı tabloda iki eksen tutulduğu için grain
`(yil, okas_bucket)` → `(yil, boyut, okas_bucket)` oldu ve kod alanı 4 → 32 karakter
genişletildi (sektör kodları `saglik_tibbi_malzeme` gibi).

⚠️ Postgres'te `varchar(n)` **uzatmak** metadata-only'dir (tablo yeniden yazılmaz).

⚠️⚠️ **DB SEVİYESİNDE DEFAULT ŞART** (bkz. `0023_keyword_column_defaults`,
2026-08-28'de üç gün veri kaybettiren arıza): Django `AddField`'in default'unu
yalnızca mevcut satırları doldurmak için kullanıp düşürür. Deploy sırasında eski
koddaki bir worker satır yazarsa kolonu INSERT'e hiç koymaz ve NOT NULL ihlali alır.

⚠️ Unique kısıt değiştiği için **eski satırlar `boyut='okas'` olarak damgalanır** —
default bunu zaten yapar; böylece mevcut pano verisi kısıt ihlali üretmeden
korunur ve ilk `refresh_market_stats` turuna kadar eski eksen çalışmaya devam eder.
"""
from django.db import migrations, models

from ._pg_ops import PgRunSQL


class Migration(migrations.Migration):

    dependencies = [
        ('ekap', '0028_uzlasi_izi'),
    ]

    operations = [
        # ── Seri: dönem sayısı + sektör ───────────────────────────────────────
        migrations.AddField(
            model_name='recurringtenderseries',
            name='donem_sayisi',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='recurringtenderseries',
            name='sektor',
            field=models.CharField(blank=True, max_length=32),
        ),
        PgRunSQL(
            "ALTER TABLE ekap_recurringtenderseries "
            "ALTER COLUMN donem_sayisi SET DEFAULT 0, "
            "ALTER COLUMN sektor SET DEFAULT '';",
            "ALTER TABLE ekap_recurringtenderseries "
            "ALTER COLUMN donem_sayisi DROP DEFAULT, "
            "ALTER COLUMN sektor DROP DEFAULT;",
        ),

        # ── Pazar panosu: eksen ───────────────────────────────────────────────
        migrations.AddField(
            model_name='marketstat',
            name='boyut',
            field=models.CharField(db_index=True, default='okas', max_length=8),
        ),
        PgRunSQL(
            "ALTER TABLE ekap_marketstat ALTER COLUMN boyut SET DEFAULT 'okas';",
            "ALTER TABLE ekap_marketstat ALTER COLUMN boyut DROP DEFAULT;",
        ),
        migrations.AlterField(
            model_name='marketstat',
            name='okas_bucket',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.RemoveConstraint(
            model_name='marketstat',
            name='ekap_marketstat_uq',
        ),
        migrations.AddConstraint(
            model_name='marketstat',
            constraint=models.UniqueConstraint(
                fields=('yil', 'boyut', 'okas_bucket'), name='ekap_marketstat_uq'),
        ),
        migrations.RemoveIndex(
            model_name='marketstat',
            name='ekap_market_yil_bedel_idx',
        ),
        migrations.AddIndex(
            model_name='marketstat',
            index=models.Index(fields=['yil', 'boyut', '-toplam_bedel'],
                               name='ekap_market_yil_bedel_idx'),
        ),
    ]
