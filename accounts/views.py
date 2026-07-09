"""accounts view'ları — kayıt, giriş, sosyal giriş, profil, çıkış."""
from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (
    FCMTokenSerializer,
    PreferencesSerializer,
    RegisterSerializer,
    UserSerializer,
    issue_tokens,
)
from .services.apple import AppleAuthError, verify_apple_identity_token
from .services.google import GoogleAuthError, verify_google_id_token

User = get_user_model()


class RegisterView(APIView):
    """POST /auth/register — e-posta + şifre ile kayıt."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(issue_tokens(user), status=status.HTTP_201_CREATED)


class LoginView(APIView):
    """POST /auth/login — username veya email + şifre ile giriş."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        identifier = request.data.get("username") or request.data.get("email")
        password = request.data.get("password")
        if not identifier or not password:
            return Response(
                {"detail": "Kullanıcı adı/e-posta ve şifre gerekli."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # email ile geldiyse username'e çevir
        user_obj = User.objects.filter(email__iexact=identifier).first()
        username = user_obj.username if user_obj else identifier

        user = authenticate(request, username=username, password=password)
        if user is None:
            return Response(
                {"detail": "Geçersiz kimlik bilgileri."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if not user.is_active:
            return Response(
                {"detail": "Hesap devre dışı."}, status=status.HTTP_403_FORBIDDEN
            )
        return Response(issue_tokens(user))


class GoogleLoginView(APIView):
    """POST /auth/social/google — {id_token} ile giriş."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        id_token = request.data.get("id_token") or request.data.get("idToken")
        try:
            info = verify_google_id_token(id_token)
        except GoogleAuthError as e:
            return Response({"detail": str(e)}, status=status.HTTP_401_UNAUTHORIZED)

        user, _ = User.objects.get_or_create_social(
            email=info["email"],
            provider=User.Provider.GOOGLE,
            provider_uid=info["sub"],
            display_name=info.get("name", ""),
            photo_url=info.get("picture", ""),
        )
        return Response(issue_tokens(user))


class AppleLoginView(APIView):
    """POST /auth/social/apple — {identity_token, full_name?} ile giriş."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        identity_token = (
            request.data.get("identity_token")
            or request.data.get("identityToken")
        )
        try:
            info = verify_apple_identity_token(identity_token)
        except AppleAuthError as e:
            return Response({"detail": str(e)}, status=status.HTTP_401_UNAUTHORIZED)

        # Apple ismi yalnızca ilk girişte gelir; istemci gönderirse kullan
        full_name = request.data.get("full_name", "") or request.data.get("fullName", "")

        user, _ = User.objects.get_or_create_social(
            email=info["email"],
            provider=User.Provider.APPLE,
            provider_uid=info["sub"],
            display_name=full_name,
        )
        return Response(issue_tokens(user))


class LogoutView(APIView):
    """POST /auth/logout — {refresh} token'ı kara listeye al."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        refresh = request.data.get("refresh")
        if not refresh:
            return Response(
                {"detail": "refresh token gerekli."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token = RefreshToken(refresh)
            token.blacklist()
        except TokenError:
            return Response(
                {"detail": "Geçersiz refresh token."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": "Çıkış yapıldı."}, status=status.HTTP_205_RESET_CONTENT)


class ProfileView(APIView):
    """GET/PATCH /auth/profile — profil görüntüle/güncelle."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class PreferencesView(APIView):
    """PATCH /auth/preferences — tercihleri güncelle."""

    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        serializer = PreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.preferences = serializer.validated_data["preferences"]
        request.user.save(update_fields=["preferences"])
        return Response({"preferences": request.user.preferences})


class FCMTokenView(APIView):
    """POST /auth/fcm-token — push bildirim token'ını kaydet."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = FCMTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.fcm_token = serializer.validated_data["fcm_token"]
        request.user.save(update_fields=["fcm_token"])
        return Response({"detail": "FCM token kaydedildi."})


class DeactivateView(APIView):
    """POST /auth/deactivate — hesabı devre dışı bırak."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        user.is_active = False
        user.deactivated_at = timezone.now()
        user.save(update_fields=["is_active", "deactivated_at"])
        return Response({"detail": "Hesap devre dışı bırakıldı."})
