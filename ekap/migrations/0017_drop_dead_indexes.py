r"""
Ölü indeks temizliği — **prod'da kanıtlandıktan sonra** (`scripts/index_audit.sql`).

Denetim (2026-08-13): `pg_stat_database.stats_reset` **NULL**, yani sayaçlar veritabanı
kurulduğundan beri birikiyor ve `idx_scan = 0` gerçekten "hiç kullanılmadı" demektir
(çok kullanılan indekslerde 7 milyon tarama görülüyor, sayaçlar sağlıklı).

⚠️ **Denetimin listelediği 16 adayın yalnızca 4'ü düşürülüyor.** `idx_scan = 0` tek
başına yeterli kanıt değildir; kalanların bilinçli gerekçeleri var:

| Düşürülmeyen | Neden kalıyor |
|---|---|
| `ekap_contract_bakanlik_idx` | **Bugün kuruldu** (0015); idare profilinin bakanlık kapsamı henüz sorgulanmadı |
| `ekap_tender_ozellik_gin` | `e_ihale` Pro filtresi bunu kullanır (CLAUDE.md'de bilinçli tercih) |
| `ekap_contra_toplam_*` / `_son_soz_*` / `_kind_*` | Firma listesinin `ORDER BY` anahtarları — mobil henüz çağırmadı |
| `ekap_okasitem_adi_trgm` (75 MB) | `okas_adi` filtresi `icontains` kullandığı için indeksi ZATEN kullanamıyor (`UPPER()` kolonun üstünde). Doğru çözüm indeksi silmek değil **filtreyi normalize desenine çevirmek** — ayrı iş |

Düşürülenler (**202 MB**), hepsi başka bir indeksin kapsadığı ya da hiç sorgulanmayan
alanlar:

- `Contract.ekap_sozlesme_id` (+`_like`, 138 MB) — kararlı upsert anahtarıdır ama
  `_bulk_upsert_children` satırları `tender.sozlesmeler.all()` ile çekip **Python'da**
  eşleştirir; DB'ye hiç `WHERE ekap_sozlesme_id = …` sorgusu gitmez.
  ⚠️ İleride koşulsuz bir `UNIQUE(tender, ekap_sozlesme_id)` kısıtı düşünülüyor
  (bkz. CLAUDE.md "sentetik `noid:{i}`"); bu düşürme onu engellemez, kısıt kendi
  indeksini yaratır.
- `Contract.yuklenici_adi_norm` (+`_like`, 64 MB) — metin araması `ekap_contract_yukadi_trgm`
  (GIN trigram) üzerinden gider; `%…%` deseni için btree zaten işe yaramaz.

⚠️ `atomic = False` + `DROP INDEX CONCURRENTLY`: düz `DROP INDEX` ACCESS EXCLUSIVE
kilidi alır ve sürekli koşan ingest görevlerinin arkasına kuyruğa girerse tüm
okuyucuları bloklar. CONCURRENTLY transaction içinde çalışamaz → rollback YOK.

⚠️ Django, `db_index=True` olan metin alanları için Postgres'te **iki** indeks yaratır
(düz + `varchar_pattern_ops` `_like`). `AlterField(db_index=False)` ikisini de düşürür;
burada state ile database işlemleri `SeparateDatabaseAndState` ile ayrılıp DB tarafı
CONCURRENTLY yapılıyor.
"""
from django.db import migrations, models

from ._pg_ops import PgRunSQL


def _drop(*adlar):
    return [PgRunSQL(f"DROP INDEX CONCURRENTLY IF EXISTS {ad};", reverse_sql=migrations.RunSQL.noop)
            for ad in adlar]


class Migration(migrations.Migration):

    atomic = False  # ⚠️ CONCURRENTLY transaction içinde çalışamaz — kaldırmayın

    dependencies = [
        ("ekap", "0016_market_stats"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="contract",
                    name="ekap_sozlesme_id",
                    field=models.CharField(blank=True, max_length=64),
                ),
                migrations.AlterField(
                    model_name="contract",
                    name="yuklenici_adi_norm",
                    field=models.CharField(blank=True, max_length=500),
                ),
            ],
            database_operations=_drop(
                "ekap_contract_ekap_sozlesme_id_91714052",
                "ekap_contract_ekap_sozlesme_id_91714052_like",
                "ekap_contract_yuklenici_adi_norm_5067a706",
                "ekap_contract_yuklenici_adi_norm_5067a706_like",
            ),
        ),
    ]
