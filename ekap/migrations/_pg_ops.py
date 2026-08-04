r"""
Migration yardımcıları — PostgreSQL'e özel, **yeniden çalıştırılabilir** indeks işlemleri.

⚠️ Dosya adı `_` ile başlar: Django'nun migration yükleyicisi bu dosyaları migration
saymaz, yani burası ortak kod için güvenli bir yerdir.

İki sorunu birden çözer:

1. **SQLite'ta patlamasın.** `config/settings.py` `DATABASE_URL` yoksa SQLite'a düşer
   (yerel geliştirme) ve `GinIndex` / `CONCURRENTLY` / `CREATE EXTENSION` orada
   desteklenmez. Guard yalnızca **DB işlemini** atlar; migration state her iki tarafta
   da aynı ilerlediği için sonraki migration'lar tutarlı kalır.

2. **Yarıda kalan CONCURRENTLY tekrar denenebilsin.** `CREATE INDEX CONCURRENTLY`
   transaction içinde çalışamaz → onu kullanan migration `atomic = False` olmak
   zorundadır → **rollback YOKTUR**. Deploy yarışı, timeout, OOM ya da Ctrl-C
   durumunda bir kısım indeks oluşmuş olur ama migration kaydı yazılmaz; tekrar
   `migrate` çalıştırınca `relation ... already exists` ile patlardı ve elle
   `DROP INDEX` gerekirdi. Buradaki sınıflar her indeksi önce katalogda arar:
     • zaten var ve GEÇERLİ  → atlanır (kaldığı yerden devam)
     • var ama GEÇERSİZ      → yarım kalmış CONCURRENTLY kalıntısıdır; düşürülüp
                                yeniden kurulur (geçersiz indeks sorguda kullanılmaz
                                ama yazmaları yavaşlatır)
   Bu, üretimde bir kez yaşanan hatanın (bkz. CLAUDE.md "atomic=False + CONCURRENTLY")
   tekrarını önler.
"""
from django.contrib.postgres.operations import (
    AddIndexConcurrently,
    RemoveIndexConcurrently,
    TrigramExtension,
)
from django.db import migrations


def index_state(schema_editor, name):
    """`(var_mi, gecerli_mi)` — indeks katalogda var mı ve `indisvalid` mi."""
    with schema_editor.connection.cursor() as cur:
        cur.execute(
            "SELECT i.indisvalid FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = %s AND c.relkind = 'i'",
            [name],
        )
        row = cur.fetchone()
    return (row is not None, bool(row[0]) if row else False)


class _PostgresOnly:
    """
    PostgreSQL dışında hiçbir DB işlemi yapmayan operasyon karışımı.

    `CreateExtension` (dolayısıyla `TrigramExtension`) bu kontrolü `database_forwards`'ta
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
    """Yeniden çalıştırılabilir `AddIndexConcurrently` — bkz. modül docstring'i."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor != "postgresql":
            return
        name = self.index.name
        exists, valid = index_state(schema_editor, name)
        if exists and valid:
            print(f"  ↷ {name} zaten var, atlanıyor")
            return
        if exists and not valid:
            print(f"  ⚠ {name} GEÇERSİZ (yarım kalmış) — düşürülüp yeniden kuruluyor")
            schema_editor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class PgRemoveIndexConcurrently(_PostgresOnly, RemoveIndexConcurrently):
    """Yeniden çalıştırılabilir `RemoveIndexConcurrently` — indeks yoksa sessizce geçer."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor != "postgresql":
            return
        exists, _ = index_state(schema_editor, self.name)
        if not exists:
            print(f"  ↷ {self.name} zaten yok, atlanıyor")
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class PgTrigramExtension(_PostgresOnly, TrigramExtension):
    """`pg_trgm` — `CreateExtension` zaten `IF NOT EXISTS` mantığıyla idempotent."""


class PgRunSQL(_PostgresOnly, migrations.RunSQL):
    """Yalnızca PostgreSQL'de çalışan ham SQL (SQLite'ta sessizce atlanır)."""


def lock_timeout(saniye="5s"):
    """
    `SET LOCAL lock_timeout` — dolu tabloda `ALTER TABLE` için **zorunlu koruma**.

    Nullable kolon eklemek PG11+'da metadata-only'dir (tablo yeniden yazılmaz) ama yine
    de kısa bir ACCESS EXCLUSIVE kilidi ister. O kilit, uzun süren bir transaction'ın
    (ör. gece koşan `sync_contractors` süpürmesi) arkasında **kuyruğa girerse**, kendisi
    de sonraki tüm okuyucuları arkasına dizer → migration beklerken site durur.
    `lock_timeout` ile migration kilidi alamazsa hızlıca hata verir; deploy tekrar
    denenir, kimse bloklanmaz.

    `SET LOCAL` transaction kapsamlıdır → migration `atomic = True` OLMALIDIR.
    """
    return PgRunSQL(f"SET LOCAL lock_timeout = '{saniye}'", migrations.RunSQL.noop)
