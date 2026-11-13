"""IhaleTakip API — kök URL yapılandırması."""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from accounts.views_eposta import eposta_abonelik, ses_bildirim
from blog.views import blog_medya
from core.views import health_check
from core import admin_toplu
from core.admin_views import guvenlik_ekrani
from ekap.admin_views import dogrulama_ekrani, sektor_ekrani
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
        "admin/guvenlik/",
        admin.site.admin_view(guvenlik_ekrani),
        name="core_guvenlik",
    ),
    # Toplu e-posta / push (core/admin_toplu.py) — tamamı süper kullanıcıya açık.
    path("admin/toplu-gonderim/", admin.site.admin_view(admin_toplu.toplu_ekrani),
         name="core_toplu"),
    path("admin/toplu-gonderim/ara/", admin.site.admin_view(admin_toplu.kullanici_ara),
         name="core_toplu_ara"),
    path("admin/toplu-gonderim/sayim/", admin.site.admin_view(admin_toplu.sayim),
         name="core_toplu_sayim"),
    path("admin/toplu-gonderim/onizle/", admin.site.admin_view(admin_toplu.onizle),
         name="core_toplu_onizle"),
    path("admin/toplu-gonderim/test/", admin.site.admin_view(admin_toplu.test_gonder),
         name="core_toplu_test"),
    path("admin/toplu-gonderim/gonder/", admin.site.admin_view(admin_toplu.gonder),
         name="core_toplu_gonder"),
    path("admin/toplu-gonderim/liste/", admin.site.admin_view(admin_toplu.liste),
         name="core_toplu_liste"),
    path("admin/toplu-gonderim/<int:pk>/", admin.site.admin_view(admin_toplu.detay),
         name="core_toplu_detay"),
    path("admin/toplu-gonderim/<int:pk>/<str:ad>/", admin.site.admin_view(admin_toplu.eylem),
         name="core_toplu_eylem"),
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
    # EKAP v2 Turnstile çözücü: durum + müdahale (ekap/turnstile.py).
    path(
        "admin/ekap/dogrulama/",
        admin.site.admin_view(dogrulama_ekrani),
        name="ekap_dogrulama",
    ),
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health"),
    # Blog kapak görselleri (web portalı). ⚠️ Yalnızca `media/blog/` — başka medya
    # bilinçli olarak dışarı açılmaz.
    path("media/blog/<path:yol>", blog_medya, name="blog-medya"),
    # Günlük e-posta bülteninden çıkma (e-postadaki bağlantı + List-Unsubscribe).
    path("eposta/abonelik/", eposta_abonelik, name="eposta-abonelik"),
    # Amazon SES bounce/şikâyet bildirimleri (SNS HTTPS aboneliği).
    path("eposta/ses-bildirim/", ses_bildirim, name="ses-bildirim"),
    path("api/v1/", include((api_v1, "v1"))),
    # API şema & dokümantasyon
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
