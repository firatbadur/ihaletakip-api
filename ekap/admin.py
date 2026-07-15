"""ekap admin — ihale verisi ve senkron gözlemi."""
from django.contrib import admin

from .models import (
    Announcement,
    Authority,
    City,
    Contract,
    ContractSection,
    OkasCode,
    OkasItem,
    SyncCheckpoint,
    SyncRun,
    Tender,
    TenderDate,
)


class TenderDateInline(admin.TabularInline):
    model = TenderDate
    extra = 0


class OkasItemInline(admin.TabularInline):
    model = OkasItem
    extra = 0


class AnnouncementInline(admin.TabularInline):
    model = Announcement
    extra = 0
    fields = ["ilan_tip", "ilan_tarihi", "baslik", "istekli_adi"]


class ContractInline(admin.TabularInline):
    model = Contract
    extra = 0
    fields = ["yuklenici_adi", "sozlesme_bedeli", "yaklasik_maliyet", "sozlesme_tarih"]


@admin.register(Tender)
class TenderAdmin(admin.ModelAdmin):
    list_display = ["ikn", "ihale_adi_kisa", "ihale_il_adi", "ihale_tip", "ihale_durum", "detail_synced_at", "sync_status"]
    list_filter = ["ihale_tip", "ihale_durum", "sync_status", "e_ihale"]
    search_fields = ["ikn", "ekap_id", "ihale_adi", "idare_adi"]
    readonly_fields = ["created_at", "updated_at", "list_synced_at", "detail_synced_at", "detail_raw", "list_raw"]
    inlines = [TenderDateInline, OkasItemInline, AnnouncementInline, ContractInline]
    date_hierarchy = "ihale_tarihi"

    @admin.display(description="İhale Adı")
    def ihale_adi_kisa(self, obj):
        return (obj.ihale_adi or "")[:70]


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ["tender", "yuklenici_adi", "sozlesme_bedeli", "yaklasik_maliyet"]
    search_fields = ["tender__ikn", "yuklenici_adi"]
    raw_id_fields = ["tender"]


@admin.register(OkasCode)
class OkasCodeAdmin(admin.ModelAdmin):
    list_display = ["kod", "adi"]
    search_fields = ["kod", "adi", "adi_eng"]


@admin.register(Authority)
class AuthorityAdmin(admin.ModelAdmin):
    list_display = ["detsis_no", "ad", "idare_id", "parent_detsis", "has_items", "seviye"]
    search_fields = ["detsis_no", "ad", "idare_id"]
    list_filter = ["has_items", "seviye"]


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ["ekap_il_id", "plaka", "ad", "is_big_city"]
    search_fields = ["ad"]
    list_filter = ["is_big_city"]


@admin.register(SyncCheckpoint)
class SyncCheckpointAdmin(admin.ModelAdmin):
    list_display = ["name", "cursor_skip", "oldest_date", "newest_date", "done", "updated_at"]


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ["task", "started_at", "finished_at", "status", "items", "errors"]
    list_filter = ["task", "status"]
    readonly_fields = ["task", "started_at", "finished_at", "status", "items", "errors", "note"]


admin.site.register(ContractSection)
