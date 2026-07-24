"""Core URL'leri."""
from django.urls import path

from .views import SupportTicketView, app_status

urlpatterns = [
    # Public — mobil açılışta bakım modu + zorunlu güncelleme kontrolü
    path("app-status/", app_status, name="app-status"),
    path("support/", SupportTicketView.as_view(), name="support"),
    # Not: DETSIS/kurum arama artık ekap app'inde: /api/v1/ekap/authorities/search/
]
