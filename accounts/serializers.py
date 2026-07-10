"""accounts serializer'ları."""
from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """Kullanıcı profili (istemciye dönen)."""

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "display_name",
            "photo_url",
            "provider",
            "preferences",
            "date_joined",
        ]
        read_only_fields = ["id", "username", "provider", "date_joined"]


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


class PreferencesSerializer(serializers.Serializer):
    """Tercih güncelleme (serbest JSON)."""

    preferences = serializers.JSONField()


class FCMTokenSerializer(serializers.Serializer):
    fcm_token = serializers.CharField(max_length=500)


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


class TokenPairSerializer(serializers.Serializer):
    """register / login / social yanıtı."""

    access = serializers.CharField()
    refresh = serializers.CharField()
    user = UserSerializer()


class DetailSerializer(serializers.Serializer):
    """Tek mesajlık yanıt gövdesi."""

    detail = serializers.CharField()


def issue_tokens(user):
    """Bir kullanıcı için access + refresh JWT üret."""
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": UserSerializer(user).data,
    }
