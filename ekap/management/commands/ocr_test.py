"""
Base64 resimden metin çıkarımını elle dener (tesseract kurulumunu da doğrular).

Kullanım:
    python manage.py ocr_test --dosya yol/resim.png
    python manage.py ocr_test --base64 "iVBORw0KGgo..."
    cat resim.b64 | python manage.py ocr_test --stdin
    python manage.py ocr_test --dosya kod.png --psm 8 --whitelist "ABC...xyz0-9"
"""
import base64
import sys

from django.core.management.base import BaseCommand, CommandError

from ekap.tools import OCRHatasi, resimden_metin_cikar


class Command(BaseCommand):
    help = "Base64/dosya resimden metin çıkarır (tesseract)."

    def add_arguments(self, parser):
        kaynak = parser.add_mutually_exclusive_group(required=True)
        kaynak.add_argument("--dosya", help="Resim dosyası yolu")
        kaynak.add_argument("--base64", help="Base64 metin (data: öneki olabilir)")
        kaynak.add_argument("--stdin", action="store_true", help="Base64'ü stdin'den oku")
        parser.add_argument("--dil", help="Tesseract dili (tur, eng, tur+eng)")
        parser.add_argument("--psm", type=int, help="Sayfa segmentasyon modu (3/7/8)")
        parser.add_argument("--whitelist", help="Yalnızca bu karakterler tanınsın")
        parser.add_argument("--buyut", type=int, default=2, help="Ön büyütme katsayısı")

    def handle(self, *args, **o):
        if o["dosya"]:
            with open(o["dosya"], "rb") as f:
                veri = base64.b64encode(f.read()).decode("ascii")
        elif o["stdin"]:
            veri = sys.stdin.read()
        else:
            veri = o["base64"]

        try:
            metin = resimden_metin_cikar(
                veri, dil=o["dil"], psm=o["psm"],
                whitelist=o["whitelist"], buyut=o["buyut"],
            )
        except OCRHatasi as e:
            raise CommandError(str(e))

        if not metin:
            self.stdout.write(self.style.WARNING("Metin bulunamadı."))
            return
        self.stdout.write(self.style.SUCCESS(metin))
