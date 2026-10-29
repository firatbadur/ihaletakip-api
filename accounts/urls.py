"""accounts URL'leri — /api/v1/auth/..."""
from django.urls import path

from .views import (
    AppleLoginView,
    DeactivateView,
    DocumentedTokenRefreshView,
    FCMTokenView,
    GoogleLoginView,
    LoginView,
    LogoutView,
    PreferencesView,
    ProfileView,
    RegisterView,
)
from .views_eposta import (
    EmailRegisterStartView,
    EmailRegisterVerifyView,
    PasswordResetConfirmView,
    PasswordResetStartView,
    PasswordResetVerifyView,
)
from .views_handoff import (
    HandoffCreateView,
    HandoffDetailView,
    HandoffRedeemView,
    HandoffScanView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    # E-posta ile kayıt (kodla doğrulama) + şifre sıfırlama — bkz. views_eposta.py
    path("email/register/start/", EmailRegisterStartView.as_view(), name="email-register-start"),
    path("email/register/verify/", EmailRegisterVerifyView.as_view(), name="email-register-verify"),
    path("password/reset/start/", PasswordResetStartView.as_view(), name="password-reset-start"),
    path("password/reset/verify/", PasswordResetVerifyView.as_view(), name="password-reset-verify"),
    path("password/reset/confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
    # Web → mobil geçiş kodu (QR ile uygulamada aynı hesapla ödeme ekranı) — views_handoff.py
    path("handoff/", HandoffCreateView.as_view(), name="handoff-create"),
    path("handoff/okutuldu/", HandoffScanView.as_view(), name="handoff-scan"),
    path("handoff/redeem/", HandoffRedeemView.as_view(), name="handoff-redeem"),
    path("handoff/<int:pk>/", HandoffDetailView.as_view(), name="handoff-detail"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("token/refresh/", DocumentedTokenRefreshView.as_view(), name="token-refresh"),
    path("social/google/", GoogleLoginView.as_view(), name="google-login"),
    path("social/apple/", AppleLoginView.as_view(), name="apple-login"),
    path("profile/", ProfileView.as_view(), name="profile"),
    path("preferences/", PreferencesView.as_view(), name="preferences"),
    path("fcm-token/", FCMTokenView.as_view(), name="fcm-token"),
    path("deactivate/", DeactivateView.as_view(), name="deactivate"),
]
