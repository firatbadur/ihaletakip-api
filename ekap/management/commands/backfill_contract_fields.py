"""
`Contract.okas_ana_kod` / `okas_bucket` / `en_ust_idare_kod` ingest-kopyalarını
`Tender`'dan geriye dönük doldurur.

⚠️ **Neden ham SQL, Python döngüsü değil:** 1,4 milyon satırlık bir join-update'i
ORM ile satır satır yürütmek saatler sürerdi. `UPDATE ... FROM` tek geçişte yapar;
PK aralığıyla partlara bölünür ki her transaction kısa kalsın (uzun transaction
`CREATE INDEX CONCURRENTLY`'yi bekletir ve autovacuum'u geciktirir).

⚠️ **`detail_raw` OKUNMAZ** — bu saf DB-içi bir kolon kopyalamadır, `sync_contractors`
süpürmesinin TOAST yükü burada yoktur. Gece penceresi gerekmez.

Kullanım:
    python manage.py backfill_contract_fields [--batch 50000] [--dry-run]

Yeniden çalıştırılabilir: yalnızca kaynağı dolu ve hedefi boş satırlara dokunur,
yani yarıda kesilirse kaldığı yerden devam eder.
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection


SQL = """
UPDATE ekap_contract c
   SET okas_ana_kod     = t.okas_ana_kod,
       okas_bucket      = t.okas_bucket,
       en_ust_idare_kod = t.en_ust_idare_kod
  FROM ekap_tender t
 WHERE t.id = c.tender_id
   AND c.id >= %s AND c.id < %s
   -- Yalnızca gerçekten değişecek satırlar: gereksiz satır sürümü üretme
   -- (her UPDATE yeni tuple yazar → tablo şişer, autovacuum yükü artar).
   AND (c.okas_ana_kod     IS DISTINCT FROM t.okas_ana_kod
     OR c.okas_bucket      IS DISTINCT FROM t.okas_bucket
     OR c.en_ust_idare_kod IS DISTINCT FROM t.en_ust_idare_kod)
"""


class Command(BaseCommand):
    help = "Contract OKAS/bakanlık ingest-kopyalarını Tender'dan doldurur."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=50000, help="PK aralığı adımı")
        parser.add_argument("--dry-run", action="store_true", help="yazma, sadece say")

    def handle(self, *args, **opts):
        batch = opts["batch"]
        dry = opts["dry_run"]

        with connection.cursor() as cur:
            cur.execute("SELECT COALESCE(min(id),0), COALESCE(max(id),0) FROM ekap_contract")
            lo, hi = cur.fetchone()

        if hi == 0:
            self.stdout.write("Sözleşme yok.")
            return

        self.stdout.write(f"PK aralığı {lo}..{hi} · adım {batch}" + (" · DRY-RUN" if dry else ""))
        if dry:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM ekap_contract c JOIN ekap_tender t ON t.id=c.tender_id "
                    "WHERE c.okas_ana_kod IS DISTINCT FROM t.okas_ana_kod "
                    "   OR c.okas_bucket IS DISTINCT FROM t.okas_bucket "
                    "   OR c.en_ust_idare_kod IS DISTINCT FROM t.en_ust_idare_kod"
                )
                self.stdout.write(f"Güncellenecek satır: {cur.fetchone()[0]:,}")
            return

        toplam = 0
        basla = time.monotonic()
        start = lo
        while start <= hi:
            end = start + batch
            with connection.cursor() as cur:
                cur.execute(SQL, [start, end])
                toplam += cur.rowcount
            gecen = time.monotonic() - basla
            self.stdout.write(
                f"  {start:>10,}..{end:>10,} → toplam {toplam:,} satır · {gecen:6.1f} sn"
            )
            start = end

        self.stdout.write(self.style.SUCCESS(f"Bitti: {toplam:,} satır güncellendi."))
        self.stdout.write("⚠️ Şimdi indeksleri kurun: migrate ekap 0015")
