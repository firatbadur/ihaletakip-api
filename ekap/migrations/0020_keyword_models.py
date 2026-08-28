"""
Anahtar kelime (keyword) katmanı — modeller ve `Tender` kolonları.

⚠️ Bu migration **indeks kurmaz** (`kalip_hash`/`sektor` üzerinde `db_index` yok).
Sıra bilinçli ve `seri_anahtar`'ınkiyle aynı:

    0020 kolon (metadata-only) → doldurma (1M satır UPDATE) → 0021 indeks (CONCURRENTLY)

Ters sırada iki şey birden bozulurdu: `AddField`'ın ürettiği düz `CREATE INDEX` dolu
tabloda ACCESS EXCLUSIVE kilidi alır (site durur) ve ardından gelen 1M satırlık UPDATE
o indeksi şişirirdi.

⚠️ `lock_timeout` ZORUNLU: nullable kolon eklemek PG11+'da tabloyu yeniden yazmaz ama
yine de kısa bir ACCESS EXCLUSIVE kilidi ister. O kilit, gece koşan `sync_contractors`
süpürmesi gibi uzun bir transaction'ın arkasına düşerse kendisi de sonraki tüm
okuyucuları arkasına dizer. Timeout ile migration kilidi alamazsa hızlıca hata verir;
deploy tekrar denenir, kimse bloklanmaz. `SET LOCAL` transaction kapsamlı olduğu için
bu migration `atomic = True` KALMALIDIR.
"""

import django.db.models.deletion
from django.db import migrations, models

from ._pg_ops import lock_timeout


class Migration(migrations.Migration):

    dependencies = [
        ('ekap', '0019_okasitem_norm_index'),
    ]

    operations = [
        lock_timeout("5s"),
        migrations.CreateModel(
            name='Keyword',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('metin', models.CharField(max_length=64, unique=True)),
                ('metin_ham', models.CharField(blank=True, max_length=64)),
                ('derece', models.SmallIntegerField(default=1)),
                ('kullanim_sayisi', models.IntegerField(db_index=True, default=0)),
                ('pasif', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Anahtar Kelime',
                'verbose_name_plural': 'Anahtar Kelimeler',
                'ordering': ['-kullanim_sayisi'],
            },
        ),
        migrations.CreateModel(
            name='KeywordBatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('batch_id', models.CharField(max_length=80, unique=True)),
                ('durum', models.CharField(choices=[('created', 'Oluşturuldu'), ('in_progress', 'İşleniyor'), ('ended', 'Tamamlandı'), ('processed', 'Sonuçlar İşlendi'), ('expired', 'Süresi Doldu'), ('error', 'Hata')], db_index=True, default='created', max_length=16)),
                ('model', models.CharField(blank=True, max_length=40)),
                ('istek_sayisi', models.IntegerField(default=0)),
                ('kalip_sayisi', models.IntegerField(default=0)),
                ('basarili', models.IntegerField(default=0)),
                ('hatali', models.IntegerField(default=0)),
                ('expired', models.IntegerField(default=0)),
                ('input_tokens', models.BigIntegerField(default=0)),
                ('output_tokens', models.BigIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('islendi_at', models.DateTimeField(blank=True, null=True)),
                ('note', models.TextField(blank=True)),
            ],
            options={
                'verbose_name': 'Keyword Batch',
                'verbose_name_plural': "Keyword Batch'leri",
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='tender',
            name='kalip_hash',
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name='tender',
            name='sektor',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.CreateModel(
            name='TenderNamePattern',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kalip_hash', models.CharField(max_length=40, unique=True)),
                ('kalip_norm', models.TextField()),
                ('ornek_ad', models.TextField(blank=True)),
                ('ihale_sayisi', models.IntegerField(db_index=True, default=0)),
                ('durum', models.CharField(choices=[('pending', 'Bekliyor'), ('queued', "Batch'te"), ('ok', 'Tamam'), ('skipped', 'Atlandı'), ('error', 'Hata')], db_index=True, default='pending', max_length=12)),
                ('deneme', models.SmallIntegerField(default=0)),
                ('sektor', models.CharField(blank=True, max_length=32)),
                ('keyword_ids', models.JSONField(blank=True, default=list)),
                ('guven', models.DecimalField(blank=True, decimal_places=3, max_digits=4, null=True)),
                ('model', models.CharField(blank=True, max_length=40)),
                ('hata', models.TextField(blank=True)),
                ('islendi_at', models.DateTimeField(blank=True, null=True)),
                ('batch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='kaliplar', to='ekap.keywordbatch')),
            ],
            options={
                'verbose_name': 'İhale Adı Kalıbı',
                'verbose_name_plural': 'İhale Adı Kalıpları',
                'ordering': ['-ihale_sayisi'],
            },
        ),
        migrations.CreateModel(
            name='TenderKeyword',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('keyword', models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='ihale_baglari', to='ekap.keyword')),
                ('tender', models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='keyword_baglari', to='ekap.tender')),
            ],
            options={
                'verbose_name': 'İhale Anahtar Kelimesi',
                'verbose_name_plural': 'İhale Anahtar Kelimeleri',
                'indexes': [models.Index(fields=['keyword', 'tender'], name='ekap_tk_kw_tender_idx')],
                'constraints': [models.UniqueConstraint(fields=('tender', 'keyword'), name='ekap_tk_uniq')],
            },
        ),
    ]
