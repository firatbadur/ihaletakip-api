"""subscription URL yapılandırması — /api/v1/subscription/..."""
from django.urls import path

from .views import MagazaFiyatlariView, RevenueCatWebhookView, SubscriptionVerifyView

urlpatterns = [
    path("fiyatlar/", MagazaFiyatlariView.as_view(), name="subscription-fiyatlar"),
    path("verify/", SubscriptionVerifyView.as_view(), name="subscription-verify"),
    path(
        "revenuecat-webhook/",
        RevenueCatWebhookView.as_view(),
        name="subscription-revenuecat-webhook",
    ),
]
