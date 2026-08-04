"""
Pro veri katmanı — Adım 1: `detail_raw`'dan türetilen sinyal kolonları.

Kolonlar boş eklenir; doldurma `ekap.tasks.backfill_tender_fields` (gece penceresi,
zaman bütçeli, PK imleçli) ile günlere yayılır. Yeni ve tazelenen kayıtlar zaten
`sync.upsert_tender_detail` içindeki `apply_pro_fields` ile anında dolar.

⚠️ İNDEKS YOK — 0010 ile aynı gerekçe: nullable kolon eklemek PG11+'da metadata-only,
ama indeks şimdi kurulursa doldurma ~1M satırı UPDATE ederken onu şişirir.
Sıra: kolon → doldur → `AddIndexConcurrently` (Adım 2, filtreler geldiğinde).
"""
from django.db import migrations, models

from ._pg_ops import lock_timeout


class Migration(migrations.Migration):

    # `SET LOCAL` transaction kapsamlı → atomic ŞART.
    atomic = True

    dependencies = [
        ('ekap', '0010_pro_ozet_ve_bakanlik'),
    ]

    operations = [
        lock_timeout("5s"),
        migrations.AddField(
            model_name='tender',
            name='duzeltme_ilani_var',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tender',
            name='e_eksiltme_yapilacak',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tender',
            name='fiyat_disi_unsur_var',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tender',
            name='idareye_sikayet_var',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tender',
            name='ilansiz_mi',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tender',
            name='istekli_sayisi',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tender',
            name='itirazen_sikayet_var',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tender',
            name='kismi_ihale',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tender',
            name='okas_ana_adi',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='tender',
            name='okas_ana_kod',
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name='tender',
            name='okas_bucket',
            field=models.CharField(blank=True, max_length=4),
        ),
        migrations.AddField(
            model_name='tender',
            name='okas_kalem_sayisi',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='tender',
            name='seri_anahtar',
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name='tender',
            name='sikayet_dilekce_var',
            field=models.BooleanField(blank=True, null=True),
        ),
    ]
