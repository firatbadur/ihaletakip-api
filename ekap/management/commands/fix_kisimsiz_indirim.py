"""
Kısımsız tek sözleşmeli ihalelerde boş kalan sözleşme yaklaşık maliyetini + indirim
oranını doldurur (tek seferlik onarım, 2026-10-28).

## Sorun

Kısımsız ihalenin Sonuç İlanı "Sözleşmeye Esas Kısımlarının Yaklaşık Maliyeti" satırını
taşımaz → `Contract.yaklasik_maliyet_num` ve `indirim_orani` hiç dolmuyordu. Ölçüldü:
2024+ tek sözleşmeli + sonuç ilanlı sözleşmelerin yalnızca ~%3,5'inde indirim vardı.
Kural ve ölçüm: `sync._kisimsiz_tek_sozlesme` (canlı senkron aynı fonksiyonu kullanır).

## Ne yapar

Sonuç ilanına bağlı, kısım maliyeti boş, ihale maliyeti (`tender_yaklasik_maliyet_num`)
dolu, ihalesi kısımsız olduğu BİLİNEN (`kismi_ihale=False`) ve ihalede TEK sözleşme olan
satırlara ihale maliyetini yazar; kaynak `ihale_ym` (geri alınabilir), indirim
`sync._indirim_orani` ile. Dokunulan firmaların agregaları yeniden hesaplanır.

**Saf DB işi** — `detail_raw` okumaz. PK aralıklı parçalar (tek dev UPDATE
`DB_STATEMENT_TIMEOUT_MS`'e takılırdı); kesilirse `--from-pk` ile devam.

Geri alma: `Contract.objects.filter(yaklasik_maliyet_kaynak="ihale_ym").update(
yaklasik_maliyet_num=None, yaklasik_maliyet_kaynak="", indirim_orani=None)`.

Kullanım:
    python manage.py fix_kisimsiz_indirim --dry-run
    python manage.py fix_kisimsiz_indirim [--from-pk N] [--parca 5000]
"""
import time

from django.core.management.base import BaseCommand
from django.db.models import Count, OuterRef, Subquery

from ekap import contractors as contractors_mod
from ekap.models import Contract
from ekap.sync import YM_KAYNAK_IHALE, _indirim_orani, _kisimsiz_tek_sozlesme


class Command(BaseCommand):
    help = "Kısımsız tek sözleşmeli ihalelerde sözleşme YM + indirim oranını doldurur."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--from-pk", type=int, default=0, dest="from_pk")
        parser.add_argument("--parca", type=int, default=5000)

    def handle(self, *args, **o):
        kardes = (Contract.objects.filter(tender_id=OuterRef("tender_id")).order_by()
                  .values("tender_id").annotate(n=Count("*")).values("n"))
        adaylar = (Contract.objects
                   .filter(yaklasik_maliyet_num__isnull=True,
                           tender_yaklasik_maliyet_num__isnull=False,
                           tender__kismi_ihale=False)
                   .exclude(ekap_ilan_id=""))
        son_pk = Contract.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
        bas, yazilan, bakilan, firmalar = o["from_pk"], 0, 0, set()
        t0 = time.monotonic()
        ornekler = []
        while bas <= son_pk:
            bit = bas + o["parca"]
            satirlar = list(
                adaylar.filter(pk__gte=bas, pk__lt=bit)
                .annotate(kardes=Subquery(kardes))
                .values_list("pk", "kardes", "tender_yaklasik_maliyet_num",
                             "sozlesme_bedeli_num", "yuklenici_id"))
            guncel = []
            for pk, n, ym, bedel, firma in satirlar:
                bakilan += 1
                if not _kisimsiz_tek_sozlesme(n > 1, False):
                    continue
                ind = _indirim_orani(ym, bedel)
                guncel.append(Contract(pk=pk, yaklasik_maliyet_num=ym,
                                       yaklasik_maliyet_kaynak=YM_KAYNAK_IHALE,
                                       indirim_orani=ind))
                if firma:
                    firmalar.add(firma)
                if len(ornekler) < 5:
                    ornekler.append((pk, ym, bedel, ind))
            if guncel and not o["dry_run"]:
                Contract.objects.bulk_update(
                    guncel, ["yaklasik_maliyet_num", "yaklasik_maliyet_kaynak", "indirim_orani"])
            yazilan += len(guncel)
            bas = bit
            if not o["dry_run"] and yazilan and (bas // o["parca"]) % 20 == 0:
                self.stdout.write(f"  pk<{bas}: {yazilan} satır ({time.monotonic() - t0:.0f} sn)")

        for pk, ym, bedel, ind in ornekler:
            self.stdout.write(f"  örnek pk={pk} ihale_ym={ym} bedel={bedel} indirim={ind}")
        if o["dry_run"]:
            self.stdout.write(self.style.WARNING(
                f"[DRY-RUN] bakılan {bakilan}, yazılacak {yazilan}, firma {len(firmalar)} "
                f"({time.monotonic() - t0:.0f} sn)"))
            return
        if firmalar:
            contractors_mod.recompute_aggregates(firmalar)
        self.stdout.write(self.style.SUCCESS(
            f"{yazilan} sözleşme dolduruldu, {len(firmalar)} firmanın agregası yenilendi "
            f"({time.monotonic() - t0:.0f} sn)"))
