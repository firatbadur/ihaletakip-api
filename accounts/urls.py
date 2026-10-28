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
from subscription.views_odeme import BillingProfileView

from .views_eposta import (
    EmailRegisterStartView,
    EmailRegisterVerifyView,
    PasswordResetConfirmView,
    PasswordResetStartView,
    PasswordResetVerifyView,
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
    path("logout/", LogoutView.as_view(), name="logout"),
    path("token/refresh/", DocumentedTokenRefreshView.as_view(), name="token-refresh"),
    path("social/google/", GoogleLoginView.as_view(), name="google-login"),
    path("social/apple/", AppleLoginView.as_view(), name="apple-login"),
    path("profile/", ProfileView.as_view(), name="profile"),
    path("preferences/", PreferencesView.as_view(), name="preferences"),
    path("fcm-token/", FCMTokenView.as_view(), name="fcm-token"),
    path("deactivate/", DeactivateView.as_view(), name="deactivate"),
    # Web Pro satışı için fatura profili (web 6. not #16) — model `subscription` app'inde.
    path("billing-profile/", BillingProfileView.as_view(), name="billing-profile"),
]
