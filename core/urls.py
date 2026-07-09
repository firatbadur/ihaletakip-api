"""Core URL'leri."""
from django.urls import path

from .views import SupportTicketView

urlpatterns = [
    path("support/", SupportTicketView.as_view(), name="support"),
    # Not: DETSIS/kurum arama artık ekap app'inde: /api/v1/ekap/authorities/search/
]
