"""
Admin ekranı: bekleyen EKAP mobil CAPTCHA'sını gösterip operatör cevabını alır.

⚠️ **Neden var:** OCR `EKAP_MOBIL_CAPTCHA_DENEME` kez tutmazsa toplama durur. Sistemin
o noktada **sessizce durmaması** tasarımın bir parçası: resim buraya düşer, bir kişi
yazar, akış kaldığı yerden devam eder. SSH'sız yol budur (SSH'lı yol
`manage.py mobil_captcha`).

⚠️ Yalnızca **staff**: `admin.site.admin_view` ile sarılır (bkz. `config/urls.py`).
"""
import logging

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse

from . import captcha as captcha_mod
from . import throttle
from .client import EkapMobilClient, MobilError

logger = logging.getLogger("ihaletakip")


def captcha_ekrani(request):
    cli = EkapMobilClient()

    if request.method == "POST":
        eylem = request.POST.get("eylem")
        try:
            if eylem == "yeni":
                veri = cli.captcha_getir()
                captcha_mod.bekleyen_kaydet(
                    str(veri.get("captchaId") or ""), str(veri.get("captchaImage") or "")
                )
                messages.info(request, "Yeni captcha alındı.")
            elif eylem == "ocr":
                bekleyen = captcha_mod.bekleyen_oku()
                tahmin = captcha_mod.ocr_coz(bekleyen["resim"]) if bekleyen else ""
                messages.info(request, f"OCR tahmini: {tahmin or '(çözemedi)'}")
            else:
                cevap = (request.POST.get("cevap") or "").strip()
                if not cevap:
                    messages.error(request, "Cevap boş olamaz.")
                elif captcha_mod.insan_cevapla(cli, cevap):
                    messages.success(request, "Captcha doğrulandı, toplama devam edecek.")
                else:
                    # ⚠️ Reddedilen captchaId yeniden kullanılamaz → yeni tur gerekir.
                    messages.error(request, "Cevap reddedildi. Yeni captcha alın.")
        except (MobilError, ValueError) as e:
            messages.error(request, str(e))
        except Exception as e:                          # noqa: BLE001
            logger.exception("captcha ekranı hatası")
            messages.error(request, f"Beklenmeyen hata: {e}")
        return HttpResponseRedirect(reverse("ekap_mobil_captcha"))

    bekleyen = captcha_mod.bekleyen_oku()
    return render(request, "admin/ekap/mobil_captcha.html", {
        "title": "EKAP Mobil CAPTCHA",
        "bekleyen": bekleyen,
        "durum": captcha_mod.bekleyen_durum(),
        "geri_cekilme": captcha_mod.bekliyor_mu(),
        "butce": throttle.butce_ozet(),
    })
