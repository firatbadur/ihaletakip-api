"""Admin panosu şablon etiketleri (yalnızca `templates/admin/index.html` kullanır)."""
import logging

from django import template

from core.dashboard import bicimle, panel_metrikleri, sparkline_path

logger = logging.getLogger(__name__)
register = template.Library()


@register.simple_tag(takes_context=True)
def dashboard_metrics(context):
    """
    Panonun tüm metriklerini döndürür.

    ⚠️ Bu bir template tag'dir, context processor DEĞİL: projedeki her `render()`
    (DRF BrowsableAPI, hata sayfaları...) yerine yalnızca admin anasayfası render
    edilirken çalışır. Yetki kontrolü gerekmez — `AdminSite.index` zaten
    `admin_view()` ile sarılıdır (staff_member_required + never_cache).

    ⚠️ Metrik hesabı ADMİN ANASAYFASINI DÜŞÜREMEZ: Redis/DB kaynaklı her hata
    yutulur ve şablon `hata` bayrağıyla uyarı basıp sayfayı yine de açar.
    """
    request = context.get("request")
    # ⚠️ `?refresh=1` yalnızca superuser'a: aksi halde herhangi bir staff kullanıcı
    # cache-busting ile ağır sorguları istediği kadar tetikleyebilirdi.
    force = bool(
        request
        and request.GET.get("refresh")
        and getattr(request.user, "is_superuser", False)
    )
    try:
        return panel_metrikleri(force_refresh=force)
    except Exception:
        logger.exception("Panel metrikleri hesaplanamadı")
        return {"hata": True}


@register.filter(name="tr_sayi")
def tr_sayi(deger):
    """Türkçe binlik ayracıyla sayı (1.234). Bkz. `core.dashboard.bicimle`."""
    return bicimle(deger)


@register.simple_tag
def sparkline(degerler):
    """Sunucuda üretilen mini trend çizgisinin SVG `d` özniteliği (bkz. dashboard.sparkline_path)."""
    return sparkline_path(list(degerler or []))
