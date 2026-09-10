"""
Mobil kaynaklı ilan HTML'lerindeki karakter referanslarını onarır (tek seferlik).

⚠️ **Neden gerekli:** mobil API `ilanHtml`'i sayısal karakter referanslarıyla
döndürüyor (`SA&#286;LIK` = "SAĞLIK"); v2 aynı belgeyi düz UTF-8 veriyor. Normalize
etme `adapt.html_normalize` ile ingest'e eklendi, ama **daha önce yazılmış** satırlar
ham hâlde kaldı — onlar yalnızca ihale yeniden senkronlanırsa düzelirdi.

Saf DB işidir: EKAP'a hiç gidilmez.
"""
from django.core.management.base import BaseCommand

from ekap.mobil.adapt import ILAN_ONEK, html_normalize
from ekap.models import Announcement


class Command(BaseCommand):
    help = "Mobil kaynaklı ilan HTML'lerindeki &#NNN; referanslarını çözer"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch", type=int, default=500)

    def handle(self, *args, **o):
        # ⚠️ Yalnızca mobil kaynaklı satırlar: v2'nin `veriHtml`'i zaten UTF-8 ve
        # orada `&#...;` görülüyorsa o EKAP'ın kendi kodlamasıdır, dokunulmaz.
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

        self.stdout.write(
            f"bakılan={bakilan} onarılan={onarilan}"
            + (" (dry-run, yazılmadı)" if o["dry_run"] else "")
        )
