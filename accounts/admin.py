"""accounts admin — özelleştirilmiş kullanıcı yönetimi."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Q
from django.utils import timezone

from .models import EpostaEngeli, EpostaGeriBildirim, EpostaKodu, PushDevice, User, WebHandoff


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


class EpostaEngelliFilter(admin.SimpleListFilter):
    """Adresine e-posta gönderilmeyen (kalıcı bounce / şikâyet / elle) kullanıcılar."""

    title = "e-posta engeli"
    parameter_name = "eposta_engeli"

    def lookups(self, request, model_admin):
        return [("evet", "Engelli (e-posta gitmiyor)"), ("hayir", "Engelsiz")]

    def queryset(self, request, queryset):
        from django.db.models import Exists, OuterRef
        from django.db.models.functions import Lower

        var = Exists(EpostaEngeli.objects.filter(aktif=True, email=Lower(OuterRef("email"))))
        if self.value() == "evet":
            return queryset.filter(var)
        if self.value() == "hayir":
            return queryset.exclude(var)
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
        EpostaEngelliFilter,
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
    actions = ["askiya_al_7g", "askiya_al_suresiz", "askiyi_kaldir"]

    # ── Hesap askıya alma (core.guvenlik) ──────────────────
    # ⚠️ `is_active` kutusunu elle kapatmak yerine bunlar kullanılır: engel kaydı (sebep,
    # süre, ekleyen) tutulur, yenileme token'ları kara listeye alınır, süre dolunca geri açılır.
    def _askiya(self, request, queryset, sure):
        from datetime import timedelta

        from django.contrib import messages

        from core import guvenlik

        if not request.user.is_superuser:
            self.message_user(request, "Yalnızca süper kullanıcı.", messages.ERROR)
            return
        n = 0
        for u in queryset.exclude(is_superuser=True):
            guvenlik.hesap_engelle(u, sebep="Yönetim panelinden",
                                   sure=timedelta(days=sure) if sure else None,
                                   olusturan=request.user)
            n += 1
        self.message_user(request, f"{n} hesap askıya alındı.", messages.SUCCESS)

    @admin.action(description="Askıya al (7 gün)")
    def askiya_al_7g(self, request, queryset):
        self._askiya(request, queryset, 7)

    @admin.action(description="Askıya al (süresiz)")
    def askiya_al_suresiz(self, request, queryset):
        self._askiya(request, queryset, None)

    @admin.action(description="Askıyı kaldır")
    def askiyi_kaldir(self, request, queryset):
        from django.contrib import messages

        from core import guvenlik
        from core.models import ErisimEngeli

        n = 0
        for e in ErisimEngeli.objects.filter(tur=ErisimEngeli.TUR_KULLANICI, aktif=True,
                                             kullanici__in=queryset):
            guvenlik.engeli_kaldir(e, kaldiran=request.user)
            n += 1
        self.message_user(request, f"{n} askı kaldırıldı.", messages.SUCCESS)

    fieldsets = BaseUserAdmin.fieldsets + (
        (
            "IhaleTakip Profili",
            {
                "fields": (
                    "display_name",
                    "photo_url",
                    "provider",
                    "provider_uid",
                    "email_verified_at",
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
        "email_verified_at",
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


@admin.register(EpostaKodu)
class EpostaKoduAdmin(admin.ModelAdmin):
    """Doğrulama kodları — destek için "kod gelmedi / kilitlendi" ekranı.

    ⚠️ Kod görünmez (yalnızca özeti saklanır). Satırı SİLMEK o e-postanın bekleme ve
    saatlik sınırını sıfırlar — kullanıcı kilitlendiyse meşru bir destek işlemidir.
    """

    list_display = ["email", "amac", "son_gonderim", "gecerlilik", "deneme",
                    "gonderim", "kullanildi_at"]
    list_filter = ["amac"]
    search_fields = ["email"]
    ordering = ["-son_gonderim"]
    fields = ["email", "amac", "son_gonderim", "gecerlilik", "deneme", "gonderim",
              "pencere_bas", "kullanildi_at", "created_at"]
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EpostaGeriBildirim)
class EpostaGeriBildirimAdmin(admin.ModelAdmin):
    """SES bounce/şikâyet kayıtları — "kullanıcıya mail gitmiyor" sorusunun ekranı."""

    list_display = ["email", "tur", "alt_tur", "bulten_kapatildi", "created_at"]
    list_filter = ["tur", "bulten_kapatildi"]
    search_fields = ["email"]
    readonly_fields = ["email", "tur", "alt_tur", "aciklama", "ses_mesaj_id",
                       "bulten_kapatildi", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EpostaEngeli)
class EpostaEngeliAdmin(admin.ModelAdmin):
    """E-posta gönderilmeyen adresler. Kalıcı bounce / şikâyet kendiliğinden düşer.

    ⚠️ Satır silinmez — "Engeli kaldır" eylemi `aktif=False` yapar (iz kalır). Adres yine
    geri dönerse engel kendiliğinden tekrar etkinleşir.
    """

    list_display = ["email", "sebep", "aktif", "sayac", "created_at", "updated_at",
                    "kaldirildi_at", "kaldiran"]
    list_filter = ["aktif", "sebep"]
    search_fields = ["email"]
    readonly_fields = ["sayac", "created_at", "updated_at", "kaldirildi_at", "kaldiran"]
    fields = ["email", "sebep", "aciklama", "aktif", *readonly_fields]
    actions = ["engeli_kaldir"]

    def get_readonly_fields(self, request, obj=None):
        # Var olan satırda adres değişmez; elle yeni engel eklerken yazılabilir.
        return [*self.readonly_fields, "email"] if obj else self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        from .services.eposta_engel import normalize

        obj.email = normalize(obj.email)
        if not change:
            obj.sebep = obj.sebep or EpostaEngeli.Sebep.ELLE
        super().save_model(request, obj, form, change)

    @admin.action(description="Engeli kaldır (bu adreslere yeniden e-posta gönderilir)")
    def engeli_kaldir(self, request, queryset):
        from .services.eposta_engel import engeli_kaldir

        n = 0
        for satir in queryset.filter(aktif=True):
            engeli_kaldir(satir, kaldiran=request.user)
            n += 1
        self.message_user(request, f"{n} adresin engeli kaldırıldı.")


@admin.register(WebHandoff)
class WebHandoffAdmin(admin.ModelAdmin):
    """Web → mobil geçiş kodları — "QR'ı okuttum ama uygulama açılmadı" sorusunun ekranı.

    ⚠️ Kodlar görünmez (yalnızca özetleri saklanır). Salt okunur; satır makine üretimi.
    """

    list_display = ["user", "durum_gosterim", "baglam", "created_at", "okutuldu_at",
                    "kullanildi_at", "gecerlilik"]
    list_select_related = ["user"]
    list_filter = ["durum", "baglam"]
    search_fields = ["user__email", "user__username"]
    ordering = ["-created_at"]
    fields = ["user", "durum", "baglam", "gecerlilik", "created_at", "olusturan_ip",
              "okutuldu_at", "kullanildi_at", "kullanan_ip", "iptal_at"]
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Durum", ordering="durum")
    def durum_gosterim(self, obj):
        return "Süresi doldu" if obj.etkin_durum == WebHandoff.SURESI_DOLDU else obj.get_durum_display()
