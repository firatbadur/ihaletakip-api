"""subscription URL yapılandırması — /api/v1/subscription/..."""
from django.urls import path

from .views import RevenueCatWebhookView, SubscriptionVerifyView

urlpatterns = [
    path("verify/", SubscriptionVerifyView.as_view(), name="subscription-verify"),
    path(
        "revenuecat-webhook/",
        RevenueCatWebhookView.as_view(),
        name="subscription-revenuecat-webhook",
    ),
]
