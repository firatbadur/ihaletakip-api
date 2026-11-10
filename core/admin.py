"""Core admin kayıtları."""
from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.utils.html import format_html, format_html_join, linebreaks
from django.utils.safestring import mark_safe

from . import support
from .models import AppConfig, AppSetting, SupportMessage, SupportTicket


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


class SupportTicketForm(forms.ModelForm):
    # Model alanı DEĞİL: doluysa kaydederken destek mesajı olarak eklenir + kullanıcıya
    # bildirim/push gider (bkz. core.support.destek_yaniti).
    yeni_yanit = forms.CharField(
        label="Yanıtınız",
        required=False,
        widget=forms.Textarea(attrs={"rows": 6, "placeholder": "Kullanıcıya yanıt yazın…"}),
        help_text=(
            "Kaydedince yanıt yazışmaya eklenir, kullanıcıya uygulama içi bildirim ve push "
            "gider. Durum 'Bekliyor' ise kendiliğinden 'Yanıtlandı' olur; 'İşlemde' ya da "
            "'Kapandı' seçerseniz o durum kalır."
        ),
    )

    class Meta:
        model = SupportTicket
        fields = ["status"]


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    form = SupportTicketForm
    list_display = [
        "talep", "email", "category", "status", "mesaj_sayisi", "okundu", "last_message_at",
    ]
    list_display_links = ["talep"]
    list_filter = ["status", "category", "user_unread", "created_at"]
    search_fields = ["email", "phone", "message", "messages__body"]
    list_editable = ["status"]
    readonly_fields = [
        "yazisma", "user", "email", "phone", "category", "cihaz", "created_at",
        "last_message_at",
    ]
    fieldsets = (
        ("Yazışma", {"fields": ("yazisma",)}),
        ("Yanıt", {"fields": ("yeni_yanit", "status")}),
        (
            "Talep",
            {"fields": ("user", "email", "phone", "category", "cihaz", "created_at",
                        "last_message_at")},
        ),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_mesaj=Count("messages"))

    def has_add_permission(self, request):
        return False  # talepleri kullanıcılar açar

    @admin.display(description="Talep")
    def talep(self, obj):
        return f"#{obj.pk} · {support.onizleme(obj.message, 70)}"

    @admin.display(description="Mesaj", ordering="_mesaj")
    def mesaj_sayisi(self, obj):
        return obj._mesaj

    @admin.display(description="Kullanıcı gördü", boolean=True)
    def okundu(self, obj):
        # Son destek yanıtını kullanıcı uygulamada açtı mı? (yanıt yoksa da "evet")
        return not obj.user_unread

    @admin.display(description="Cihaz")
    def cihaz(self, obj):
        m = obj.meta or {}
        if not m:
            return "—"
        return " · ".join(
            str(m[k]) for k in ("platform", "os_version", "device_model", "app_version", "build")
            if m.get(k)
        )

    @admin.display(description="")
    def yazisma(self, obj):
        mesajlar = list(obj.messages.select_related("author"))
        if not mesajlar:
            return "—"

        def balon(m):
            ekip = m.sender == SupportMessage.SENDER_STAFF
            kim = (
                f"Destek · {m.author.get_full_name() or m.author.email}" if ekip and m.author
                else "Destek" if ekip
                else (obj.email or "Kullanıcı")
            )
            return (
                "margin-left:auto;background:#EAF3FB;border-color:#B9D6F2" if ekip
                else "margin-right:auto;background:#F6F8FB;border-color:#E2E8F0",
                kim,
                m.created_at.strftime("%d.%m.%Y %H:%M"),
                # Gövde önce kaçırılır (autoescape=True), sonra <p>/<br> güvenli işaretlenir.
                mark_safe(linebreaks(m.body, autoescape=True)),
            )

        return format_html(
            '<div style="display:flex;flex-direction:column;gap:10px;max-width:760px">{}</div>',
            format_html_join(
                "",
                '<div style="{};max-width:80%;border:1px solid;border-radius:12px;'
                'padding:10px 14px;color:#1E293B">'
                '<div style="font-size:12px;color:#5D6B80;margin-bottom:4px">'
                "<b>{}</b> · {}</div><div>{}</div></div>",
                (balon(m) for m in mesajlar),
            ),
        )

    def save_model(self, request, obj, form, change):
        yanit = (form.cleaned_data.get("yeni_yanit") or "").strip()
        if not yanit:
            super().save_model(request, obj, form, change)
            return
        durum = obj.status if obj.status in ("İşlemde", "Kapandı") else "Yanıtlandı"
        support.destek_yaniti(obj, yanit, author=request.user, status=durum)
        obj.refresh_from_db()
        messages.success(
            request,
            f"Yanıt #{obj.pk} talebine eklendi; kullanıcıya bildirim ve push gönderiliyor.",
        )


# ── Güvenlik katmanı (core/guvenlik.py) ─────────────────
from datetime import timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from . import guvenlik  # noqa: E402
from .models import ErisimEngeli, GuvenlikOlayi  # noqa: E402


class ErisimEngeliForm(forms.ModelForm):
    class Meta:
        model = ErisimEngeli
        fields = ["tur", "deger", "kullanici", "sebep", "bitis"]

    def clean(self):
        veri = super().clean()
        tur, deger = veri.get("tur"), (veri.get("deger") or "").strip()
        if self.instance.pk:
            return veri
        try:
            if tur == ErisimEngeli.TUR_IP:
                guvenlik.ag_coz(deger)
            elif tur == ErisimEngeli.TUR_KULLANICI:
                if not veri.get("kullanici"):
                    raise forms.ValidationError("Hesap engeli için hesap seçin.")
                if veri["kullanici"].is_superuser:
                    raise forms.ValidationError("Süper kullanıcı askıya alınamaz.")
            elif tur == ErisimEngeli.TUR_EPOSTA_ALANI and "." not in deger:
                raise forms.ValidationError("Geçerli bir alan adı girin.")
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e
        return veri


@admin.register(ErisimEngeli)
class ErisimEngeliAdmin(admin.ModelAdmin):
    """
    ⚠️ Ekleme/kaldırma `core.guvenlik` üzerinden yapılır: hesap engeli `is_active`'i kapatır,
    IP listesi süreç önbelleğini sürüm anahtarıyla yeniler. Satırı SİLMEK bunları yapmaz →
    silme kapalı, "Engeli kaldır" eylemi kullanılır.
    """

    form = ErisimEngeliForm
    list_display = ["tur", "hedef", "aktif", "bitis", "isabet_sayisi", "son_isabet",
                    "olusturan", "created_at"]
    list_filter = ["tur", "aktif"]
    search_fields = ["deger", "sebep", "kullanici__email", "kullanici__username"]
    raw_id_fields = ["kullanici"]
    readonly_fields = ["aktif", "onceki_aktif", "olusturan", "isabet_sayisi",
                       "son_isabet", "kaldirilma", "created_at", "updated_at"]
    actions = ["engeli_kaldir"]

    @admin.display(description="Hedef")
    def hedef(self, obj):
        return obj.deger or (obj.kullanici and (obj.kullanici.email or obj.kullanici.username))

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        ro = list(self.readonly_fields)
        if obj is not None:
            ro += ["tur", "deger", "kullanici"]
        return ro

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            guvenlik.degisti()
            return
        sure = None
        if obj.bitis:
            sure = obj.bitis - timezone.now()
        if obj.tur == ErisimEngeli.TUR_KULLANICI:
            yeni = guvenlik.hesap_engelle(obj.kullanici, sebep=obj.sebep, sure=sure,
                                          olusturan=request.user)
        elif obj.tur == ErisimEngeli.TUR_IP:
            yeni = guvenlik.ip_engelle(obj.deger, sebep=obj.sebep, sure=sure,
                                       olusturan=request.user)
        else:
            yeni = guvenlik.eposta_alani_engelle(obj.deger, sebep=obj.sebep,
                                                 olusturan=request.user)
        obj.pk = yeni.pk
        obj.__dict__.update({k: v for k, v in yeni.__dict__.items() if not k.startswith("_")})

    @admin.action(description="Seçili engelleri kaldır")
    def engeli_kaldir(self, request, queryset):
        n = 0
        for e in queryset.filter(aktif=True):
            guvenlik.engeli_kaldir(e, kaldiran=request.user)
            n += 1
        self.message_user(request, f"{n} engel kaldırıldı.", messages.SUCCESS)


@admin.register(GuvenlikOlayi)
class GuvenlikOlayiAdmin(admin.ModelAdmin):
    list_display = ["zaman", "tur", "kimlik", "kullanici", "ip", "istemci_turu", "yol"]
    list_filter = ["tur", "istemci_turu"]
    search_fields = ["kimlik", "ip", "kullanici__email", "ua", "yol"]
    date_hierarchy = "zaman"
    list_select_related = ["kullanici"]
    actions = ["hesabi_askiya_al", "ipyi_engelle"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.action(description="Olaydaki hesapları 7 gün askıya al")
    def hesabi_askiya_al(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, "Yalnızca süper kullanıcı.", messages.ERROR)
            return
        n = 0
        for o in queryset.exclude(kullanici=None).select_related("kullanici"):
            if o.kullanici.is_superuser:
                continue
            guvenlik.hesap_engelle(o.kullanici, sebep=f"Olay #{o.pk} ({o.tur})",
                                   sure=timedelta(days=7), olusturan=request.user)
            n += 1
        self.message_user(request, f"{n} hesap askıya alındı.", messages.SUCCESS)

    @admin.action(description="Olaydaki IP'leri 7 gün engelle")
    def ipyi_engelle(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, "Yalnızca süper kullanıcı.", messages.ERROR)
            return
        n, hata = 0, []
        for ip in set(queryset.exclude(ip=None).values_list("ip", flat=True)):
            if guvenlik.ip_engeli(ip):
                continue
            try:
                guvenlik.ip_engelle(ip, sebep="Güvenlik olayından", sure=timedelta(days=7),
                                    olusturan=request.user)
                n += 1
            except ValueError as e:
                hata.append(f"{ip}: {e}")
        self.message_user(request, f"{n} IP engellendi." + (f" Atlanan: {'; '.join(hata)}"
                                                            if hata else ""),
                          messages.SUCCESS if not hata else messages.WARNING)


from .models import TopluGonderim, TopluGonderimAlici  # noqa: E402


@admin.register(TopluGonderim)
class TopluGonderimAdmin(admin.ModelAdmin):
    """Toplu gönderim kayıtları (salt okunur). Yeni gönderim: üst menü → Toplu Gönderim."""

    list_display = ["created_at", "kanal", "konu", "hedef_ozet", "durum", "toplam",
                    "gonderilen", "atlanan", "hatali", "olusturan"]
    list_filter = ["kanal", "durum"]
    search_fields = ["konu"]
    list_select_related = ["olusturan"]
    readonly_fields = [f.name for f in TopluGonderim._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(TopluGonderimAlici)
class TopluGonderimAliciAdmin(admin.ModelAdmin):
    """Alıcı satırları — "şu kullanıcıya neden gitmedi" sorusunun ekranı."""

    list_display = ["gonderim", "user", "durum", "sebep", "islem_at"]
    list_filter = ["durum", "gonderim"]
    search_fields = ["user__email"]
    list_select_related = ["gonderim", "user"]
    raw_id_fields = ["gonderim", "user"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
