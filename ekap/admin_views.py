"""
Admin ekranı: sektör dağılımı.

⚠️ **Neden ayrı bir sayfa:** `sektor` kapalı bir taksonomidir ve **kendi tablosu
yoktur** (bkz. `sektor_ozet`), dolayısıyla `admin.site.register` ile gelen hazır bir
liste ekranı da yoktur. Kayıtlı bir model olmadan kenar çubuğunda da görünemez →
`config/urls.py`'de `admin.site.admin_view` ile bağlanır (captcha ekranıyla aynı
desen) ve üst menüye `JAZZMIN_SETTINGS["topmenu_links"]` ile eklenir.

⚠️ Yalnızca **staff**: yetki `admin_view` sarmalayıcısındadır, burada tekrarlanmaz.
"""
from django.shortcuts import render

from . import sektor_ozet


def sektor_ekrani(request):
    # ⚠️ `?yenile=1` sayfayı her açılışta değil, **istendiğinde** hesaplatır. Ölçülen
    # maliyet yarım saniyenin altında ama üç tabloya birden GROUP BY atıyor; sekmeyi
    # açık unutan bir operatörün bunu dakikada bir tetiklemesi gereksiz yük olurdu.
    veri = sektor_ozet.ozet(yenile=request.GET.get("yenile") == "1")
    return render(request, "admin/ekap/sektorler.html", {
        **veri,
        "title": "Sektör Dağılımı",
    })
