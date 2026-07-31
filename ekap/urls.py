"""ekap URL'leri — /api/v1/ekap/..."""
from django.urls import path

from .views import (
    AuthoritySearchView,
    AuthorityTreeView,
    CityListView,
    ContractorContractsView,
    ContractorDetailView,
    ContractorListView,
    DocumentUrlView,
    OkasSearchView,
    TenderAnnouncementsView,
    TenderContractsView,
    TenderDetailView,
    TenderListView,
)

urlpatterns = [
    path("tenders/", TenderListView.as_view(), name="ekap-tenders"),
    path("tenders/<str:key>/", TenderDetailView.as_view(), name="ekap-tender-detail"),
    path("tenders/<str:key>/announcements/", TenderAnnouncementsView.as_view(), name="ekap-tender-announcements"),
    # İKN `/` içerdiği için bu alt rotalarda **ekap_id** kullanılır (bkz. TenderDetailView).
    path("tenders/<str:key>/contracts/", TenderContractsView.as_view(), name="ekap-tender-contracts"),
    path("tenders/<str:ekap_id>/document-url/", DocumentUrlView.as_view(), name="ekap-document-url"),
    # Yüklenici (firma) uçları
    path("contractors/", ContractorListView.as_view(), name="ekap-contractors"),
    path("contractors/<int:pk>/", ContractorDetailView.as_view(), name="ekap-contractor-detail"),
    path("contractors/<int:pk>/contracts/", ContractorContractsView.as_view(), name="ekap-contractor-contracts"),
    path("okas/search/", OkasSearchView.as_view(), name="ekap-okas-search"),
    path("authorities/tree/", AuthorityTreeView.as_view(), name="ekap-authority-tree"),
    path("authorities/search/", AuthoritySearchView.as_view(), name="ekap-authority-search"),
    path("cities/", CityListView.as_view(), name="ekap-cities"),
]
