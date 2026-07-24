"""Core admin kayıtları."""
from django.contrib import admin

from .models import AppConfig, AppSetting, SupportTicket


@admin.register(AppConfig)
class AppConfigAdmin(admin.ModelAdmin):
    """Tekil uygulama durumu (bakım modu + zorunlu güncelleme)."""

    fieldsets = (
        (
            "Bakım Modu",
            {
                "fields": (
                    "maintenance_active",
                    "maintenance_title",
                    "maintenance_message",
                ),
                "description": (
                    "Açıldığında uygulama yalnızca bakım ekranını gösterir, "
                    "başka ekran açılmaz."
                ),
            },
        ),
        (
            "Zorunlu Güncelleme",
            {
                "fields": (
                    "min_ios_version",
                    "min_android_version",
                    "ios_store_url",
                    "android_store_url",
                    "update_title",
                    "update_message",
                ),
                "description": (
                    "Min. sürümü boş bırakmak o platform için kontrolü kapatır. "
                    "Bu sürümün altındaki cihazlar güncellemeye zorlanır."
                ),
            },
        ),
    )
    readonly_fields = ["created_at", "updated_at"]

    def has_add_permission(self, request):
        # Tekil kayıt — yalnızca bir tane olabilir
        return not AppConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    list_display = ["key", "description", "updated_at"]
    search_fields = ["key", "description"]


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ["email", "status", "user", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["email", "phone", "message"]
    list_editable = ["status"]
    readonly_fields = ["created_at", "updated_at"]
