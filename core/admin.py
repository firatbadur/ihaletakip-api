"""Core admin kayıtları."""
from django.contrib import admin, messages

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

    def save_model(self, request, obj, form, change):
        """EKAP doğrulama çerezi buradan da yenilenebilsin (SSH gerekmesin).

        ⚠️ Ham `Cookie` başlığı doğrudan yazılamaz: analitik çerezlerin
        ayıklanması (kaydeden kişinin izini saklamayalım) ve "doğrulama düştü"
        bayrağının temizlenmesi gerekir. Bu yüzden yazma `ekap.session.kaydet`
        üzerinden geçirilir — komut satırıyla **tek** kod yolu.
        """
        from ekap import session as ekap_session

        if obj.key == ekap_session.ANAHTAR_CEREZ and obj.value:
            try:
                ekap_session.kaydet(obj.value)
            except ValueError as e:
                messages.error(request, f"Çerez kaydedilemedi: {e}")
                return
            # `kaydet` update_or_create ile yazdı; admin'in sonraki adımları
            # (response_add/change) pk bekliyor → satırı geri okuyup bağla.
            kayit = AppSetting.objects.filter(key=obj.key).first()
            if kayit:
                obj.pk = kayit.pk
            messages.success(
                request,
                "EKAP doğrulama çerezi kaydedildi. Toplama görevleri bir sonraki "
                "turda kendiliğinden devam edecek."
            )
            return
        super().save_model(request, obj, form, change)


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ["email", "status", "user", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["email", "phone", "message"]
    list_editable = ["status"]
    readonly_fields = ["created_at", "updated_at"]
