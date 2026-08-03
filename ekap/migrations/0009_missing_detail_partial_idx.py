r"""
"Detayı hiç çekilmemiş ihaleler" için kısmi (partial) indeksler.

`pg_stat_statements` üretimde şu sorguyu **en pahalı** olarak gösterdi:
    SELECT ekap_id FROM ekap_tender WHERE detail_synced_at IS NULL AND ekap_id <> ''
    ORDER BY ihale_tarihi DESC LIMIT 50
    → 290 çağrı · çağrı başına **78 sn** · toplam **6.3 saat** DB zamanı

Kaynak: `sync_contractors.enqueue_missing_detail` (10 dk'da bir) ve `refresh_stale`.
Planlayıcı `ORDER BY <tarih> DESC LIMIT N`'i görünce tarih indeksini geriye tarayıp
`detail_synced_at IS NULL` filtreliyor; eşleşen satırlar seyrek olduğu için 50 satır
bulmak neredeyse tüm tabloyu gezmek demek. (Aynı plan tuzağı ihale detay ucunda ve
`_tender_idare_id_set`'te de yaşandı — bkz. CLAUDE.md.)

Kısmi indeks yalnızca `detail_synced_at IS NULL` satırlarını tutar → hem küçük hem de
sorgunun (filtre + sıralama) tam karşılığı. Bu sorgu, arama sorgularının buffer
cache'ini boşaltan asıl failin büyük kısmıydı (heap cache isabeti %53 ölçüldü).

⚠️ `atomic = False` + CONCURRENTLY + yeniden çalıştırılabilirlik: bkz. `_pg_ops`.
"""
from django.db import migrations, models

from ._pg_ops import PgAddIndexConcurrently


class Migration(migrations.Migration):

    atomic = False  # ⚠️ CONCURRENTLY transaction içinde çalışamaz — kaldırmayın

    dependencies = [
        ("ekap", "0008_contractor_search_indexes"),
    ]

    operations = [
        # `sync_contractors.enqueue_missing_detail` → ORDER BY ihale_tarihi DESC
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                condition=models.Q(("detail_synced_at__isnull", True)),
                fields=["-ihale_tarihi"],
                name="ekap_tender_detayeksik_iht",
            ),
        ),
        # `refresh_stale` aday havuzu → ORDER BY ilan_tarihi DESC
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                condition=models.Q(("detail_synced_at__isnull", True)),
                fields=["-ilan_tarihi"],
                name="ekap_tender_detayeksik_ilt",
            ),
        ),
    ]
