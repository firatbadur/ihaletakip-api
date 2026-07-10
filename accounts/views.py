"""accounts view'ları — kayıt, giriş, sosyal giriş, profil, çıkış."""
from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from .serializers import (
    AppleLoginSerializer,
    DetailSerializer,
    FCMTokenSerializer,
    GoogleLoginSerializer,
    LoginSerializer,
    LogoutSerializer,
    PreferencesSerializer,
    RegisterSerializer,
    TokenPairSerializer,
    UserSerializer,
    issue_tokens,
)
from .services.apple import AppleAuthError, verify_apple_identity_token
from .services.google import GoogleAuthError, verify_google_id_token

User = get_user_model()


@extend_schema(
    tags=["auth"],
    summary="Kayıt ol",
    auth=[],
    description=(
        "E-posta + şifre ile yeni hesap açar ve doğrudan `access` + `refresh` token "
        "döner — ayrıca login çağırmaya gerek yoktur. Şifre en az 6 karakter olmalıdır."
    ),
    request=RegisterSerializer,
    responses={201: TokenPairSerializer},
    examples=[
        OpenApiExample(
            "Yeni kullanıcı",
            request_only=True,
            value={
                "email": "test@ihaletakip.com",
                "password": "Test1234!",
                "display_name": "Test Kullanıcı",
            },
        )
    ],
)
class RegisterView(APIView):
    """POST /auth/register — e-posta + şifre ile kayıt."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(issue_tokens(user), status=status.HTTP_201_CREATED)


@extend_schema(
    tags=["auth"],
    summary="Giriş yap",
    auth=[],
    description=(
        "`username` **veya** `email` ile giriş yapılır (biri yeterli). Yanıttaki "
        "`access` token'ı `Authorization: Bearer <access>` header'ında kullanın.\n\n"
        "Postman'de bu istek başarılı olduğunda `access_token` ve `refresh_token` "
        "koleksiyon değişkenleri otomatik doldurulur."
    ),
    request=LoginSerializer,
    responses={200: TokenPairSerializer, 401: DetailSerializer},
    examples=[
        OpenApiExample(
            "E-posta ile",
            request_only=True,
            value={"email": "test@ihaletakip.com", "password": "Test1234!"},
        ),
        OpenApiExample(
            "Kullanıcı adı ile",
            request_only=True,
            value={"username": "firat", "password": "Test1234!"},
        ),
    ],
)
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


@extend_schema(
    tags=["auth"],
    summary="Google ile giriş",
    auth=[],
    description=(
        "İstemci `@react-native-google-signin` ile aldığı `id_token`'ı gönderir; "
        "sunucu Google imzasını doğrular. Hesap yoksa oluşturulur (upsert)."
    ),
    request=GoogleLoginSerializer,
    responses={200: TokenPairSerializer, 401: DetailSerializer},
    examples=[
        OpenApiExample(
            "Google ID token",
            request_only=True,
            value={"id_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6IjE2ZGE...google-id-token"},
        )
    ],
)
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


@extend_schema(
    tags=["auth"],
    summary="Apple ile giriş",
    auth=[],
    description=(
        "İstemci `@invertase/react-native-apple-authentication` ile aldığı "
        "`identity_token`'ı gönderir; sunucu Apple public key'leriyle doğrular "
        "(audience `com.envisoft.ihaletakip`).\n\n"
        "Apple kullanıcının adını **yalnızca ilk girişte** döner — istemci bunu "
        "`full_name` alanında iletmezse isim kalıcı olarak kaybolur."
    ),
    request=AppleLoginSerializer,
    responses={200: TokenPairSerializer, 401: DetailSerializer},
    examples=[
        OpenApiExample(
            "Apple identity token",
            request_only=True,
            value={
                "identity_token": "eyJraWQiOiJXNldjT0tCIiwiYWxnIjoiUlMyNTYifQ...apple-identity-token",
                "full_name": "Test Kullanıcı",
            },
        )
    ],
)
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


@extend_schema(
    tags=["auth"],
    summary="Çıkış yap",
    description=(
        "Gönderilen `refresh` token'ı kara listeye alır.\n\n"
        "**Dikkat:** Bu endpoint kimlik doğrulaması ister — gövdedeki `refresh`'in "
        "yanında geçerli bir `Authorization: Bearer <access>` header'ı da gerekir.\n\n"
        "Kara liste yalnızca refresh token'ları kapsar; mevcut `access` token kendi "
        "ömrü (1 gün) dolana kadar geçerli kalmaya devam eder — istemci onu cihazdan "
        "silmelidir."
    ),
    request=LogoutSerializer,
    responses={205: DetailSerializer, 400: DetailSerializer},
    examples=[
        OpenApiExample(
            "Refresh token ile",
            request_only=True,
            value={"refresh": "{{refresh_token}}"},
        )
    ],
)
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


@extend_schema(tags=["auth"])
class ProfileView(APIView):
    """GET/PATCH /auth/profile — profil görüntüle/güncelle."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Profili getir",
        description="Oturum açmış kullanıcının profilini döner.",
        responses={200: UserSerializer},
    )
    def get(self, request):
        return Response(UserSerializer(request.user).data)

    @extend_schema(
        summary="Profili güncelle",
        description=(
            "Kısmi güncelleme. Yalnızca `display_name`, `email`, `photo_url` ve "
            "`preferences` yazılabilir; `id`, `username`, `provider` ve `date_joined` "
            "salt okunurdur."
        ),
        request=UserSerializer,
        responses={200: UserSerializer},
        examples=[
            OpenApiExample(
                "İsim güncelle",
                request_only=True,
                value={"display_name": "Fırat Badur"},
            )
        ],
    )
    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@extend_schema(
    tags=["auth"],
    summary="Tercihleri güncelle",
    description=(
        "`preferences` serbest biçimli bir JSON nesnesidir; gönderilen değer mevcut "
        "tercihlerin **yerine geçer** (birleştirme yapılmaz)."
    ),
    request=PreferencesSerializer,
    responses={200: PreferencesSerializer},
    examples=[
        OpenApiExample(
            "Bildirim + tema tercihleri",
            request_only=True,
            value={
                "preferences": {
                    "theme": "dark",
                    "notifications": {"push": True, "email": False},
                    "default_cities": [251, 284],
                }
            },
        )
    ],
)
class PreferencesView(APIView):
    """PATCH /auth/preferences — tercihleri güncelle."""

    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        serializer = PreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.preferences = serializer.validated_data["preferences"]
        request.user.save(update_fields=["preferences"])
        return Response({"preferences": request.user.preferences})


@extend_schema(
    tags=["auth"],
    summary="FCM token kaydet",
    description=(
        "Push bildirimleri için cihazın Firebase Cloud Messaging token'ını kaydeder. "
        "Sunucuda `FCM_CREDENTIALS` tanımlı değilse push gönderimi devre dışıdır, "
        "token yine de saklanır."
    ),
    request=FCMTokenSerializer,
    responses={200: DetailSerializer},
    examples=[
        OpenApiExample(
            "Cihaz token'ı",
            request_only=True,
            value={"fcm_token": "fMEP0vJqR0m2Xy1s_example_device_token_abc123"},
        )
    ],
)
class FCMTokenView(APIView):
    """POST /auth/fcm-token — push bildirim token'ını kaydet."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = FCMTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.fcm_token = serializer.validated_data["fcm_token"]
        request.user.save(update_fields=["fcm_token"])
        return Response({"detail": "FCM token kaydedildi."})


@extend_schema(
    tags=["auth"],
    summary="Access token yenile",
    auth=[],
    description=(
        "Süresi dolan `access` token'ı `refresh` ile yeniler.\n\n"
        "**Rotasyon açıktır** (`ROTATE_REFRESH_TOKENS`): yanıt yeni bir `access` ile "
        "birlikte **yeni bir `refresh`** de döner ve gönderdiğiniz eski `refresh` "
        "anında kara listeye alınır. İstemci sakladığı refresh token'ı her yenilemede "
        "güncellemelidir; eskisini tekrar kullanmak `401` verir.\n\n"
        "Bu uç kimlik doğrulaması istemez — `refresh` token'ın kendisi yeterlidir.\n\n"
        "Postman'de başarılı yanıt `access_token` ve `refresh_token` değişkenlerini "
        "otomatik günceller."
    ),
    examples=[
        OpenApiExample(
            "Refresh token ile",
            request_only=True,
            value={"refresh": "{{refresh_token}}"},
        )
    ],
)
class DocumentedTokenRefreshView(TokenRefreshView):
    """POST /auth/token/refresh — access token'ı yeniler (refresh rotasyonlu)."""


@extend_schema(
    tags=["auth"],
    summary="Hesabı devre dışı bırak",
    description=(
        "Hesabı pasifleştirir (`is_active=False`). Kayıtlar silinmez, ancak kullanıcı "
        "bir daha giriş yapamaz — login `403` döner. Gövde gerektirmez."
    ),
    request=None,
    responses={200: DetailSerializer},
)
class DeactivateView(APIView):
    """POST /auth/deactivate — hesabı devre dışı bırak."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        user.is_active = False
        user.deactivated_at = timezone.now()
        user.save(update_fields=["is_active", "deactivated_at"])
        return Response({"detail": "Hesap devre dışı bırakıldı."})
