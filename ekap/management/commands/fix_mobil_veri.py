"""
Mobil kaynaklı satırlarda **sonradan düzeltilen** iki alanı geriye dönük onarır.

1. **İlan HTML'i** — mobil API sayısal karakter referansı döndürüyor
   (`SA&#286;LIK` = "SAĞLIK"); v2 aynı belgeyi düz UTF-8 veriyor. Aynı kolonda iki
   kodlama, HTML olarak render etmeyen her yerde metni okunamaz gösteriyordu.
2. **Açıklama metinleri** — mobil ham metni v2'ninkinden farklı
   ("İhale İlanı Yayımlanmış/İlansız, Katılıma Açık" ↔ "İhale İlanı Yayımlanmış,
   Katılıma Açık"). Mobil uygulama bu alanı doğrudan gösterdiği için kaynağa göre
   değişmesi kullanıcıya görünen bir tutarsızlık.

İkisi de ingest'te düzeltildi; bu komut **daha önce yazılmış** satırlar içindir.
Saf DB işidir: EKAP'a hiç gidilmez.
"""
from django.core.management.base import BaseCommand

from ekap.mobil import constants as C
from ekap.mobil.adapt import ILAN_ONEK, html_normalize
from ekap.models import Announcement, Tender


class Command(BaseCommand):
    help = "Mobil kaynaklı ilan HTML'i ve açıklama metinlerini onarır"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch", type=int, default=500)

    def handle(self, *args, **o):
        self._html(o)
        self._aciklamalar(o)

    def _html(self, o):
        qs = Announcement.objects.filter(
            ekap_ilan_id__startswith=ILAN_ONEK
        ).only("id", "veri_html").iterator(chunk_size=o["batch"])
        bakilan = onarilan = 0
        yigin = []
        for a in qs:
            bakilan += 1
            yeni = html_normalize(a.veri_html or "")
            if yeni != (a.veri_html or ""):
                a.veri_html = yeni
                yigin.append(a)
                onarilan += 1
            if len(yigin) >= o["batch"] and not o["dry_run"]:
                Announcement.objects.bulk_update(yigin, ["veri_html"])
                yigin = []
        if yigin and not o["dry_run"]:
            Announcement.objects.bulk_update(yigin, ["veri_html"])
        self.stdout.write(f"ilan html : bakılan={bakilan} onarılan={onarilan}")

    def _aciklamalar(self, o):
        # ⚠️ Yalnızca mobil kaynaklı satırlar: v2'nin metni zaten kanoniktir.
        temel = Tender.objects.filter(detay_kaynak="mobil")
        toplam = 0
        for kod, metin in C.DURUM_ACIKLAMA.items():
            n = temel.filter(ihale_durum=kod).exclude(ihale_durum_aciklama=metin)
            toplam += n.count() if o["dry_run"] else n.update(
                ihale_durum_aciklama=metin)
        for kod, metin in C.TIP_ACIKLAMA.items():
            n = temel.filter(ihale_tip=kod).exclude(ihale_tipi_aciklama=metin)
            toplam += n.count() if o["dry_run"] else n.update(
                ihale_tipi_aciklama=metin)
        for kod, metin in C.USUL_ACIKLAMA.items():
            n = temel.filter(ihale_usul=kod).exclude(ihale_usul_aciklama=metin)
            toplam += n.count() if o["dry_run"] else n.update(
                ihale_usul_aciklama=metin)
        # ⚠️ **Kapsam KODU da onarılır**, yalnızca metin değil: ilk sürümde
        # `İstisna` ↔ `Kapsam Dışı` kodları ters yazılmıştı (2 ↔ 3) ve mobil
        # kaynaklı kayıtlar v2'den farklı kod taşıyordu → `yasa_kapsami` filtresi
        # iki kaynağı karıştırıyordu. Doğru kod, kaydın kendi açıklama metninden
        # yeniden türetilir.
        for metin_norm, kod in C.KAPSAM_METIN.items():
            metin = C.KAPSAM_ACIKLAMA.get(kod)
            if not metin:
                continue
            n = temel.filter(ihale_kapsam_aciklama=metin).exclude(yasa_kapsami=kod)
            toplam += n.count() if o["dry_run"] else n.update(yasa_kapsami=kod)
        for kod, metin in C.KAPSAM_ACIKLAMA.items():
            n = temel.filter(yasa_kapsami=kod).exclude(ihale_kapsam_aciklama=metin)
            toplam += n.count() if o["dry_run"] else n.update(
                ihale_kapsam_aciklama=metin)
        self.stdout.write(
            f"açıklama  : güncellenen={toplam}"
            + (" (dry-run, yazılmadı)" if o["dry_run"] else "")
        )
