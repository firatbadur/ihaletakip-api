"""
Kısımlı ihalelerde şişmiş indirim oranlarını temizler (tek seferlik onarım).

## Sorun

Sonuç İlanı ayrıştırıcısı, kısımlı bir ihalede kısmın maliyeti yerine ihalenin
**tamamının** maliyetini yakalayabiliyor. O değer tek bir kısma atfedilince indirim
oranı yapay olarak 1'e yaklaşıyor: 10 kısımlı ihalede toplam YM 10M iken bir kısmı
1M'ye alan firma için `(10−1)/10 = 0,9`.

Üretimde ölçüldü (220.248 sözleşme, 2026-08):

    çok sözleşmeli + kısım YM == ihale YM  →  19.509 satır, ortalama indirim 0,88,
                                              %86,6'sı >%70   ← BOZUK
    tek sözleşmeli + kısım YM == ihale YM  →   8.431 satır, ortalama 0,14,
                                              yalnızca %2,7'si >%70   ← MEŞRU

Tek sözleşmeli ihalede eşitlik normaldir (kısım zaten ihalenin kendisi) — bu komut
onlara **dokunmaz**.

## Ne yapar

Bozuk satırlarda kısım maliyetini "bilinmiyor" sayar: `yaklasik_maliyet_num` ve
`indirim_orani` `NULL`, `yaklasik_maliyet_kaynak` boş. İhalenin toplam maliyeti
(`tender_yaklasik_maliyet_num`) DOĞRUdur ve korunur.

**Saf DB işi** — `detail_raw` okumaz, TOAST'a dokunmaz, gece penceresi gerekmez.

`sync.py`'deki ingest yolu da düzeltildi, yani yeni/tazelenen kayıtlar zaten doğru
yazılıyor; bu komut yalnızca **geçmişi** onarır. (Süpürme zamanla aynı işi yapar ama
imleci geçtiği satırlara dönmez, o yüzden tek seferlik onarım gerekli.)

Kullanım:
    python manage.py fix_indirim_orani --dry-run
    python manage.py fix_indirim_orani
"""
from django.core.management.base import BaseCommand
from django.db.models import Count, F, OuterRef, Subquery

from ekap.models import Contract


class Command(BaseCommand):
    help = "Kısımlı ihalelerde şişmiş indirim oranlarını temizler (tek seferlik)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Yazma; kaç satırın etkileneceğini göster.")
        parser.add_argument("--batch", type=int, default=2000,
                            help="Güncelleme parti boyutu (vars. 2000).")

    def handle(self, *args, **o):
        # İhale başına sözleşme sayısı — `cok_sozlesmeli` koşulunun DB karşılığı.
        kardes_sayisi = (
            Contract.objects.filter(tender_id=OuterRef("tender_id"))
            .order_by()
            .values("tender_id")
            .annotate(n=Count("*"))
            .values("n")
        )
        bozuk = (
            Contract.objects.filter(
                yaklasik_maliyet_num__isnull=False,
                yaklasik_maliyet_num=F("tender_yaklasik_maliyet_num"),
            )
            .annotate(kardes=Subquery(kardes_sayisi))
            .filter(kardes__gt=1)
        )

        pks = list(bozuk.values_list("pk", flat=True))
        self.stdout.write(f"bozuk satır: {len(pks)}")

        if not pks:
            self.stdout.write(self.style.SUCCESS("temizlenecek satır yok ✓"))
            return

        if o["dry_run"]:
            ornek = list(
                Contract.objects.filter(pk__in=pks[:5]).values(
                    "tender__ikn", "yaklasik_maliyet_num",
                    "tender_yaklasik_maliyet_num", "sozlesme_bedeli_num", "indirim_orani",
                )
            )
            for r in ornek:
                self.stdout.write(
                    f"  {r['tender__ikn']:16s} kısım_ym={r['yaklasik_maliyet_num']} "
                    f"ihale_ym={r['tender_yaklasik_maliyet_num']} "
                    f"bedel={r['sozlesme_bedeli_num']} indirim={r['indirim_orani']}"
                )
            self.stdout.write(self.style.WARNING(
                f"[DRY-RUN] {len(pks)} satır temizlenecekti (yazılmadı)"
            ))
            return

        guncellenen = 0
        for i in range(0, len(pks), o["batch"]):
            guncellenen += Contract.objects.filter(pk__in=pks[i:i + o["batch"]]).update(
                yaklasik_maliyet_num=None,
                yaklasik_maliyet_kaynak="",
                indirim_orani=None,
            )
        self.stdout.write(self.style.SUCCESS(
            f"{guncellenen} satır temizlendi ✓ "
            "(ihale toplam maliyeti korundu; kısım maliyeti artık 'bilinmiyor')"
        ))
