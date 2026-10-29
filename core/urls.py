"""Core URL'leri."""
from django.urls import path

from .views import (
    SupportTicketView,
    app_status,
    support_ticket_close,
    support_ticket_detail,
    support_ticket_reply,
    support_unread_count,
)

urlpatterns = [
    # Public — mobil açılışta bakım modu + zorunlu güncelleme kontrolü
    path("app-status/", app_status, name="app-status"),
    path("support/", SupportTicketView.as_view(), name="support"),
    path("support/unread-count/", support_unread_count, name="support-unread"),
    path("support/<int:pk>/", support_ticket_detail, name="support-detail"),
    path("support/<int:pk>/messages/", support_ticket_reply, name="support-reply"),
    path("support/<int:pk>/close/", support_ticket_close, name="support-close"),
    # Not: DETSIS/kurum arama artık ekap app'inde: /api/v1/ekap/authorities/search/
]
