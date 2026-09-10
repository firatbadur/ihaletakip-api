"""
EKAP Mobil CAPTCHA — operatör (insan-döngü) yolu.

Argümansız çalıştırılırsa bekleyen captcha'nın durumunu basar ve resmi bir dosyaya
yazar; `--cevap` ile cevap gönderilir.

⚠️ Bu, otomatik OCR çözümünün **yedeğidir**: OCR `EKAP_MOBIL_CAPTCHA_DENEME` kez
tutmazsa toplama durur ve buraya düşer. Sistem sessizce durmaz — bkz.
`ekap/mobil/captcha.py`.
"""
import base64
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ekap.mobil import captcha as captcha_mod
from ekap.mobil.client import EkapMobilClient, MobilError


class Command(BaseCommand):
    help = "Bekleyen EKAP mobil captcha'sını gösterir / operatör cevabını gönderir"

    def add_arguments(self, parser):
        parser.add_argument("--cevap", help="Resimdeki 6 karakter")
        parser.add_argument("--kaydet", default="/tmp/ekap_mobil_captcha.png",
                            help="Resmin yazılacağı dosya")
        parser.add_argument("--yeni", action="store_true",
                            help="Bekleyen yoksa yeni bir captcha iste")
        parser.add_argument("--ocr", action="store_true",
                            help="Bekleyen captcha'yı OCR ile çözmeyi dene")

    def handle(self, *args, **o):
        cli = EkapMobilClient()

        if o["cevap"]:
            try:
                ok = captcha_mod.insan_cevapla(cli, o["cevap"])
            except ValueError as e:
                raise CommandError(str(e))
            if ok:
                self.stdout.write(self.style.SUCCESS("Captcha doğrulandı, toplama devam edebilir."))
            else:
                # ⚠️ Reddedilen captchaId yeniden kullanılamaz → yeni tur gerekir.
                self.stdout.write(self.style.ERROR(
                    "Cevap reddedildi. `--yeni` ile yeni bir captcha isteyin."
                ))
            return

        bekleyen = captcha_mod.bekleyen_oku()
        if not bekleyen and o["yeni"]:
            try:
                veri = cli.captcha_getir()
            except MobilError as e:
                raise CommandError(str(e))
            captcha_mod.bekleyen_kaydet(
                str(veri.get("captchaId") or ""), str(veri.get("captchaImage") or "")
            )
            bekleyen = captcha_mod.bekleyen_oku()

        if not bekleyen:
            self.stdout.write("Bekleyen captcha yok.")
            self.stdout.write(f"Durum: {captcha_mod.bekleyen_durum() or '(kayıt yok)'}")
            return

        yol = Path(o["kaydet"])
        try:
            yol.write_bytes(base64.b64decode(bekleyen["resim"]))
            self.stdout.write(f"Resim yazıldı: {yol}")
        except Exception as e:                          # noqa: BLE001
            self.stderr.write(self.style.WARNING(f"Resim yazılamadı: {e}"))

        self.stdout.write(f"captchaId : {bekleyen['captchaId']}")
        self.stdout.write(f"alındı    : {bekleyen.get('ts', '?')}")
        if o["ocr"]:
            tahmin = captcha_mod.ocr_coz(bekleyen["resim"])
            self.stdout.write(f"OCR tahmini: {tahmin or '(çözemedi)'}")
        self.stdout.write("Cevap göndermek için: manage.py mobil_captcha --cevap XXXXXX")
