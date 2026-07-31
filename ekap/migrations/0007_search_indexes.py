r"""
İhale arama indeksleri — `pg_trgm` GIN + filtre/sıralama bileşikleri.

`0005_search_norm` normalize arama sütunlarını (`Tender.ihale_adi_norm`,
`idare_adi_norm`) eklerken indekssiz bıraktı; bu sütunlar `__contains` (`LIKE '%…%'`)
ile sorgulandığı için düz B-tree zaten yetmezdi — gereken trigram GIN'idir. 500k+
satırda her metin araması tam tablo taramasına dönüşüyordu.

⚠️ **`Tender.ikn` de trigram alır, keyfi değildir.** `q` filtresi üç koşulu OR'lar
(`ihale_adi_norm` | `idare_adi_norm` | `ikn`) ve Postgres BitmapOr'u ancak **her dal**
indekslenebiliyorsa kurar. İKN dalı indekssiz kalsaydı diğer iki trigram indeksi de
kullanılamaz, sorgu yine seq scan olurdu. Bu yüzden `apply_tender_filters` içinde
`ikn__icontains` → `ikn__contains` yapıldı (`icontains` Postgres'te
`UPPER(ikn::text) LIKE …` üretir, kolonun üstündeki `UPPER()` indeksi öldürür).

⚠️ **`atomic = False` ve `AddIndexConcurrently` bilinçlidir.** Dolu tabloda normal
`CREATE INDEX` yazmaları ACCESS EXCLUSIVE ile kilitler; ingest görevleri (`backfill`,
`refresh_stale`, `sync_contractors`) sürekli çalıştığı için bu kabul edilemez.
CONCURRENTLY daha yavaştır ve transaction içinde çalışamaz.

⚠️ **Deploy**: entrypoint'in senkron `migrate`'i `web` healthcheck penceresini (180 sn)
aşarsa konteyner "unhealthy" olur. 500k satırda GIN indeksleri toplam 15-40 dk sürebilir,
bu yüzden CLAUDE.md'deki ağır-migration kuralına uyun — önce elle:
    docker compose exec worker python manage.py migrate ekap
sonra `./install.sh`.

⚠️ **CREATE INDEX CONCURRENTLY açık uzun transaction'ları BEKLER.** `sync_contractors`
süpürmesi koşarken migration saatlerce asılı kalabilir. Migration boyunca beat'ten
`ekap-sync-contractors` ve `ekap-backfill` görevlerini geçici olarak kapatın
(admin → Periodic Tasks).

⚠️ **Yarıda kesilirse rollback YOKTUR** (`atomic=False`) → geçersiz indeks kalabilir:
    SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
    DROP INDEX CONCURRENTLY <ad>;
sonra `migrate ekap` tekrar. Bitince `ANALYZE ekap_tender; ANALYZE ekap_okasitem;`
"""
import django.contrib.postgres.indexes
from django.contrib.postgres.operations import AddIndexConcurrently, TrigramExtension
from django.db import migrations, models


class _PostgresOnly:
    """
    PostgreSQL dışında hiçbir DB işlemi yapmayan operasyon karışımı.

    ⚠️ Gerekçe: `config/settings.py` `DATABASE_URL` yoksa SQLite'a düşer (yerel
    geliştirme) ve `GinIndex` / `CONCURRENTLY` / `CREATE EXTENSION` orada desteklenmez →
    `manage.py migrate` kırılırdı. Guard yalnızca **DB işlemini** atlar; migration state
    her iki tarafta da aynı ilerlediği için sonraki migration'lar tutarlı kalır.

    `CreateExtension` (dolayısıyla `TrigramExtension`) `database_forwards`'ta bu kontrolü
    kendi yapar ama **`database_backwards`'ta YAPMAZ** — geri alma SQLite'ta
    `no such table: pg_extension` ile patlar. Bu yüzden onu da sarmalıyoruz.
    """

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor != "postgresql":
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor != "postgresql":
            return
        super().database_backwards(app_label, schema_editor, from_state, to_state)


class PgAddIndexConcurrently(_PostgresOnly, AddIndexConcurrently):
    pass


class PgTrigramExtension(_PostgresOnly, TrigramExtension):
    pass


def _tune_autovacuum(apps, schema_editor):
    """`ekap_tender` autovacuum eşiklerini sıkılaştır (Postgres'e özel)."""
    if schema_editor.connection.vendor != "postgresql":
        return
    # Varsayılan %20 eşiği 500k satırda ~100k ölü tuple demek → seq scan'ler şişmiş heap
    # okur. `refresh_stale` + `sync_contractors` + backfill sürekli UPDATE atıyor,
    # `0005`'in 505k satırlık bulk_update backfill'i de bir kerelik büyük iz bıraktı.
    # TOAST ayrı ayarlanır: asıl şişen yer `detail_raw`/`list_raw`.
    schema_editor.execute(
        "ALTER TABLE ekap_tender SET ("
        "autovacuum_vacuum_scale_factor = 0.02, "
        "autovacuum_analyze_scale_factor = 0.01, "
        "toast.autovacuum_vacuum_scale_factor = 0.02)"
    )
    schema_editor.execute(
        "ALTER TABLE ekap_contract SET (autovacuum_vacuum_scale_factor = 0.05)"
    )


def _reset_autovacuum(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "ALTER TABLE ekap_tender RESET ("
        "autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, "
        "toast.autovacuum_vacuum_scale_factor)"
    )
    schema_editor.execute(
        "ALTER TABLE ekap_contract RESET (autovacuum_vacuum_scale_factor)"
    )


class Migration(migrations.Migration):

    atomic = False  # ⚠️ CONCURRENTLY transaction içinde çalışamaz — kaldırmayın

    dependencies = [
        ("ekap", "0006_contractoralias_contractormembership_and_more"),
    ]

    operations = [
        # `-ilan_tarihi` indeksi `ilan_tarihi` üzerindeki `db_index=True` btree'sinin
        # birebir kopyasıydı (Postgres btree'yi geriye tarayabilir) → backfill'in her
        # yazmasında iki kat indeks maliyeti ödeniyordu.
        migrations.RemoveIndex(
            model_name="tender",
            name="ekap_tender_ilan_ta_09293b_idx",
        ),
        # `gin_trgm_ops` opclass'ının kaynağı — trigram indekslerinden ÖNCE gelmeli.
        PgTrigramExtension(),
        # ── Metin araması (q / ihale_adi / idare_adi / ikn / okas_adi) ──
        PgAddIndexConcurrently(
            model_name="tender",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["ihale_adi_norm"],
                name="ekap_tender_ihaleadi_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        PgAddIndexConcurrently(
            model_name="tender",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["idare_adi_norm"],
                name="ekap_tender_idareadi_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        PgAddIndexConcurrently(
            model_name="tender",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["ikn"], name="ekap_tender_ikn_trgm", opclasses=["gin_trgm_ops"]
            ),
        ),
        PgAddIndexConcurrently(
            model_name="okasitem",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["adi"], name="ekap_okasitem_adi_trgm", opclasses=["gin_trgm_ops"]
            ),
        ),
        # ── JSONB containment (`ozellik` filtresi → `@>`) ──
        PgAddIndexConcurrently(
            model_name="tender",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["ozellikler"],
                name="ekap_tender_ozellik_gin",
                opclasses=["jsonb_path_ops"],
            ),
        ),
        # ── Filtre + sıralama bileşikleri (liste ucu daima tarihe göre sıralar) ──
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                fields=["ihale_durum", "-ihale_tarihi"], name="ekap_tender_ihale_d_882a4e_idx"
            ),
        ),
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                fields=["il_id", "-ihale_tarihi"], name="ekap_tender_il_id_3be88b_idx"
            ),
        ),
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(
                fields=["idare_id", "-ihale_tarihi"], name="ekap_tender_idare_i_ab1c37_idx"
            ),
        ),
        PgAddIndexConcurrently(
            model_name="tender",
            index=models.Index(fields=["ihale_usul"], name="ekap_tender_ihale_u_abcb04_idx"),
        ),
        # ── OKAS kod araması (`okas_kod` → kodu__startswith, Exists alt sorgusunda) ──
        PgAddIndexConcurrently(
            model_name="okasitem",
            index=models.Index(fields=["kodu", "tender"], name="ekap_okasit_kodu_efba25_idx"),
        ),
        migrations.RunPython(_tune_autovacuum, _reset_autovacuum),
    ]
