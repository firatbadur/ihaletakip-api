"""Web ödeme URL'leri — /api/v1/payments/... (web 6. not #17)."""
from django.urls import path

from .views_odeme import (
    CheckoutCompleteView,
    CheckoutView,
    PaymentCancelRequestView,
    PaymentInvoiceView,
    PaymentListView,
    PlanView,
)

urlpatterns = [
    path("", PaymentListView.as_view(), name="payments-list"),
    path("plan/", PlanView.as_view(), name="payments-plan"),
    path("checkout/", CheckoutView.as_view(), name="payments-checkout"),
    path("checkout/complete/", CheckoutCompleteView.as_view(), name="payments-complete"),
    path("<int:pk>/invoice/", PaymentInvoiceView.as_view(), name="payments-invoice"),
    path("<int:pk>/cancel-request/", PaymentCancelRequestView.as_view(),
         name="payments-cancel-request"),
]
