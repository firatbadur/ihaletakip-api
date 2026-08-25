"""accounts admin — özelleştirilmiş kullanıcı yönetimi."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Q
from django.utils import timezone

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



class SubscriptionCancelFilter(admin.SimpleListFilter):
    """
    Aboneliğini iptal eden kullanıcıları süz.

    ⚠️ Katmana (`subscription_tier`) bakarak iptal görülemez: iptal eden kullanıcı
    dönem sonuna kadar **Pro kalır**. Bu yüzden filtre `subscription_cancelled_at`
    izine bakar (RevenueCat webhook'u yazar) ve "hâlâ erişimi var" ile "süresi doldu"yu
    ayırır.
    """

    title = "abonelik iptali"
    parameter_name = "cancel"

    def lookups(self, request, model_admin):
        return [
            ("all", "İptal eden (hepsi)"),
            ("trial", "Ücretsiz deneme iptali"),
            ("paid", "Ücretli abonelik iptali"),
            ("active", "İptal etti · erişimi sürüyor"),
            ("churn", "İptal etti · süresi doldu"),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        cancelled = queryset.filter(subscription_cancelled_at__isnull=False)
        if value == "all":
            return cancelled
        if value == "trial":
            return cancelled.filter(subscription_period_type="TRIAL")
        if value == "paid":
            return cancelled.exclude(subscription_period_type="TRIAL")
        now = timezone.now()
        if value == "active":
            return cancelled.filter(subscription_expires_at__gt=now)
        if value == "churn":
            # Bitiş geçmişte VEYA katman zaten free'ye düşmüş.
            return cancelled.filter(
                Q(subscription_expires_at__lte=now)
                | Q(subscription_expires_at__isnull=True, subscription_tier=User.Tier.FREE)
            )
        return queryset


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = [
        "username",
        "email",
        "display_name",
        "provider",
        "subscription_tier",
        "subscription_status",
        "fcm_token_status",
        "is_active",
        "is_staff",
        "date_joined",
    ]
    list_filter = [
        "subscription_tier",
        SubscriptionCancelFilter,
        "subscription_period_type",
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
                "fields": (
                    "subscription_tier",
                    "subscription_expires_at",
                    "subscription_cancelled_at",
                    "subscription_cancel_reason",
                    "subscription_period_type",
                    "subscription_last_event",
                ),
                "description": (
                    "Pro katman tüm premium özellikleri açar. Bitiş boşsa süresiz; "
                    "doluysa o tarihten sonra otomatik Free'ye düşer.<br>"
                    "İptal alanları RevenueCat webhook'undan yazılır (salt okunur): "
                    "iptal eden kullanıcı dönem sonuna kadar Pro kalır, bu yüzden "
                    "iptal katmandan değil bu alandan görülür."
                ),
            },
        ),
    )


    readonly_fields = [
        "subscription_cancelled_at",
        "subscription_cancel_reason",
        "subscription_period_type",
        "subscription_last_event",
    ]

    @admin.display(description="Abonelik durumu", ordering="subscription_cancelled_at")
    def subscription_status(self, obj):
        """İptal iznini insan diliyle özetler (liste görünümü için)."""
        if obj.subscription_cancelled_at is None:
            return "Pro (aktif)" if obj.is_premium else "—"
        tarih = timezone.localtime(obj.subscription_cancelled_at).strftime("%d.%m.%Y")
        tur = "deneme" if obj.subscription_period_type == "TRIAL" else "ücretli"
        exp = obj.subscription_expires_at
        if exp and exp > timezone.now():
            kalan = timezone.localtime(exp).strftime("%d.%m.%Y")
            return f"✖ İptal ({tur}) · {tarih} → {kalan}'e kadar erişim"
        return f"✖ İptal ({tur}) · {tarih} · süresi doldu"

    @admin.display(description="Push (FCM)", ordering="fcm_token")
    def fcm_token_status(self, obj):
        """Liste görünümünde token durumunu özetler (kısaltılmış)."""
        token = (obj.fcm_token or "").strip()
        if not token:
            return "—"
        short = token if len(token) <= 18 else f"{token[:18]}…"
        return f"✓ {short}"
