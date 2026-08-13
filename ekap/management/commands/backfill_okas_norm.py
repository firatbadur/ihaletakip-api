"""
`OkasItem.adi_norm`'u geriye dönük doldurur (Türkçe-güvenli OKAS adı araması).

⚠️ **Neden Python'da eşleme, SQL'de `translate()` değil:** `normalize_tr` ile birebir
aynı sonucu vermek zorunlu — en ufak sapma aramayı sessizce bozar (bazı kayıtlar
bulunamaz, hata da vermez). SQL'de taklit etmek yerine eşleme Python'da hesaplanıp
geçici tabloya yazılır; `normalize_tr`'nin kendisi kullanıldığı için sapma imkânsızdır.

⚠️ **Neden satır satır değil:** 1,2 milyon satır var ama farklı OKAS **adı** yalnızca
birkaç bin. Eşlemeyi bir kez hesaplayıp geçici tabloyla join etmek, satır başına Python
çağrısından iki mertebe hızlıdır.

PK aralığıyla partlanır: tek dev `UPDATE` uzun bir transaction açar, `CREATE INDEX
CONCURRENTLY`'yi bekletir ve autovacuum'u geciktirir.

Kullanım:
    python manage.py backfill_okas_norm [--batch 200000] [--dry-run]

Yeniden çalıştırılabilir: yalnızca değeri farklı olan satırlara dokunur.
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection

from ekap.utils import normalize_tr


class Command(BaseCommand):
    help = "OkasItem.adi_norm'u normalize_tr ile doldurur."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=200000, help="PK aralığı adımı")
        parser.add_argument("--dry-run", action="store_true", help="yazma, sadece say")

    def handle(self, *args, **opts):
        batch = opts["batch"]

        with connection.cursor() as cur:
            cur.execute("SELECT DISTINCT adi FROM ekap_okasitem WHERE adi <> ''")
            adlar = [r[0] for r in cur.fetchall()]

        if not adlar:
            self.stdout.write("OKAS kalemi yok.")
            return

        eslem = [(a, normalize_tr(a)) for a in adlar]
        self.stdout.write(f"Farklı OKAS adı: {len(eslem):,}")

        if opts["dry_run"]:
            with connection.cursor() as cur:
                cur.execute("SELECT count(*) FROM ekap_okasitem WHERE adi <> '' AND adi_norm = ''")
                self.stdout.write(f"Doldurulacak satır: {cur.fetchone()[0]:,}")
            for a, n in eslem[:5]:
                self.stdout.write(f"  örnek: {a[:50]!r} → {n[:50]!r}")
            return

        basla = time.monotonic()
        toplam = 0
        with connection.cursor() as cur:
            # Geçici tablo bağlantı ömrü boyunca yaşar; her partta yeniden kurulmaz.
            # ⚠️ `ON COMMIT PRESERVE ROWS` YAZMAYIN: Postgres'te zaten varsayılan ve
            # SQLite'ta sözdizimi hatası verir (yerel geliştirme DATABASE_URL yoksa
            # SQLite'a düşer).
            cur.execute("CREATE TEMP TABLE okas_norm_map (adi text PRIMARY KEY, norm text)")
            cur.executemany(
                "INSERT INTO okas_norm_map (adi, norm) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                eslem,
            )
            cur.execute("ANALYZE okas_norm_map")

            cur.execute("SELECT COALESCE(min(id),0), COALESCE(max(id),0) FROM ekap_okasitem")
            lo, hi = cur.fetchone()
            self.stdout.write(f"PK aralığı {lo:,}..{hi:,} · adım {batch:,}")

            start = lo
            while start <= hi:
                end = start + batch
                # ⚠️ Hedef tabloya TAKMA AD verilmiyor: Postgres kabul eder ama
                # SQLite etmez (yerel geliştirme DATABASE_URL yoksa SQLite'a düşer ve
                # duman testi orada koşuyor). `<>` yeterli — `adi_norm` NOT NULL.
                cur.execute(
                    "UPDATE ekap_okasitem SET adi_norm = m.norm "
                    "  FROM okas_norm_map m "
                    " WHERE ekap_okasitem.adi = m.adi "
                    "   AND ekap_okasitem.id >= %s AND ekap_okasitem.id < %s "
                    # Yalnızca gerçekten değişecek satır: gereksiz satır sürümü üretme
                    "   AND ekap_okasitem.adi_norm <> m.norm",
                    [start, end],
                )
                toplam += cur.rowcount
                self.stdout.write(
                    f"  {start:>10,}..{end:>10,} → toplam {toplam:,} satır "
                    f"· {time.monotonic() - basla:6.1f} sn"
                )
                start = end

        self.stdout.write(self.style.SUCCESS(f"Bitti: {toplam:,} satır güncellendi."))
        self.stdout.write("⚠️ Şimdi indeksi kurun: migrate ekap 0019")
