"""accounts admin — özelleştirilmiş kullanıcı yönetimi."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = [
        "username",
        "email",
        "display_name",
        "provider",
        "is_active",
        "is_staff",
        "date_joined",
    ]
    list_filter = ["provider", "is_active", "is_staff", "is_superuser"]
    search_fields = ["username", "email", "display_name", "provider_uid"]
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
    )
