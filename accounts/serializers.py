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


def issue_tokens(user):
    """Bir kullanıcı için access + refresh JWT üret."""
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": UserSerializer(user).data,
    }
