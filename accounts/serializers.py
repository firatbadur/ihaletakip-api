"""accounts serializer'ları."""
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """Kullanıcı profili (istemciye dönen)."""

    # Abonelik durumu — mobil bunu okuyarak Pro özellikleri gösterir/gizler.
    # Katman salt okunurdur; API üzerinden değiştirilemez (admin/ödeme ile ayarlanır).
    is_premium = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "display_name",
            "first_name",
            "last_name",
            "photo_url",
            "provider",
            "preferences",
            "date_joined",
            "is_premium",
            "subscription_tier",
            "subscription_expires_at",
            "age_range",
            "onboarding_status",
            "onboarding_completed_at",
            "email_notifications",
        ]
        read_only_fields = [
            "id",
            "username",
            # ⚠️ E-posta artık giriş kimliği + bülten adresi → doğrulamasız değiştirilemez
            # (mobil + web hiç değiştirmiyordu — ölçüldü 2026-10-27).
            "email",
            "provider",
            "date_joined",
            "is_premium",
            "subscription_tier",
            "subscription_expires_at",
            "onboarding_completed_at",
        ]

    def update(self, instance, validated_data):
        # Tamamlanma anı sunucuda damgalanır (istemci saati güvenilmez).
        # Sihirbaz sonradan yeniden tamamlanırsa ilk tarih korunur.
        if (
            validated_data.get("onboarding_status") == User.OnboardingStatus.COMPLETED
            and instance.onboarding_completed_at is None
        ):
            instance.onboarding_completed_at = timezone.now()
        return super().update(instance, validated_data)


class RegisterSerializer(serializers.ModelSerializer):
    """E-posta + şifre ile kayıt."""

    password = serializers.CharField(write_only=True, min_length=6)

    class Meta:
        model = User
        fields = ["email", "password", "display_name"]

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Bu e-posta zaten kayıtlı.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        email = validated_data["email"]
        user = User(
            username=User.objects._unique_username(email),
            email=email,
            display_name=validated_data.get("display_name", ""),
            provider=User.Provider.EMAIL,
        )
        user.set_password(password)
        user.save()
        return user


def _sifre_dogrula(sifre, aday):
    """Django şifre kuralları (en az 8 karakter, yaygın/yalnız rakam olamaz, e-postaya
    benzemez) — mesajlar `LANGUAGE_CODE=tr` ile Türkçe gelir."""
    try:
        validate_password(sifre, user=aday)
    except DjangoValidationError as e:
        raise serializers.ValidationError(list(e.messages)) from e
    return sifre


class EmailRegisterStartSerializer(serializers.Serializer):
    """E-posta ile kayıt — 1. adım (kod gönderilir, hesap henüz AÇILMAZ)."""

    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)
    display_name = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, attrs):
        email = attrs["email"].strip().lower()
        attrs["email"] = email
        aday = User(email=email, username=email.split("@")[0],
                    display_name=attrs.get("display_name", ""))
        try:
            _sifre_dogrula(attrs["password"], aday)
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"password": e.detail}) from e
        return attrs


class EmailCodeSerializer(serializers.Serializer):
    """E-posta + 6 haneli kod (kayıt ve şifre sıfırlama doğrulaması)."""

    email = serializers.EmailField(max_length=254)
    code = serializers.CharField(max_length=12, help_text="E-postadaki 6 haneli kod.")


class EmailOnlySerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)


class PasswordResetConfirmSerializer(serializers.Serializer):
    reset_token = serializers.CharField(max_length=500)
    new_password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)


class CodeSentSerializer(serializers.Serializer):
    """Kod gönderildi yanıtı — geri sayım ekranları bu sürelerle kurulur."""

    email = serializers.EmailField()
    gecerlilik_sn = serializers.IntegerField(help_text="Kodun geçerlilik süresi (sn).")
    tekrar_gonderim_sn = serializers.IntegerField(
        help_text="Yeni kod istemeden önce beklenecek süre (sn).")


class ResetTokenSerializer(serializers.Serializer):
    reset_token = serializers.CharField()
    gecerlilik_sn = serializers.IntegerField()


class PreferencesSerializer(serializers.Serializer):
    """Tercih güncelleme (serbest JSON)."""

    preferences = serializers.JSONField()


class FCMTokenSerializer(serializers.Serializer):
    fcm_token = serializers.CharField(
        max_length=500, help_text="Cihazın FCM kayıt token'ı."
    )
    # Opsiyonel: yalnızca admin'de teşhis kolaylığı; hiçbir mantık buna bağlı değil.
    platform = serializers.CharField(
        max_length=10, required=False, allow_blank=True, help_text="ios | android"
    )


# ── Şema/doküman serializer'ları ───────────────────────
# Bu view'lar düz APIView olduğu için drf-spectacular gövdeyi kendi çıkaramaz;
# aşağıdakiler yalnızca OpenAPI + Postman üretimi içindir.
class LoginSerializer(serializers.Serializer):
    """`username` veya `email`den biri + `password` zorunlu."""

    username = serializers.CharField(required=False)
    email = serializers.EmailField(required=False)
    password = serializers.CharField(write_only=True)


class GoogleLoginSerializer(serializers.Serializer):
    id_token = serializers.CharField(help_text="Google Sign-In'den dönen ID token.")


class AppleLoginSerializer(serializers.Serializer):
    identity_token = serializers.CharField(
        help_text="Apple Sign-In'den dönen identity token."
    )
    full_name = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Apple ismi yalnızca ilk girişte gönderir; istemci iletmelidir.",
    )


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(help_text="Kara listeye alınacak refresh token.")
    # Opsiyonel: gönderilirse YALNIZCA bu cihazın push kaydı silinir (kullanıcının
    # başka telefonu varsa o etkilenmez). Eski mobil sürümler göndermez — sorun değil,
    # cihaz bir sonraki girişte zaten yeni hesaba devredilir.
    fcm_token = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
        help_text="Çıkış yapılan cihazın FCM token'ı (opsiyonel).",
    )


class TokenPairSerializer(serializers.Serializer):
    """register / login / social yanıtı."""

    access = serializers.CharField()
    refresh = serializers.CharField()
    user = UserSerializer()
    created = serializers.BooleanField(
        help_text=(
            "Bu istekte yeni bir hesap açıldıysa `true`, mevcut hesaba giriş "
            "yapıldıysa `false`. Sosyal girişte (Google/Apple) tek uç hem kayıt "
            "hem giriş yaptığı için analitik olayını (kayıt vs. giriş) ayırmak "
            "isteyen istemciler bu bayrağa bakmalıdır."
        )
    )


class DetailSerializer(serializers.Serializer):
    """Tek mesajlık yanıt gövdesi."""

    detail = serializers.CharField()


def issue_tokens(user, created=False):
    """
    Bir kullanıcı için access + refresh JWT üret.

    `created`: bu istekte hesap **yeni** açıldıysa True. Sosyal girişte tek uç
    hem kayıt hem giriş yaptığından istemci bunsuz her girişi "login" sayar
    (analitikte kayıt olayı hiç tetiklenmez).
    """
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": UserSerializer(user).data,
        "created": bool(created),
    }
