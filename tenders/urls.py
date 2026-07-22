"""tenders URL'leri — /api/v1/..."""
from django.urls import path

from .views import (
    FavoriteAuthorityDetailView,
    FavoriteAuthorityListCreateView,
    FavoriteDetailView,
    FavoriteListCreateView,
    NotificationListView,
    NotificationReadAllView,
    NotificationReadView,
    NotificationUnreadCountView,
    SavedFilterDetailView,
    SavedFilterListCreateView,
    SavedTenderDetailView,
    SavedTenderListCreateView,
    TenderAlarmDetailView,
    TenderAlarmListCreateView,
)

urlpatterns = [
    # Favoriler
    path("favorites/", FavoriteListCreateView.as_view(), name="favorites"),
    path("favorites/<str:tender_id>/", FavoriteDetailView.as_view(), name="favorite-detail"),
    # Favori idareler (DETSIS kurum düğümü)
    path("favorite-authorities/", FavoriteAuthorityListCreateView.as_view(), name="favorite-authorities"),
    path("favorite-authorities/<str:detsis_no>/", FavoriteAuthorityDetailView.as_view(), name="favorite-authority-detail"),
    # Kayıtlı filtreler
    path("saved-filters/", SavedFilterListCreateView.as_view(), name="saved-filters"),
    path("saved-filters/<int:pk>/", SavedFilterDetailView.as_view(), name="saved-filter-detail"),
    # Kayıtlı ihaleler
    path("saved-tenders/", SavedTenderListCreateView.as_view(), name="saved-tenders"),
    # İKN `2025/1234567` biçimindedir; `str` dönüştürücüsü `/` eşleştirmez ve WSGI
    # sunucusu `%2F`'yi yola çözdüğü için kodlamak da işe yaramaz → `path` şart.
    path("saved-tenders/<path:ikn>/", SavedTenderDetailView.as_view(), name="saved-tender-detail"),
    # Alarmlar
    path("alarms/", TenderAlarmListCreateView.as_view(), name="alarms"),
    path("alarms/<str:tender_id>/", TenderAlarmDetailView.as_view(), name="alarm-detail"),
    # Bildirimler
    path("notifications/", NotificationListView.as_view(), name="notifications"),
    path("notifications/read-all/", NotificationReadAllView.as_view(), name="notifications-read-all"),
    path("notifications/unread-count/", NotificationUnreadCountView.as_view(), name="notifications-unread"),
    path("notifications/<int:notification_id>/read/", NotificationReadView.as_view(), name="notification-read"),
]
