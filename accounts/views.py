"""accounts view'ları — kayıt, giriş, sosyal giriş, profil, çıkış."""
import logging

from django.contrib.auth import authenticate, get_user_model
from django.db import IntegrityError, transaction
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
from .models import PushDevice
from .services.apple import AppleAuthError, verify_apple_identity_token
from .services.google import GoogleAuthError, verify_google_id_token

User = get_user_model()
logger = logging.getLogger("ihaletakip")


@extend_schema(
    tags=["auth"],
    summary="Kayıt ol",
    auth=[],
    description=(
        "E-posta + şifre ile yeni hesap açar ve doğrudan `access` + `refresh` token "
        "döner — ayrıca login çağırmaya gerek yoktur. Şifre en az 6 karakter olmalıdır."
        "\n\nYanıt `created: true` taşır (bu uç yalnızca kayıt yapar); sosyal girişle "
        "aynı sözleşmeyi kullanır."
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
        return Response(
            issue_tokens(user, created=True), status=status.HTTP_201_CREATED
        )


@extend_schema(
    tags=["auth"],
    summary="Giriş yap",
    auth=[],
    description=(
        "`username` **veya** `email` ile giriş yapılır (biri yeterli). Yanıttaki "
        "`access` token'ı `Authorization: Bearer <access>` header'ında kullanın.\n\n"
        "Postman'de bu istek başarılı olduğunda `access_token` ve `refresh_token` "
        "koleksiyon değişkenleri otomatik doldurulur.\n\n"
        "Yanıt her zaman `created: false` taşır (bu uç hesap açmaz)."
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
        "sunucu Google imzasını doğrular. Hesap yoksa oluşturulur (upsert).\n\n"
        "Yanıttaki **`created`** bayrağı bu istekte hesabın yeni açılıp açılmadığını "
        "söyler: `true` → kayıt, `false` → mevcut hesaba giriş. Uç ikisini birden "
        "yaptığı için istemci analitik olayını (ör. AppsFlyer "
        "`af_complete_registration` vs. `af_login`) bu bayrağa göre ayırmalıdır. "
        "Aynı e-posta daha önce şifreyle kayıtlıysa hesap eşleştirilir ve "
        "`created=false` döner."
    ),
    request=GoogleLoginSerializer,
    responses={200: TokenPairSerializer, 401: DetailSerializer},
    examples=[
        OpenApiExample(
            "Google ID token",
            request_only=True,
            value={"id_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6IjE2ZGE...google-id-token"},
        ),
        OpenApiExample(
            "İlk giriş (kayıt)",
            response_only=True,
            value={
                "success": True,
                "message": "",
                "data": {
                    "access": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "refresh": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "user": {"id": 42, "email": "test@ihaletakip.com"},
                    "created": True,
                },
            },
        ),
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

        user, created = User.objects.get_or_create_social(
            email=info["email"],
            provider=User.Provider.GOOGLE,
            provider_uid=info["sub"],
            display_name=info.get("name", ""),
            first_name=info.get("given_name", ""),
            last_name=info.get("family_name", ""),
            photo_url=info.get("picture", ""),
        )
        return Response(issue_tokens(user, created=created))


@extend_schema(
    tags=["auth"],
    summary="Apple ile giriş",
    auth=[],
    description=(
        "İstemci `@invertase/react-native-apple-authentication` ile aldığı "
        "`identity_token`'ı gönderir; sunucu Apple public key'leriyle doğrular "
        "(audience `com.envisoft.ihaletakip`).\n\n"
        "Apple kullanıcının adını **yalnızca ilk girişte** döner — istemci bunu "
        "`full_name` alanında iletmezse isim kalıcı olarak kaybolur.\n\n"
        "Yanıttaki **`created`** bayrağı bu istekte hesabın yeni açılıp açılmadığını "
        "söyler: `true` → kayıt, `false` → mevcut hesaba giriş. Uç ikisini birden "
        "yaptığı için istemci analitik olayını (ör. AppsFlyer "
        "`af_complete_registration` vs. `af_login`) bu bayrağa göre ayırmalıdır."
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

        user, created = User.objects.get_or_create_social(
            email=info["email"],
            provider=User.Provider.APPLE,
            provider_uid=info["sub"],
            display_name=full_name,
        )
        return Response(issue_tokens(user, created=created))


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
        ),
        OpenApiExample(
            "Cihazın push kaydını da sil (önerilen)",
            request_only=True,
            value={
                "refresh": "{{refresh_token}}",
                "fcm_token": "fMEP0vJqR0m2Xy1s_example_device_token_abc123",
            },
        ),
    ],
)
class LogoutView(APIView):
    """POST /auth/logout — {refresh} token'ı kara listeye al.

    `fcm_token` gönderilirse o cihazın push kaydı da silinir → kullanıcı tekrar
    giriş yapana kadar bu cihazdan bildirim almaz. Diğer cihazları etkilenmez.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # ⚠️ Cihaz kaydı refresh DOĞRULAMASINDAN ÖNCE silinir: istemci bozuk/eski bir
        # refresh gönderse de "bu cihazdan ayrılıyorum" niyeti kesindir. 400 dönüp
        # cihazı kayıtlı bırakmak, çıkış yapılmış hesaba push atmaya devam etmek olurdu.
        # ⚠️ Kullanıcının TÜM cihazlarını silmek YANLIŞ olurdu — başka telefonu olabilir
        # ve ondan da bildirim almayı keserdi. Yalnızca gövdede bildirilen cihaz silinir.
        # ⚠️ Eski mobil sürümler `fcm_token` göndermez → o cihaz burada silinmez; bir
        # sonraki girişte `FCMTokenView` devri zaten yapar (kırılma yok).
        cikis_token = (request.data.get("fcm_token") or "").strip()
        if cikis_token:
            PushDevice.objects.filter(token=cikis_token, user=request.user).delete()

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
            "Kısmi güncelleme. Yazılabilir: `display_name`, `first_name`, `last_name`, "
            "`email`, `photo_url`, `preferences`, `age_range` "
            "(`18_24|25_34|35_44|45_54|55_plus`) ve `onboarding_status` "
            "(`pending|skipped|completed`). `completed`'a ilk geçişte "
            "`onboarding_completed_at` sunucuda damgalanır. `id`, `username`, "
            "`provider` ve `date_joined` salt okunurdur."
        ),
        request=UserSerializer,
        responses={200: UserSerializer},
        examples=[
            OpenApiExample(
                "İsim güncelle",
                request_only=True,
                value={"display_name": "Fırat Badur"},
            ),
            OpenApiExample(
                "Tanışma sihirbazını tamamla",
                request_only=True,
                value={
                    "first_name": "Fırat",
                    "last_name": "Badur",
                    "age_range": "25_34",
                    "onboarding_status": "completed",
                },
            ),
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
        "token yine de saklanır.\n\n"
        "⚠️ Bir token **en çok bir hesaba** aittir: token cihaza aittir, kullanıcıya "
        "değil. Aynı telefonda hesap değiştirilirse cihaz kaydı yeni hesaba **geçer** "
        "ve eski hesap o cihazdan bildirim almaz — kasıtlıdır.\n\n"
        "Bir kullanıcının birden çok cihazı olabilir; her birine ayrı push gider."
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
        # `.strip()`: saklanan değer `notify.push_to_user`'ın kıyasladığıyla birebir
        # aynı olmalı ve bu uç serializer ayarına bağlı kalmasın.
        token = serializer.validated_data["fcm_token"].strip()
        platform = (request.data.get("platform") or "").strip()[:10]

        # ⚠️⚠️ "Bir cihaz = en çok bir hesap" kuralının YAZILDIĞI yer. `token` unique
        # olduğu için `update_or_create` cihazı yeni sahibine DEVREDER, ikinci satır
        # açmaz. Eski tasarımda (User.fcm_token tek kolon) bu kural yoktu ve üretimde
        # tek telefonun token'ı dört hesapta birden kaldı → aynı cihaza 3 push
        # (2026-10-05; bkz. PushDevice docstring).
        eski_sahip = None
        for deneme in (1, 2):
            try:
                # ⚠️ try bloğu `atomic`in DIŞINDA: IntegrityError işlemi "rollback
                # bekliyor" durumuna düşürür, aynı atomic blok içinde sorgu sürdürmek
                # TransactionManagementError verir.
                with transaction.atomic():
                    cihaz = PushDevice.objects.filter(token=token).first()
                    eski_sahip = cihaz.user_id if cihaz else None
                    PushDevice.objects.update_or_create(
                        token=token,
                        defaults={"user": request.user, "platform": platform},
                    )
                break
            except IntegrityError:
                # ⚠️ İki istek aynı token'ı milisaniyeler içinde yazdı. İkinci turda
                # karşı satır artık görünür → yakınsar. Kullanıcıya 500 dönmek kabul
                # edilemez; token kaydedilmemişken 200 dönmek daha da kötü olurdu.
                if deneme == 2:
                    logger.error(
                        "push cihaz yarışı iki denemede çözülemedi uid=%s", request.user.pk
                    )
                    raise

        if eski_sahip and eski_sahip != request.user.pk:
            # Cihazda hesap değişti. Sonraki bir arızada "kim kimden devraldı"
            # sorusunun tek kaydı bu satır olur.
            logger.warning(
                "push cihazı devralındı: token=%s… eski_uid=%s → yeni_uid=%s",
                token[:10], eski_sahip, request.user.pk,
            )
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
        # Hesap kapatılıyor → bu hesabın TÜM cihaz kayıtları silinir. (Çıkıştan farkı:
        # orada yalnızca o cihaz silinir, burada hesabın kendisi devre dışı.)
        # ⚠️ `push_to_user` zaten `is_active`e bakıp atlıyor; yine de silinir çünkü
        # (a) hesap yeniden aktif edilirse aylar önceki, belki artık başkasının
        # telefonundaki kayıt canlanırdı, (b) aynı cihazda açılan yeni hesap o satırı
        # devralmak zorunda kalırdı, (c) pano "push açık" sayacı ölü kurulumlarla şişer.
        PushDevice.objects.filter(user=user).delete()
        return Response({"detail": "Hesap devre dışı bırakıldı."})
