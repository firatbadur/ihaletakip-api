r"""
`Tender.ilan_gorunur_at` — ihalenin bildirim akışına "yeni" olarak göründüğü an.

**Neden**: `ilan_tarihi` yalnızca **detay** senkronunda dolar, detay ise çoğu zaman ertesi
gece gelir. Bildirim görevleri "ilan_tarihi = BUGÜN" penceresi kullandığı için ihaleler
kalıcı olarak kaçıyordu: görev koştuğu gün alan hâlâ NULL, alan dolduğunda ise ihale artık
"dün" sayılıyor. Üretimde ölçüldü (2026-08-04): 3 Ağustos'ta ilan edilen 354 ihalenin
`ilan_tarihi`'si 4 Ağustos 01:27–02:02'de doldu → 3 Ağustos 10:00'daki filtre görevi hiçbir
şey bulamadı, 4 Ağustos'taki görev de onları "bugün değil" diye eledi.

**Veri backfill'i bilinçli olarak SINIRLI**: yalnızca son `_BACKFILL_DAYS` günde ilan edilen
ve detayı gelmiş satırlara `detail_synced_at` damgası yazılır (birkaç bin satır). Amaç:
  • kaçırılan son günleri telafi etmek (abonelik watermark'ı sayesinde yalnızca watermark'tan
    sonra görünür olanlar bildirilir — eskiler sessiz kalır),
  • 500k satırlık tam tabloyu UPDATE etmemek (entrypoint migrate'i healthcheck penceresinde
    koşuyor — bkz. CLAUDE.md "ağır data-migration").
Arşivin geri kalanı NULL kalır; NULL satırlar bildirim penceresine hiç girmez (istenen
davranış: geçmiş 500k ihale için push fırtınası olmasın).

⚠️ `atomic = False` + CONCURRENTLY: indeks açık transaction'ları bekler, ingest görevleri
sürerken uzun asılabilir. Yeniden çalıştırılabilirlik `_pg_ops` tarafından sağlanır.
"""
from django.db import migrations, models

from ._pg_ops import PgAddIndexConcurrently

# Telafi penceresi: bundan eski ihaleler damgalanmaz (bildirimde de zaten kapsam dışı).
_BACKFILL_DAYS = 15


def stamp_recent(apps, schema_editor):
    """Son _BACKFILL_DAYS günün ilanlarına `detail_synced_at`'i görünürlük damgası olarak yaz."""
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cur:
        cur.execute(
            "UPDATE ekap_tender SET ilan_gorunur_at = detail_synced_at "
            "WHERE ilan_gorunur_at IS NULL AND detail_synced_at IS NOT NULL "
            "AND ilan_tarihi IS NOT NULL "
            "AND ilan_tarihi >= now() - make_interval(days => %s)",
            [_BACKFILL_DAYS],
        )


def noop(apps, schema_editor):
    """Geri alma: alan zaten düşürüleceği için veri temizliğine gerek yok."""


class Migration(migrations.Migration):

    atomic = False  # ⚠️ CONCURRENTLY transaction içinde çalışamaz — kaldırmayın

    dependencies = [
        ("ekap", "0009_missing_detail_partial_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="tender",
            name="ilan_gorunur_at",
            # İndeks ayrı adımda CONCURRENTLY kurulur (dolu tabloda kilit almamak için).
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(stamp_recent, noop),
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(fields=["ilan_gorunur_at"], name="ekap_tender_ilan_gor_idx"),
        ),
    ]
