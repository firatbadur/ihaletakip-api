"""
Kalıp üretim kuralı değiştiğinde etkilenen kayıtları **hedefli** düzeltir.

⚠️ `kalip_norm`/`kalip_hash` mantığı her değiştiğinde arşivdeki kalıplar bayatlar.
Naif çözüm tüm arşivi yeniden taramaktır (1M satır, ~2,5 saat). Gerek yok: kural
değişiklikleri tipik olarak kayıtların %1-2'sini etkiliyor. Bu komut mevcut 669k
kalıbı bellekte yeniden hesaplar (saniyeler), yalnızca DEĞİŞENLERİ bulur ve sadece
onlara bağlı ihalelere dokunur.

⚠️ `durum="ok"` kalıplara DOKUNULMAZ: AI'dan geçmiş, parası ödenmiş iştir. Kural
değişikliği yüzünden onları çöpe atmak, tasarruf etmeye çalıştığımız paradan fazlasına
mal olur. Bu yüzden komut **AI'ya gönderilmeden ÖNCE** çalıştırılmalıdır.

    python manage.py fix_kalip_kural --dry-run
    python manage.py fix_kalip_kural
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from ekap import keywords as kw
from ekap.models import Tender, TenderNamePattern


class Command(BaseCommand):
    help = "Kalıp kuralı değiştikten sonra bayatlayan kalıpları yeniden hesaplar."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        yaz = self.stdout.write
        kuru = o["dry_run"]

        yaz("  mevcut kalıplar taranıyor…")
        bayat, korunan = [], 0
        for p in TenderNamePattern.objects.only("id", "kalip_hash", "kalip_norm",
                                                "ornek_ad", "durum").iterator(chunk_size=5000):
            # Kalıbı ÖRNEK ADDAN yeniden üret — `kalip_norm` zaten işlenmiş metindir,
            # onu tekrar işlemek kuralın tamamını sınamaz.
            kaynak = p.ornek_ad or p.kalip_norm
            yeni = kw.kalip_hash(kaynak)
            if yeni and yeni != p.kalip_hash:
                if p.durum == "ok":
                    korunan += 1          # ödenmiş iş — dokunma
                else:
                    bayat.append(p)

        yaz(f"  bayat kalıp        : {len(bayat):,}")
        if korunan:
            yaz(self.style.WARNING(
                f"  ⚠ {korunan:,} kalıp bayat ama AI'dan geçmiş — korunuyor"))
        if not bayat:
            yaz(self.style.SUCCESS("  Yapılacak bir şey yok."))
            return

        hashler = [p.kalip_hash for p in bayat]
        ihaleler = list(Tender.objects.filter(kalip_hash__in=hashler)
                        .only("pk", "ihale_adi", "kalip_hash"))
        yaz(f"  etkilenen ihale    : {len(ihaleler):,}")

        yeni_kalip, degisen = {}, []
        for t in ihaleler:
            h = kw.kalip_hash(t.ihale_adi)
            if h and h != t.kalip_hash:
                t.kalip_hash = h
                degisen.append(t)
                yeni_kalip.setdefault(h, (kw.kalip_norm(t.ihale_adi),
                                          (t.ihale_adi or "")[:500]))

        yaz(f"  kalıbı değişen ihale: {len(degisen):,}")
        yaz(f"  oluşacak yeni kalıp : {len(yeni_kalip):,}")
        yaz(self.style.SUCCESS(
            f"  → net kazanç: ~{max(len(bayat) - len(yeni_kalip), 0):,} daha az AI sorusu"))

        if kuru:
            yaz("\n  örnek dönüşümler:")
            for t in degisen[:8]:
                yaz(f"    {t.ihale_adi[:60]}\n      → {yeni_kalip[t.kalip_hash][0]}")
            yaz(self.style.WARNING("\n  --dry-run: hiçbir şey yazılmadı."))
            return

        with transaction.atomic():
            Tender.objects.bulk_update(degisen, ["kalip_hash"], batch_size=1000)
            TenderNamePattern.objects.bulk_create(
                [TenderNamePattern(kalip_hash=h, kalip_norm=n, ornek_ad=ad,
                                   durum="pending")
                 for h, (n, ad) in yeni_kalip.items()],
                ignore_conflicts=True, batch_size=1000,
            )
            # Artık hiçbir ihalenin kullanmadığı bayat kalıpları sil — AI'ya
            # gönderilmesinler. `durum="ok"` olanlar zaten `bayat` listesinde yok.
            silinen = (TenderNamePattern.objects
                       .filter(kalip_hash__in=hashler)
                       .exclude(kalip_hash__in=list(yeni_kalip))
                       .exclude(durum="ok")
                       .delete()[0])
        yaz(self.style.SUCCESS(
            f"\n  ✓ {len(degisen):,} ihale güncellendi · {len(yeni_kalip):,} yeni kalıp · "
            f"{silinen:,} bayat kalıp silindi"))
        yaz("  ⚠️ Şimdi sayaçları tazele: run_keywords --job sayac")
