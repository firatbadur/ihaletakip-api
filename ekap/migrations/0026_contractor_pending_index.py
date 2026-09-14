r"""
`sync_contractors` artımlı sorgusu için KISMİ indeks.

⚠️⚠️ Arıza (üretim, 2026-09-14): sunucu saatlerce ağırlaştı — iowait %90, load 10,
kullanıcılar HTTP 499 (yanıt gelmeden vazgeçme). Sebep tek bir sorguydu:

    SELECT id, ikn, ..., detail_raw FROM ekap_tender
     WHERE detail_raw IS NOT NULL
       AND (contractors_synced_at IS NULL OR contractors_synced_at < detail_synced_at)
     ORDER BY detail_synced_at ASC LIMIT 50000

⚠️ **Yüklenici borcu SIFIRDI** (%2 örneklemede 20.490 satırın 0'ı bayat) — sorgu hiçbir
şey bulmuyordu. Pahalı olan "yok"u kanıtlamaktı: `contractors_synced_at <
detail_synced_at` satır-içi bir karşılaştırma olduğu için düz indeksle süzülemiyor,
`ORDER BY detail_synced_at LIMIT n` ise planlayıcıyı o indeksi yürüyüp her satırı
heap'ten elemeye itiyor. `LIMIT` asla dolmadığından tarama tabloyu **bitirmek zorunda**
→ maliyet eşleşen satır sayısına DEĞİL tablonun tamamına (~1M satır) bağlı.
**Boş sonuç en pahalı hâldir.** ⚠️ `LIMIT`i küçültmek çözmez: ölçüldü, `LIMIT 1000` ile
de >150 sn. (Aynı plan tuzağının üçüncü tekrarı — bkz. 0009 ve CLAUDE.md.)

Süre tur tur tırmanıyordu (`SyncRun`: 8 sn → 54 sn), 300 sn'lik `CELERY_TASK_TIME_LIMIT`
aşılınca **yetim sorgu sarmalı** başladı: Celery süreci öldürüyor ama Postgres backend'i
IO'da bloke olduğu için ölü istemciyi fark etmiyor ve taramaya devam ediyor; Redis kilidi
TTL ile düşünce beat 10 dakikada bir bir yenisini doğuruyor. Sekiz yetim birikti (en
eskisi 1sa32dk), hepsi aynı buffer'lar için boğuşup birbirinin ve arama sorgularının
cache'ini süpürdü. (Yetim sarmalının yapısal panzehiri `settings.DB_STATEMENT_TIMEOUT_MS`.)

Kısmi indeks yalnızca yüklenici borcu olan satırları tutar: borç sıfırken indeks de
boştur, sorgu mikrosaniyede biter; borç varken de `detail_synced_at` sırası bedava gelir.

⚠️ **İndeks koşulu sorgunun `WHERE`'iyle BİREBİR aynı olmalı** — planlayıcı kısmi
indeksi ancak sorgu koşulunun indeks koşulunu ima ettiğini kanıtlayabilirse kullanır ve
kanıt yapısal eşitlikle yürür. `ekap/models.py`'deki koşulu değiştiren, `tasks.py`'deki
sorguyu da aynı şekilde değiştirmelidir; aksi hâlde indeks sessizce devre dışı kalır.
Doğrulama: `EXPLAIN` çıktısında `ekap_tender_firmabekleyen` üzerinde Index Scan görünmeli.

⚠️ `atomic = False` + CONCURRENTLY + yeniden çalıştırılabilirlik: bkz. `_pg_ops`.
⚠️ CONCURRENTLY açık uzun transaction'ları BEKLER; kurulum asılırsa admin → Periodic
Tasks'tan ingest görevlerini geçici kapatın (bkz. CLAUDE.md, 0007 notu).
"""

from django.db import migrations, models

from ._pg_ops import PgAddIndexConcurrently


class Migration(migrations.Migration):

    atomic = False  # ⚠️ CONCURRENTLY transaction içinde çalışamaz — kaldırmayın

    dependencies = [
        ("ekap", "0025_mobil_idare_kaynak"),
    ]

    operations = [
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                fields=["detail_synced_at"],
                name="ekap_tender_firmabekleyen",
                condition=models.Q(detail_raw__isnull=False)
                & (
                    models.Q(contractors_synced_at__isnull=True)
                    | models.Q(contractors_synced_at__lt=models.F("detail_synced_at"))
                ),
            ),
        ),
    ]
