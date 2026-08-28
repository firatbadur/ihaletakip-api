"""
Keyword katmanı indeksleri — **doldurma bittikten SONRA** çalıştırılmalı.

⚠️ `atomic = False` ve `CREATE INDEX CONCURRENTLY`: `ekap_tender` (1M satır) ve
`ekap_contract` (880k+) canlı tablolar; düz `CREATE INDEX` ACCESS EXCLUSIVE ile
yazmaları kilitler ve ingest görevleri sürekli çalışıyor. CONCURRENTLY transaction
içinde çalışamaz → rollback YOKTUR; `PgAddIndexConcurrently` yarım kalan indeksi
tanıyıp temizleyerek yeniden denenebilir olmasını sağlar (bkz. `_pg_ops`).

⚠️ **CONCURRENTLY açık uzun transaction'ları BEKLER.** Migration boyunca admin →
Periodic Tasks'tan `ekap-sync-contractors`, `ekap-backfill` ve keyword görevlerini
geçici kapatın; aksi hâlde saatlerce asılı kalabilir.

⚠️ Sıra bilinçli: 0020 kolonları ekledi (indekssiz) → `backfill_tender_kalip` +
`propagate` 1M satırı doldurdu → indeksler ŞİMDİ kuruluyor. Ters sırada 1M satırlık
UPDATE indeksleri şişirirdi.

Bitince: `ANALYZE ekap_tender; ANALYZE ekap_tenderkeyword; ANALYZE ekap_contract;`
(ağır UPDATE sonrası istatistik tazelemek, 2026-08 ölçümünde 729 → 335 ms fark
yaratmıştı — indeks eklemekten önce düşünülmesi gereken şey budur.)
"""
from django.db import migrations, models

from ._pg_ops import PgAddIndexConcurrently


class Migration(migrations.Migration):
    # CONCURRENTLY transaction içinde çalışamaz.
    atomic = False

    dependencies = [
        ("ekap", "0021_contract_sektor"),
    ]

    operations = [
        # Kalıp sözlüğü aramasının sıcak yolu: ingest her yeni ihalede bir kez sorgular.
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(fields=["kalip_hash"], name="ekap_tender_kalip_idx"),
        ),
        # Sektör bazlı listeleme/filtreleme. ⚠️ Sıralama kolonu bileşiğin İKİNCİ
        # elemanı: baş kolon yapılsaydı "ORDER BY tarih DESC LIMIT N + seçici filtre"
        # tuzağının kendisi olurdu (bkz. CLAUDE.md, üç kez ısırdı).
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(fields=["sektor", "-ihale_tarihi"],
                               name="ekap_tender_sektor_tarih_idx"),
        ),
        # Benchmark'ın sektör kademesi — `Contract` üzerinde, JOIN'siz.
        PgAddIndexConcurrently(
            model_name="contract",
            index=models.Index(fields=["sektor", "-sozlesme_tarihi"],
                               name="ekap_contract_sektor_tarih_idx"),
        ),
    ]
