"""ekap URL'leri — /api/v1/ekap/..."""
from django.urls import path

from .verification_views import DogrulamaCereziView
from .views_kurul import KurulKarariListView, KurulToplantiListView, TenderKurulKarariView
from .views_dt import (
    DogrudanTeminDetailView,
    DogrudanTeminDokumanView,
    DogrudanTeminFiltrelerView,
    DogrudanTeminListView,
)
from .views import (
    AuthorityProfileView,
    AuthoritySearchView,
    AuthorityTreeView,
    CityListView,
    ContractorContractsView,
    ContractorDetailView,
    ContractorListView,
    DocumentUrlView,
    TenderDocumentView,
    TenderDocumentsView,
    MarketBucketView,
    MarketOverviewView,
    OkasSearchView,
    SectorListView,
    RecurringSeriesListView,
    TenderAnnouncementsView,
    TenderBenchmarkView,
    TenderContractsView,
    TenderDetailView,
    TenderListView,
    TenderRecurringView,
)

urlpatterns = [
    path("tenders/", TenderListView.as_view(), name="ekap-tenders"),
    path("tenders/<str:key>/", TenderDetailView.as_view(), name="ekap-tender-detail"),
    path("tenders/<str:key>/announcements/", TenderAnnouncementsView.as_view(), name="ekap-tender-announcements"),
    # İKN `/` içerdiği için bu alt rotalarda **ekap_id** kullanılır (bkz. TenderDetailView).
    path("tenders/<str:key>/contracts/", TenderContractsView.as_view(), name="ekap-tender-contracts"),
    path("tenders/<str:key>/kurul-kararlari/", TenderKurulKarariView.as_view(),
         name="ekap-tender-kurul-kararlari"),
    path("recurring/", RecurringSeriesListView.as_view(), name="ekap-recurring"),
    path("tenders/<str:key>/recurring/", TenderRecurringView.as_view(), name="ekap-tender-recurring"),
    path("tenders/<str:key>/benchmark/", TenderBenchmarkView.as_view(), name="ekap-tender-benchmark"),
    path("tenders/<str:ekap_id>/document-url/", DocumentUrlView.as_view(), name="ekap-document-url"),
    # Mobil kaynaklı ihalelerde belge indirme: mobil uçta kalıcı URL yok, tek
    # kullanımlık id var → dosya bizim üzerimizden akıtılır (bkz. TenderDocumentView).
    path("tenders/<str:key>/documents/", TenderDocumentsView.as_view(), name="ekap-tender-documents"),
    path("tenders/<str:key>/document/", TenderDocumentView.as_view(), name="ekap-tender-document"),
    # Yüklenici (firma) uçları
    path("contractors/", ContractorListView.as_view(), name="ekap-contractors"),
    path("contractors/<int:pk>/", ContractorDetailView.as_view(), name="ekap-contractor-detail"),
    path("contractors/<int:pk>/contracts/", ContractorContractsView.as_view(), name="ekap-contractor-contracts"),
    path("okas/search/", OkasSearchView.as_view(), name="ekap-okas-search"),
    path("authorities/profile/", AuthorityProfileView.as_view(), name="ekap-authority-profile"),
    path("authorities/tree/", AuthorityTreeView.as_view(), name="ekap-authority-tree"),
    path("authorities/search/", AuthoritySearchView.as_view(), name="ekap-authority-search"),
    # Pazar panosu (Adım 6). ⚠️ `okas_bucket` 4 haneli ASCII → `<str:>` yeterli,
    # İKN'deki gibi `path:` gerekmez.
    path("market/", MarketOverviewView.as_view(), name="ekap-market"),
    path("market/<str:okas_bucket>/", MarketBucketView.as_view(), name="ekap-market-bucket"),
    path("sektorler/", SectorListView.as_view(), name="ekap-sektorler"),
    # KİK kurul kararları (uyuşmazlık). ⚠️ `toplantilar/` sabit yol, parametreli yol yok.
    path("kurul-kararlari/", KurulKarariListView.as_view(), name="ekap-kurul-kararlari"),
    path("kurul-kararlari/toplantilar/", KurulToplantiListView.as_view(),
         name="ekap-kurul-toplantilar"),
    # Doğrudan temin. ⚠️ `filtreler/` `<str:dtn>/`'den ÖNCE: aksi hâlde "filtreler"
    # bir dtn sanılırdı. dtn ASCII ve `/` içermez → `<str:>` yeterli.
    path("dogrudan-temin/", DogrudanTeminListView.as_view(), name="ekap-dt-list"),
    path("dogrudan-temin/filtreler/", DogrudanTeminFiltrelerView.as_view(),
         name="ekap-dt-filtreler"),
    path("dogrudan-temin/<str:dtn>/", DogrudanTeminDetailView.as_view(), name="ekap-dt-detail"),
    path("dogrudan-temin/<str:dtn>/dokuman/", DogrudanTeminDokumanView.as_view(),
         name="ekap-dt-dokuman"),
    path("cities/", CityListView.as_view(), name="ekap-cities"),
    # İnsan doğrulaması çerezini tarayıcı eklentisinden alır (bkz. verification_views).
    path("verification-cookie/", DogrulamaCereziView.as_view(), name="ekap-verification-cookie"),
]
