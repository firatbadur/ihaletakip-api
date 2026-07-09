"""Core URL'leri."""
from django.urls import path

from .views import DetsisSearchView, SupportTicketView

urlpatterns = [
    path("support/", SupportTicketView.as_view(), name="support"),
    path("detsis/search/", DetsisSearchView.as_view(), name="detsis-search"),
]
