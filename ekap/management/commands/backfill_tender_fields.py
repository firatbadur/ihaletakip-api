"""
Pro sinyal kolonlarını elle doldurur (Celery gerekmez, EKAP'a gitmez).

`ekap.tasks.backfill_tender_fields` görevinin ikizi; **aynı `tender_fields`
checkpoint'ini paylaşır** → arka arkaya çalıştırınca kaldığı yerden devam eder ve her
turda kalan ihale sayısını basar.

Görevden farkı: gece penceresi ve "yüklenici süpürmesine öncelik" kuralı UYGULANMAZ.
Elle çalıştırmak bilinçli bir eylemdir; ne zaman koşacağına siz karar verirsiniz.
⚠️ Ama aynı sebep hâlâ geçerli: `detail_raw` (~40 KB/satır) taraması Postgres buffer
cache'ini boşaltıp ihale aramasını yavaşlatır. Yoğun saatte büyük `--limit` vermeyin.

Kullanım:
    python manage.py backfill_tender_fields --dry-run --limit 20   # yazmadan incele
    python manage.py backfill_tender_fields --limit 5000           # partiler hâlinde
    python manage.py backfill_tender_fields --from-pk 120000       # imleci yok say
    python manage.py backfill_tender_fields --restart              # baştan tara
"""
import time

from django.core.management.base import BaseCommand

from ekap import sync as sync_mod
from ekap.models import SyncCheckpoint, Tender


class Command(BaseCommand):
    help = "Pro sinyal kolonlarını detail_raw arşivinden doldurur (offline)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1000,
                            help="Bu çalışmada işlenecek azami ihale (vars. 1000).")
        parser.add_argument("--from-pk", type=int, default=None,
                            help="Bu Tender pk'sinden itibaren başla (imleci yok sayar).")
        parser.add_argument("--restart", action="store_true",
                            help="İmleci sıfırla, arşivi baştan tara.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Hiçbir şey yazma, ne çıkacağını göster.")
        parser.add_argument("--batch", type=int, default=500,
                            help="bulk_update parti boyutu (vars. 500).")

    def handle(self, *args, **o):
        cp, _ = SyncCheckpoint.objects.get_or_create(name="tender_fields")
        if o["restart"]:
            cp.extra = {**(cp.extra or {}), "last_tender_pk": 0}
            cp.done = False
            cp.save()
            self.stdout.write("imleç sıfırlandı")

        last_pk = (
            o["from_pk"] if o["from_pk"] is not None
            else int((cp.extra or {}).get("last_tender_pk") or 0)
        )

        qs = (
            Tender.objects.filter(detail_raw__isnull=False, pk__gt=last_pk)
            .only("pk", "detail_raw", "idare_id", "ihale_adi")
            .order_by("pk")[:o["limit"]]
        )

        basla = time.monotonic()
        islenen = hata = 0
        batch = []
        ornekler = []

        for tender in qs.iterator(chunk_size=200):
            last_pk = max(last_pk, tender.pk)
            try:
                sync_mod.apply_pro_fields(
                    tender, (tender.detail_raw or {}).get("item", {})
                )
                batch.append(tender)
                islenen += 1
                if len(ornekler) < 5:
                    ornekler.append(tender)
            except Exception as e:
                hata += 1
                self.stderr.write(f"  atlandı pk={tender.pk}: {e}")

            if not o["dry_run"] and len(batch) >= o["batch"]:
                Tender.objects.bulk_update(batch, sync_mod.PRO_TENDER_FIELDS)
                batch = []

        if batch and not o["dry_run"]:
            Tender.objects.bulk_update(batch, sync_mod.PRO_TENDER_FIELDS)

        if ornekler:
            self.stdout.write("\nörnekler:")
            for t in ornekler:
                self.stdout.write(
                    f"  {t.ikn:16s} okas={t.okas_ana_kod or '-':>10s} "
                    f"bakanlik={t.en_ust_idare_kod or '-':>3s} "
                    f"istekli={t.istekli_sayisi if t.istekli_sayisi is not None else '-':>4} "
                    f"seri={(t.seri_anahtar or '-')[:10]}"
                )

        if not o["dry_run"]:
            cp.extra = {**(cp.extra or {}), "last_tender_pk": last_pk}
            cp.save()

        kalan = Tender.objects.filter(detail_raw__isnull=False, pk__gt=last_pk).count()
        sure = time.monotonic() - basla
        self.stdout.write(self.style.SUCCESS(
            f"\n{'[DRY-RUN] ' if o['dry_run'] else ''}işlenen={islenen} hata={hata} "
            f"süre={sure:.1f}sn ({islenen / sure if sure else 0:.0f} ihale/sn) "
            f"imleç={last_pk} kalan={kalan}"
        ))
