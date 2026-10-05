"""accounts admin — özelleştirilmiş kullanıcı yönetimi."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Q
from django.utils import timezone

from .models import PushDevice, User


class HasFcmTokenFilter(admin.SimpleListFilter):
    """FCM push token'ı olan/olmayan kullanıcıları süz."""

    title = "FCM token"
    parameter_name = "has_fcm"

    def lookups(self, request, model_admin):
        return [("yes", "Token var"), ("no", "Token yok")]

    def queryset(self, request, queryset):
        # ⚠️ `push_devices` çoğul: bir kullanıcının birden çok cihazı olabilir (0008).
        # "var" dalında `.distinct()` ŞART — JOIN aksi hâlde satırı çoğaltır.
        if self.value() == "yes":
            return queryset.filter(push_devices__isnull=False).distinct()
        if self.value() == "no":
            return queryset.filter(push_devices__isnull=True)
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
        # ⚠️ "Erişimi sürüyor" = `User.is_premium`'in SQL karşılığı: katman pro VE
        # (bitiş boş [süresiz/deneme] VEYA gelecekte). Yalnızca `expires_at__gt=now`
        # demek, bitişi bilinmeyen deneme kullanıcılarını sessizce churn'e düşürürdü.
        now = timezone.now()
        erisim = Q(subscription_tier=User.Tier.PRO) & (
            Q(subscription_expires_at__isnull=True) | Q(subscription_expires_at__gt=now)
        )
        if value == "active":
            return cancelled.filter(erisim)
        if value == "churn":
            return cancelled.exclude(erisim)
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
        "onboarding_status",
        "age_range",
        "is_active",
        "is_staff",
        "is_superuser",
    ]
    # ⚠️ `push_devices__token`: token artık User'da değil (0008). İlişkili alanda
    # arama JOIN üretir ama admin aramaları zaten seçicidir.
    search_fields = [
        "username", "email", "display_name", "provider_uid", "push_devices__token",
    ]
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
                    "deactivated_at",
                    "age_range",
                    "onboarding_status",
                    "onboarding_completed_at",
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
        # Backfill izinde tarih YAKLAŞIKTIR (RC v2 iptal anını vermiyor) → "≈" ile işaretle.
        if obj.subscription_last_event == "BACKFILL":
            tarih = f"≈{tarih}"
        tur = "deneme" if obj.subscription_period_type == "TRIAL" else "ücretli"
        if obj.is_premium:
            exp = obj.subscription_expires_at
            if exp:
                kalan = timezone.localtime(exp).strftime("%d.%m.%Y")
                return f"✖ İptal ({tur}) · {tarih} → {kalan}'e kadar erişim"
            return f"✖ İptal ({tur}) · {tarih} · erişim sürüyor"
        return f"✖ İptal ({tur}) · {tarih} · süresi doldu"

    def get_queryset(self, request):
        # ⚠️ `fcm_token_status` satır başına cihaz okuyor → PREFETCH ŞART.
        # Olmadan sayfa başına 50+ ek sorgu (bu kod tabanında belgelenmiş sessiz N+1).
        return super().get_queryset(request).prefetch_related("push_devices")

    @admin.display(description="Push (FCM)")
    def fcm_token_status(self, obj):
        """Liste görünümünde kayıtlı cihazları özetler.

        ⚠️ `ordering` YOK: değer artık ilişkili tablodan geliyor, tek kolonla
        sıralanamaz. Sıralama isteniyorsa `annotate(Count(...))` gerekir — bu liste
        için gereksiz bir sorgu olurdu.
        """
        # ⚠️ `.all()` + len(): prefetch önbelleğinden okur. `.count()` ya da
        # dilimleme (`[:3]`) prefetch'i ATLAYIP yeni sorgu atardı.
        cihazlar = list(obj.push_devices.all())
        if not cihazlar:
            return "—"
        ilk = cihazlar[0].token
        short = ilk if len(ilk) <= 18 else f"{ilk[:18]}…"
        return f"✓ {short}" + (f" (+{len(cihazlar) - 1} cihaz)" if len(cihazlar) > 1 else "")


@admin.register(PushDevice)
class PushDeviceAdmin(admin.ModelAdmin):
    """Kayıtlı push cihazları — "bu kişi neden bildirim almıyor" sorusunun ekranı.

    ⚠️ Satırlar makine üretimi (`POST /auth/fcm-token/`). Elle eklemek/düzenlemek
    anlamsız: token cihazdan gelir. Salt okunur tutuluyor; silme serbest (ölü bir
    kaydı elle temizlemek meşru bir operasyon).
    """

    list_display = ["token_kisa", "user", "platform", "last_registered_at"]
    list_select_related = ["user"]
    search_fields = ["token", "user__email", "user__username"]
    list_filter = ["platform"]
    ordering = ["-last_registered_at"]
    readonly_fields = ["token", "user", "platform", "last_registered_at", "created_at", "updated_at"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Token", ordering="token")
    def token_kisa(self, obj):
        return obj.token if len(obj.token) <= 24 else f"{obj.token[:24]}…"
