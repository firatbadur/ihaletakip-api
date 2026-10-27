"""IhaleTakip API — kök URL yapılandırması."""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from accounts.views_eposta import eposta_abonelik
from blog.views import blog_medya
from core.views import health_check
from ekap.admin_views import sektor_ekrani
from ekap.mobil.admin_views import captcha_ekrani

admin.site.site_header = "IhaleTakip Yönetim"
admin.site.site_title = "IhaleTakip API"
admin.site.index_title = "Yönetim Paneli"

api_v1 = [
    path("auth/", include("accounts.urls")),
    path("", include("tenders.urls")),
    path("ai/", include("ai.urls")),
    path("assistant/", include("assistant.urls")),
    path("ekap/", include("ekap.urls")),
    path("subscription/", include("subscription.urls")),
    path("blog/", include("blog.urls")),
    path("", include("core.urls")),
]

urlpatterns = [
    # ⚠️ `admin/` include'undan ÖNCE: aksi hâlde admin URL çözücüsü yakalar ve 404 verir.
    # `admin_view` sarmalayıcısı staff kontrolünü ve giriş yönlendirmesini sağlar.
    # ⚠️ `sektor`ün kayıtlı bir modeli YOK (kapalı taksonomi, ayrı tablosu yok) →
    # admin'de hazır liste ekranı da yok. Bu sayfa onun yerine geçer.
    path(
        "admin/ekap/sektorler/",
        admin.site.admin_view(sektor_ekrani),
        name="ekap_sektorler",
    ),
    path(
        "admin/ekap/mobil-captcha/",
        admin.site.admin_view(captcha_ekrani),
        name="ekap_mobil_captcha",
    ),
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health"),
    # Blog kapak görselleri (web portalı). ⚠️ Yalnızca `media/blog/` — başka medya
    # bilinçli olarak dışarı açılmaz.
    path("media/blog/<path:yol>", blog_medya, name="blog-medya"),
    # Günlük e-posta bülteninden çıkma (e-postadaki bağlantı + List-Unsubscribe).
    path("eposta/abonelik/", eposta_abonelik, name="eposta-abonelik"),
    path("api/v1/", include((api_v1, "v1"))),
    # API şema & dokümantasyon
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
