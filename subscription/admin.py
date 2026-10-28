"""subscription admin — Pro hakları, web ödemeleri (istisnai iade), fatura profilleri."""
import logging

from django import forms
from django.contrib import admin, messages
from django.db import transaction

from .models import BillingProfile, Payment, ProGrant

logger = logging.getLogger("ihaletakip")


class _AdminHakFormu(forms.ModelForm):
    class Meta:
        model = ProGrant
        fields = ["starts_at", "ends_at", "revoked_at", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.source = ProGrant.Source.ADMIN  # bu inline yalnız admin satırı yazar


class AdminProGrantInline(admin.TabularInline):
    """Elle Pro = yeni satır (kaynak otomatik `admin`). Bitiş boş = süresiz.

    Kaydedince etkin katman `recompute_pro` ile yeniden hesaplanır
    (`accounts.admin.UserAdmin.save_related`). ⚠️ Satır silinmez; geri almak için
    "iptal" alanını doldurun.
    """

    model = ProGrant
    form = _AdminHakFormu
    extra = 0
    can_delete = False
    ordering = ["-starts_at"]
    verbose_name = "elle verilen Pro hakkı"
    verbose_name_plural = "Elle verilen Pro hakları (admin)"

    def get_queryset(self, request):
        return super().get_queryset(request).filter(source=ProGrant.Source.ADMIN)


class KaynakProGrantInline(admin.TabularInline):
    """Mağaza (RevenueCat) ve web (iyzico) hakları — SALT OKUNUR: elle değiştirmek bir
    sonraki senkronda ezilir ya da ödeme kaydıyla çelişir. Web iadesi → Web ödemeleri."""

    model = ProGrant
    extra = 0
    can_delete = False
    fields = ["source", "starts_at", "ends_at", "revoked_at", "payment"]
    readonly_fields = fields
    ordering = ["-starts_at"]
    verbose_name = "mağaza / web Pro hakkı"
    verbose_name_plural = "Mağaza ve web Pro hakları (salt okunur)"

    def get_queryset(self, request):
        return (super().get_queryset(request).exclude(source=ProGrant.Source.ADMIN)
                .select_related("payment"))

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ProGrant)
class ProGrantAdmin(admin.ModelAdmin):
    list_display = ["user", "source", "starts_at", "ends_at", "revoked_at", "payment"]
    list_filter = ["source"]
    list_select_related = ["user", "payment"]
    search_fields = ["user__email", "user__username"]
    raw_id_fields = ["user", "payment"]
    readonly_fields = ["hatirlatmalar", "created_at", "updated_at"]

    def has_add_permission(self, request):
        return False  # elle Pro kullanıcı ekranından (inline) verilir

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        from .services.pro_hak import recompute_pro

        super().save_model(request, obj, form, change)
        recompute_pro(obj.user)


@admin.action(description="Seçili ödemeleri İADE ET (istisnai — çifte çekim, Pro açılamadı…)")
def iade_et(modeladmin, request, queryset):
    from .services.odeme import OdemeHatasi
    from .services.odeme import iade_et as _iade

    for odeme in queryset.select_related("user"):
        try:
            _iade(odeme, ip=request.META.get("HTTP_X_REAL_IP") or "")
        except OdemeHatasi as e:
            messages.error(request, f"#{odeme.pk}: {e.mesaj}")
        except Exception as e:  # noqa: BLE001 — iyzico ağı vb.
            logger.exception("admin iade başarısız odeme=%s", odeme.pk)
            messages.error(request, f"#{odeme.pk}: iade yapılamadı ({e})")
        else:
            messages.success(request, f"#{odeme.pk}: iade edildi, Pro hakkı kapatıldı.")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Web ödemeleri. ⚠️ Tutar/durum/iyzico alanları salt okunur — para kaydı elle
    düzeltilmez. Yazılabilir olan yalnızca FATURA alanlarıdır: entegratör seçilene kadar
    fatura muhasebede kesilip buraya yüklenir; ilk yüklemede "Fatura hazır" e-postası gider.
    """

    list_display = ["id", "user", "amount", "status", "paid_at", "card_last4",
                    "invoice_number", "cancel_requested_at", "created_at"]
    list_filter = ["status"]
    list_select_related = ["user"]
    search_fields = ["user__email", "iyzico_payment_id", "iyzico_token", "invoice_number"]
    date_hierarchy = "created_at"
    actions = [iade_et]
    fields = [
        "user", "plan_code", "months", "amount", "vat_rate", "currency", "status",
        "paid_at", "card_association", "card_last4", "iyzico_payment_id",
        "iyzico_transaction_id", "iyzico_token", "error_code", "error_message",
        "billing_snapshot", "consents", "client_ip", "cancel_requested_at", "refunded_at",
        "invoice_number", "invoice_url", "invoice_pdf", "created_at", "updated_at",
    ]
    _YAZILABILIR = {"invoice_number", "invoice_url", "invoice_pdf"}

    def get_readonly_fields(self, request, obj=None):
        return [f for f in self.fields if f not in self._YAZILABILIR]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        ilk_fatura = "invoice_pdf" in form.changed_data and bool(obj.invoice_pdf) and (
            not form.initial.get("invoice_pdf"))
        super().save_model(request, obj, form, change)
        if ilk_fatura and obj.status == Payment.Status.SUCCESS:
            from .tasks import odeme_eposta_task

            transaction.on_commit(lambda: odeme_eposta_task.delay(obj.pk, "fatura"))
            messages.info(request, "Fatura yüklendi; kullanıcıya e-postayla gönderiliyor.")


@admin.register(BillingProfile)
class BillingProfileAdmin(admin.ModelAdmin):
    """⚠️ KVKK: TCKN/VKN içerir — yalnızca fatura işi için açın."""

    list_display = ["user", "type", "full_name", "company_name", "city", "updated_at"]
    list_filter = ["type"]
    list_select_related = ["user"]
    search_fields = ["user__email", "company_name", "full_name"]
    raw_id_fields = ["user"]
