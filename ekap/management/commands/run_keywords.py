"""
Keyword boru hattını elle tetikler — beat beklemeden.

Beat girdileri `DatabaseScheduler` ile DB'ye ancak beat yeniden başlayınca senkronlanır;
ayrıca ilk yüklemede 6 saatlik `dispatch` aralığını beklemek anlamsızdır. Bu komut
görevleri **senkron** çalıştırır (Celery'ye atmaz), yani çıktıyı doğrudan görürsün.

    python manage.py run_keywords --job kalip       # Tender.kalip_hash + kalıp sözlüğü
    python manage.py run_keywords --job dispatch    # AI'ya batch gönder
    python manage.py run_keywords --job poll        # batch durumu
    python manage.py run_keywords --job process     # sonuçları yaz
    python manage.py run_keywords --job propagate   # ihalelere yay
    python manage.py run_keywords --job df          # kullanım sayacı + pasif bayrağı
    python manage.py run_keywords --job durum       # özet rapor (hiçbir şey çalıştırmaz)

⚠️ `--job kalip` ve `--job propagate` süre bütçeli tek TUR çalışır; arşivi bitirmek
için tekrar tekrar çağrılmalıdır (`--tur N` bunu yapar). Bütçe, görev
`CELERY_TASK_TIME_LIMIT`'e takılmasın diye vardır ve elle çağrıda da korunur.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from ekap import tasks


class Command(BaseCommand):
    help = "Keyword boru hattı görevlerini elle çalıştırır."

    ISLER = {
        "kalip": tasks.backfill_tender_kalip,
        "dispatch": tasks.dispatch_keyword_batches,
        "poll": tasks.poll_keyword_batches,
        "process": tasks.process_keyword_results,
        "propagate": tasks.propagate_tender_keywords,
        "df": tasks.refresh_keyword_df,
    }

    def add_arguments(self, parser):
        parser.add_argument("--job", required=True,
                            choices=list(self.ISLER) + ["durum"])
        parser.add_argument("--tur", type=int, default=1,
                            help="Görevi kaç tur çalıştır (kalip/propagate için)")

    def handle(self, *args, **o):
        if o["job"] == "durum":
            return self._durum()

        gorev = self.ISLER[o["job"]]
        for tur in range(1, o["tur"] + 1):
            sonuc = gorev()
            self.stdout.write(f"  tur {tur}: {sonuc}")
            if isinstance(sonuc, dict) and (sonuc.get("done") or sonuc.get("skipped")):
                break

    def _durum(self):
        from ekap.models import (Keyword, KeywordBatch, SyncCheckpoint, Tender,
                                 TenderKeyword, TenderNamePattern)

        yaz = self.stdout.write
        yaz(self.style.MIGRATE_HEADING("\n═══ KEYWORD BORU HATTI DURUMU ═══"))

        yaz(self.style.MIGRATE_HEADING("\n── Kalıp sözlüğü ──"))
        toplam = TenderNamePattern.objects.count()
        yaz(f"  toplam kalıp : {toplam:,}")
        for durum, _ in TenderNamePattern.DURUMLAR:
            n = TenderNamePattern.objects.filter(durum=durum).count()
            if n:
                yaz(f"    {durum:<10}: {n:>9,} (%{100 * n / max(toplam, 1):.1f})")

        yaz(self.style.MIGRATE_HEADING("\n── Batch'ler ──"))
        harcama = tasks._keyword_harcama()
        for b in KeywordBatch.objects.all()[:10]:
            yaz(f"  {b.batch_id[:28]:<28} {b.durum:<12} {b.kalip_sayisi:>7,} kalıp")
        yaz(f"  toplam harcama: ${harcama:.2f} / ${settings.KEYWORD_MAX_TOTAL_USD:.0f} "
            f"tavan")
        if not settings.KEYWORD_AI_ENABLED:
            yaz(self.style.WARNING("  ⚠ KEYWORD_AI_ENABLED=False — dispatch kapalı"))

        yaz(self.style.MIGRATE_HEADING("\n── Keyword ve bağlar ──"))
        kw_n = Keyword.objects.count()
        yaz(f"  tekil keyword : {kw_n:,} "
            f"(pasif: {Keyword.objects.filter(pasif=True).count():,})")
        yaz(f"  ihale bağı    : {TenderKeyword.objects.count():,}")
        yaz(f"  sektörlü ihale: {Tender.objects.exclude(sektor='').count():,}")

        yaz(self.style.MIGRATE_HEADING("\n── İmleçler ──"))
        for ad in ("tender_kalip", "tender_keywords", "contractors", "tender_fields"):
            cp = SyncCheckpoint.objects.filter(name=ad).first()
            if cp:
                yaz(f"  {ad:<18} imleç={(cp.extra or {}).get('last_tender_pk', 0):>9,} "
                    f"done={cp.done}")
            else:
                yaz(f"  {ad:<18} (henüz başlamadı)")
        yaz("")
