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

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("token/refresh/", DocumentedTokenRefreshView.as_view(), name="token-refresh"),
    path("social/google/", GoogleLoginView.as_view(), name="google-login"),
    path("social/apple/", AppleLoginView.as_view(), name="apple-login"),
    path("profile/", ProfileView.as_view(), name="profile"),
    path("preferences/", PreferencesView.as_view(), name="preferences"),
    path("fcm-token/", FCMTokenView.as_view(), name="fcm-token"),
    path("deactivate/", DeactivateView.as_view(), name="deactivate"),
]
