"""
`idare_id`si boş ihaleleri idare ADINDAN eşleştirir (mobil kaynaklı kayıtlar için).

Mobil API `idare_id` vermediği için yeni ihaleler bu alan boş kaydediliyor; komut
`ekap.mobil.idare.coz` ile geriye dönük doldurur. ⚠️ Yöntem kusurludur (tam ad
%97,9 · trigram ≥0,90 %92,2) → hangi satırın tahminle dolduğu `idare_kaynak`
kolonuna yazılır ve **yalnızca boş alanlar** doldurulur: v2'den gelmiş gerçek bir
id asla ezilmez.

Saf DB işidir; EKAP'a hiç gidilmez.
"""
from django.core.management.base import BaseCommand

from ekap.mobil import idare as idare_mod
from ekap.models import Tender
from ekap.series import series_key


class Command(BaseCommand):
    help = "Boş idare_id'leri idare adından eşleştirir"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=100000)
        parser.add_argument("--batch", type=int, default=500)

    def handle(self, *args, **o):
        qs = (Tender.objects.filter(idare_id="")
              .exclude(idare_adi="")
              .only("id", "idare_adi", "idare_id", "okas_ana_kod", "ihale_adi")
              .order_by("-ihale_tarihi")[:o["limit"]])

        bakilan = tam = benzer = bos = 0
        yigin = []
        for t in qs.iterator(chunk_size=o["batch"]):
            bakilan += 1
            idare_id, kaynak = idare_mod.coz(t.idare_adi)
            if not idare_id:
                bos += 1
                continue
            t.idare_id = idare_id
            t.idare_kaynak = kaynak
            # ⚠️ `seri_anahtar` idare_id'ye bağlı (bkz. `ekap/series.py`) → birlikte
            # güncellenmeli, yoksa tekrar eden ihale tespiti bu kayıtları göremez.
            t.seri_anahtar = series_key(idare_id, t.okas_ana_kod, t.ihale_adi)
            yigin.append(t)
            tam += kaynak == idare_mod.KAYNAK_TAM
            benzer += kaynak == idare_mod.KAYNAK_BENZER
            if len(yigin) >= o["batch"] and not o["dry_run"]:
                Tender.objects.bulk_update(
                    yigin, ["idare_id", "idare_kaynak", "seri_anahtar"])
                yigin = []
        if yigin and not o["dry_run"]:
            Tender.objects.bulk_update(
                yigin, ["idare_id", "idare_kaynak", "seri_anahtar"])

        self.stdout.write(
            f"bakılan={bakilan} tam={tam} benzer={benzer} eşleşmedi={bos}"
            + (" (dry-run, yazılmadı)" if o["dry_run"] else "")
        )
