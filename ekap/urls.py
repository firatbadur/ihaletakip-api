"""ekap URL'leri — /api/v1/ekap/..."""
from django.urls import path

from .views import (
    AuthoritySearchView,
    CityListView,
    DocumentUrlView,
    OkasSearchView,
    TenderAnnouncementsView,
    TenderDetailView,
    TenderListView,
)

urlpatterns = [
    path("tenders/", TenderListView.as_view(), name="ekap-tenders"),
    path("tenders/<str:key>/", TenderDetailView.as_view(), name="ekap-tender-detail"),
    path("tenders/<str:key>/announcements/", TenderAnnouncementsView.as_view(), name="ekap-tender-announcements"),
    path("tenders/<str:ekap_id>/document-url/", DocumentUrlView.as_view(), name="ekap-document-url"),
    path("okas/search/", OkasSearchView.as_view(), name="ekap-okas-search"),
    path("authorities/search/", AuthoritySearchView.as_view(), name="ekap-authority-search"),
    path("cities/", CityListView.as_view(), name="ekap-cities"),
]
