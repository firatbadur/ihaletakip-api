"""accounts admin — özelleştirilmiş kullanıcı yönetimi."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Q

from .models import User


class HasFcmTokenFilter(admin.SimpleListFilter):
    """FCM push token'ı olan/olmayan kullanıcıları süz."""

    title = "FCM token"
    parameter_name = "has_fcm"

    def lookups(self, request, model_admin):
        return [("yes", "Token var"), ("no", "Token yok")]

    def queryset(self, request, queryset):
        empty = Q(fcm_token="") | Q(fcm_token__isnull=True)
        if self.value() == "yes":
            return queryset.exclude(empty)
        if self.value() == "no":
            return queryset.filter(empty)
        return queryset


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = [
        "username",
        "email",
        "display_name",
        "provider",
        "subscription_tier",
        "fcm_token_status",
        "is_active",
        "is_staff",
        "date_joined",
    ]
    list_filter = [
        "subscription_tier",
        HasFcmTokenFilter,
        "provider",
        "is_active",
        "is_staff",
        "is_superuser",
    ]
    search_fields = ["username", "email", "display_name", "provider_uid", "fcm_token"]
    ordering = ["-date_joined"]

    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "IhaleTakip Profili",
            {
                "fields": (
                    "display_name",
                    "photo_url",
                    "provider",
                    "provider_uid",
                    "preferences",
                    "fcm_token",
                    "deactivated_at",
                )
            },
        ),
        (
            "Abonelik",
            {
                "fields": ("subscription_tier", "subscription_expires_at"),
                "description": (
                    "Pro katman tüm premium özellikleri açar. Bitiş boşsa süresiz; "
                    "doluysa o tarihten sonra otomatik Free'ye düşer."
                ),
            },
        ),
    )

    @admin.display(description="Push (FCM)", ordering="fcm_token")
    def fcm_token_status(self, obj):
        """Liste görünümünde token durumunu özetler (kısaltılmış)."""
        token = (obj.fcm_token or "").strip()
        if not token:
            return "—"
        short = token if len(token) <= 18 else f"{token[:18]}…"
        return f"✓ {short}"
