"""
Ay adı içeren kalıpları yeniden hesaplar — hedefli düzeltme.

⚠️ Neden hedefli: `_KALIP_GURULTU` listesine ay adları eklendi, yani `kalip_hash`
üretimi değişti. Bütün arşivi yeniden taramak (~2,5 saat) gereksiz — değişen yalnızca
**ay adı içeren** kalıplar ve onlara bağlı ihaleler (üretimde ~%1). Bu komut yalnızca
onlara dokunur, dakikalar sürer.

Sırası önemli: AI'ya gönderilmeden ÖNCE çalıştırılmalı. Sonra çalıştırılırsa aynı iş
için hem eski hem yeni kalıba para ödenmiş olur.

    python manage.py fix_kalip_ay --dry-run     # ne değişecek, yazmadan göster
    python manage.py fix_kalip_ay
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction

from ekap import keywords as kw
from ekap.models import Tender, TenderNamePattern

AY_DESEN = re.compile(
    r"(^| )(ocak|subat|mart|nisan|mayis|haziran|temmuz|agustos|eylul|ekim|kasim|aralik)( |$)"
)


class Command(BaseCommand):
    help = "Ay adı içeren kalıpları yeni kurala göre yeniden hesaplar."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        yaz = self.stdout.write
        kuru = o["dry_run"]

        eski = [p for p in TenderNamePattern.objects.only("id", "kalip_hash", "kalip_norm",
                                                          "durum", "ihale_sayisi")
                if AY_DESEN.search(p.kalip_norm)]
        yaz(f"  ay adı içeren kalıp: {len(eski):,}")
        if not eski:
            return

        islenmis = [p for p in eski if p.durum == "ok"]
        if islenmis:
            yaz(self.style.WARNING(
                f"  ⚠ {len(islenmis)} kalıp ZATEN AI'dan geçmiş — onlara dokunulmuyor "
                "(ödenmiş iş çöpe atılmaz)."))
        hedef = [p for p in eski if p.durum != "ok"]
        hashler = [p.kalip_hash for p in hedef]
        yaz(f"  yeniden hesaplanacak kalıp: {len(hedef):,}")

        ihaleler = list(Tender.objects.filter(kalip_hash__in=hashler)
                        .only("pk", "ihale_adi", "kalip_hash"))
        yaz(f"  etkilenen ihale: {len(ihaleler):,}")

        yeni_hash, degisen = {}, []
        for t in ihaleler:
            h = kw.kalip_hash(t.ihale_adi)
            if h and h != t.kalip_hash:
                t.kalip_hash = h
                degisen.append(t)
                yeni_hash[h] = kw.kalip_norm(t.ihale_adi)
        yaz(f"  kalıbı değişen ihale: {len(degisen):,}")
        yaz(f"  oluşacak yeni kalıp : {len(yeni_hash):,} "
            f"(net kazanç ~{len(hedef) - len(yeni_hash):,} daha az AI sorusu)")

        if kuru:
            yaz("\n  örnek dönüşümler:")
            for t in degisen[:8]:
                yaz(f"    {t.ihale_adi[:58]}\n      → {yeni_hash[t.kalip_hash]}")
            yaz(self.style.WARNING("\n  --dry-run: hiçbir şey yazılmadı."))
            return

        with transaction.atomic():
            Tender.objects.bulk_update(degisen, ["kalip_hash"], batch_size=1000)
            TenderNamePattern.objects.bulk_create(
                [TenderNamePattern(kalip_hash=h, kalip_norm=n, ornek_ad=n,
                                   durum="pending")
                 for h, n in yeni_hash.items()],
                ignore_conflicts=True, batch_size=1000,
            )
            # Artık hiçbir ihalenin kullanmadığı eski kalıpları sil — AI'ya
            # gönderilmemeleri için. ⚠️ Yalnızca `durum != "ok"` olanlar (yukarıda
            # `hedef` böyle seçildi); işlenmiş kalıplar korunur.
            silinen = (TenderNamePattern.objects
                       .filter(kalip_hash__in=hashler)
                       .exclude(kalip_hash__in=list(yeni_hash))
                       .exclude(durum="ok")
                       .delete()[0])
        yaz(self.style.SUCCESS(f"\n  ✓ {len(degisen):,} ihale güncellendi, "
                               f"{len(yeni_hash):,} yeni kalıp, {silinen:,} eski kalıp silindi"))
        yaz("  ⚠️ Şimdi sayaçları tazele: run_keywords --job sayac")
