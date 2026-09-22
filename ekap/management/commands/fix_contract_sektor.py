"""
`Contract.sektor` ingest-kopyasını geçmişe doldurur (tek seferlik onarım).

## Sorun

`sektor` keyword katmanıyla birlikte `Contract`'a **ingest-kopyası** olarak eklendi
(`sync._CONTRACT_FIELDS`) ve fiyat analizi merdivenindeki "Aynı sektör" kademesi
(`benchmark._merdiven` → `Kademe("sektor", …, Q(sektor=tender.sektor))`) bu kolona
bakıyor. Kopya yalnızca **yazma yolunda** doluyor: bir ihalenin sözleşmeleri ancak
detayı yeniden senkronlandığında güncelleniyor. Keyword katmanı arşivin tamamına
sonradan yayıldığı için geçmiş sözleşmelere hiç uğranmadı.

Üretimde ölçüldü (2026-09-22, tinyfect):

    Contract toplam            : 1.412.457
    sektor dolu                :       553   (%0,04)
    ihalesinde sektor var, yok : 1.354.944   ← bu komutun hedefi

⚠️ **Belirti sessizdir**: kademe hata vermez, yalnızca **hiç eşleşmez** ve merdiven
bir sonraki (daha genel, daha alakasız) kademeye düşer. Yani "Aynı sektör" kademesi
yazıldığı günden beri pratikte ölüydü.

## Ne yapar

`Tender.sektor`'ü, sektörü boş olan sözleşmelere kopyalar. **Saf DB işi** —
`detail_raw` okumaz, TOAST'a dokunmaz, EKAP'a gitmez, gece penceresi gerekmez.

⚠️ **Yalnızca BOŞ olanı doldurur**: dolu bir kopya ezilmez (ingest yolundan gelmiş
güncel değer, ihalenin o anki değerinden daha doğru olabilir).

⚠️ **PK imleçli ve parçalı** — tek bir dev `UPDATE ... FROM` yazılamaz:
`settings.DB_STATEMENT_TIMEOUT_MS` (240 sn) onu iptal eder ve işin tamamı boşa gider
(bkz. CLAUDE.md → "Yetim sorgu sarmalı"). Her parça ayrı deyimdir, kesilirse
kaldığı yerden devam edilir.

Kullanım:
    python manage.py fix_contract_sektor --dry-run
    python manage.py fix_contract_sektor
    python manage.py fix_contract_sektor --max-seconds 600
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection

from ekap.models import Contract

PARCA = 20_000


class Command(BaseCommand):
    help = "Contract.sektor kolonunu Tender.sektor'den doldurur (tek seferlik onarım)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Yalnızca sayar, yazmaz")
        parser.add_argument("--parca", type=int, default=PARCA,
                            help=f"Tur başına PK aralığı (vars. {PARCA})")
        parser.add_argument("--max-seconds", type=int, default=0,
                            help="Süre bütçesi (0 = sınırsız)")
        parser.add_argument("--from-pk", type=int, default=0,
                            help="Bu PK'dan itibaren başla")

    def handle(self, *args, **o):
        yaz = self.stdout.write

        hedef = Contract.objects.filter(sektor="").exclude(tender__sektor="")
        toplam = hedef.count()
        yaz(self.style.MIGRATE_HEADING("\n═══ Contract.sektor ONARIMI ═══"))
        yaz(f"  doldurulacak satır : {toplam:,}")
        if not toplam:
            yaz(self.style.SUCCESS("  Doldurulacak satır yok."))
            return
        if o["dry_run"]:
            yaz(self.style.WARNING("  --dry-run: hiçbir şey yazılmadı."))
            return

        if connection.vendor != "postgresql":
            # SQLite'a düşen yerel geliştirmede ORM yolu kullanılır; üretim yolu
            # aşağıdaki tek deyimlik UPDATE ... FROM'dur.
            yaz(self.style.WARNING("  PostgreSQL değil — ORM yoluna düşülüyor."))

        son_pk = Contract.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
        imlec = o["from_pk"]
        yazilan = 0
        basla = time.monotonic()
        butce = o["max_seconds"]

        while imlec <= son_pk:
            ust = imlec + o["parca"]
            with connection.cursor() as cur:
                # ⚠️ PK aralığı ile sınırlı: planlayıcı `ekap_contract_pkey` üzerinde
                # dar bir range scan yapar, tablo taraması olmaz.
                cur.execute(
                    """
                    UPDATE ekap_contract c
                       SET sektor = t.sektor
                      FROM ekap_tender t
                     WHERE c.tender_id = t.id
                       AND c.id >= %s AND c.id < %s
                       AND c.sektor = ''
                       AND t.sektor <> ''
                    """,
                    [imlec, ust],
                )
                yazilan += cur.rowcount or 0
            imlec = ust

            gecen = time.monotonic() - basla
            if yazilan and (imlec // o["parca"]) % 10 == 0:
                yaz(f"  …pk<{imlec:,} · yazılan {yazilan:,} ({gecen:.0f} sn)")
            if butce and gecen >= butce:
                yaz(self.style.WARNING(
                    f"  Süre bütçesi doldu. Kaldığı yer: --from-pk {imlec}"))
                break

        yaz(self.style.SUCCESS(
            f"\n  yazılan satır : {yazilan:,}  ({time.monotonic() - basla:.0f} sn)"))
        kalan = Contract.objects.filter(sektor="").exclude(tender__sektor="").count()
        yaz(f"  kalan          : {kalan:,}")
        yaz("")
