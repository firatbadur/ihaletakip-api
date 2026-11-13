"""
Admin ekranları: sektör dağılımı · EKAP v2 doğrulaması (Turnstile çözücü).

⚠️ **Neden ayrı bir sayfa:** `sektor` kapalı bir taksonomidir ve **kendi tablosu
yoktur** (bkz. `sektor_ozet`), dolayısıyla `admin.site.register` ile gelen hazır bir
liste ekranı da yoktur. Kayıtlı bir model olmadan kenar çubuğunda da görünemez →
`config/urls.py`'de `admin.site.admin_view` ile bağlanır (captcha ekranıyla aynı
desen) ve üst menüye `JAZZMIN_SETTINGS["topmenu_links"]` ile eklenir.

⚠️ Yalnızca **staff**: yetki `admin_view` sarmalayıcısındadır, burada tekrarlanmaz.
"""
import logging

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse

from . import sektor_ozet

logger = logging.getLogger("ihaletakip")


def sektor_ekrani(request):
    # ⚠️ `?yenile=1` sayfayı her açılışta değil, **istendiğinde** hesaplatır. Ölçülen
    # maliyet yarım saniyenin altında ama üç tabloya birden GROUP BY atıyor; sekmeyi
    # açık unutan bir operatörün bunu dakikada bir tetiklemesi gereksiz yük olurdu.
    veri = sektor_ozet.ozet(yenile=request.GET.get("yenile") == "1")
    return render(request, "admin/ekap/sektorler.html", {
        **veri,
        "title": "Sektör Dağılımı",
    })


def dogrulama_ekrani(request):
    """EKAP v2 insan doğrulaması: çözücü durumu + operatör müdahalesi.

    Eylemler (POST `eylem`):
      - `coz`            → servise "şimdi çöz" isteği (≤5 sn içinde alır; geri çekilmeyi ezer)
      - `otomatik_ac` / `otomatik_kapat`
      - `cerez_kaydet`   → elle yapıştırılan Cookie başlığı (tarayıcı F12 → Network)
      - `durum_sor`      → kayıtlı çerezle EKAP'a canlı sorar (yalnızca istenince)

    ⚠️ GET EKAP'a istek ATMAZ (pano kuralı); canlı sorgu yalnızca "EKAP'a sor" düğmesiyle.
    """
    from . import session as ekap_session
    from . import turnstile

    kim = getattr(request.user, "username", "") or "pano"
    if request.method == "POST":
        eylem = request.POST.get("eylem")
        try:
            if eylem == "coz":
                turnstile.istek_birak(kim)
                messages.info(request, "Çözüm istendi — servis birkaç saniye içinde deneyecek. "
                                       "Sonuç için sayfayı yenileyin.")
            elif eylem == "otomatik_ac":
                turnstile.otomatik_ayarla(True, kim)
                messages.success(request, "Otomatik çözücü açıldı.")
            elif eylem == "otomatik_kapat":
                turnstile.otomatik_ayarla(False, kim)
                messages.warning(request, "Otomatik çözücü kapatıldı. Çerez süresi dolunca "
                                          "v2 görevleri durur.")
            elif eylem == "cerez_kaydet":
                temiz = ekap_session.kaydet(request.POST.get("cerez") or "")
                turnstile.olay_ekle("ayar", f"elle çerez kaydedildi ({kim}) — {ekap_session.maskele(temiz)}")
                d = ekap_session.durum(cerezli=True)
                if d.get("verified") and d.get("expiresAtUtc"):
                    turnstile.bitis_guncelle(d["expiresAtUtc"])
                    messages.success(request, f"Çerez kaydedildi ve GEÇERLİ (bitiş {d['expiresAtUtc'][:16]}Z).")
                else:
                    messages.warning(request, f"Çerez kaydedildi ama EKAP doğrulanmış saymıyor "
                                              f"(HTTP {d.get('http')}, verified={d.get('verified')}).")
            elif eylem == "durum_sor":
                d = ekap_session.durum(cerezli=True)
                if d.get("http") != 200:
                    messages.error(request, f"EKAP'a ulaşılamadı: HTTP {d.get('http')} {d.get('hata', '')}")
                elif d.get("enabled") is False:
                    messages.info(request, "EKAP şu an doğrulama İSTEMİYOR (çerezsiz çalışılıyor).")
                elif d.get("verified"):
                    turnstile.bitis_guncelle(d.get("expiresAtUtc"))
                    messages.success(request, f"Kayıtlı çerez GEÇERLİ — bitiş {str(d.get('expiresAtUtc'))[:16]}Z.")
                else:
                    messages.error(request, "Kayıtlı çerez GEÇERSİZ — 'Şimdi çöz'e basın.")
            else:
                messages.error(request, "Bilinmeyen eylem.")
        except ValueError as e:
            messages.error(request, str(e))
        except Exception as e:                       # noqa: BLE001
            logger.exception("doğrulama ekranı hatası")
            messages.error(request, f"Beklenmeyen hata: {e}")
        return HttpResponseRedirect(reverse("ekap_dogrulama"))

    gerekli = ekap_session.dogrulama_gerekli(canli=False)
    if gerekli is None:
        gerekli, _ = ekap_session.son_bilinen_gerekli()
    return render(request, "admin/ekap/dogrulama.html", {
        "title": "EKAP Doğrulama (Turnstile)",
        "oz": turnstile.pano_ozeti(),
        "olaylar": [
            {**o, "zaman": turnstile._iso(o.get("zaman"))} for o in turnstile.olaylar()
        ],
        "ekran": turnstile.ekran_oku(),
        "gerekli": gerekli,
        "cerez_ozet": ekap_session.maskele(ekap_session.cerez()),
        "istek_bekliyor": bool(turnstile.cache.get(turnstile._ISTEK)),
    })
