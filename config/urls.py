"""IhaleTakip API — kök URL yapılandırması."""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from core.views import health_check

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
    path("", include("core.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health"),
    path("api/v1/", include((api_v1, "v1"))),
    # API şema & dokümantasyon
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
