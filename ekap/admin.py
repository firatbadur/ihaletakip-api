"""ekap admin — ihale verisi ve senkron gözlemi."""
from django.contrib import admin

from .models import (
    Announcement,
    Authority,
    City,
    Contract,
    Contractor,
    ContractorAlias,
    ContractorMembership,
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
    list_display = [
        "tender", "yuklenici_adi", "sozlesme_bedeli_num", "yaklasik_maliyet_num",
        "indirim_orani", "sozlesme_tarihi", "yuklenici",
    ]
    list_filter = ["yaklasik_maliyet_kaynak", "ihale_tip"]
    search_fields = ["tender__ikn", "yuklenici_adi", "ekap_sozlesme_id"]
    raw_id_fields = ["tender", "yuklenici"]
    date_hierarchy = "sozlesme_tarihi"


class ContractorAliasInline(admin.TabularInline):
    model = ContractorAlias
    extra = 0
    fields = ["ham_ad", "kaynak", "son_gorulme"]
    readonly_fields = ["son_gorulme"]


class ContractorMembershipInline(admin.TabularInline):
    """Ortak girişimin üyeleri."""

    model = ContractorMembership
    fk_name = "ortak_girisim"
    extra = 0
    fields = ["uye", "sira", "pilot", "guven", "kaynak_metin"]
    raw_id_fields = ["uye"]


@admin.register(Contractor)
class ContractorAdmin(admin.ModelAdmin):
    list_display = [
        "kanonik_ad_kisa", "kind", "sozlesme_sayisi", "ihale_sayisi", "idare_sayisi",
        "toplam_sozlesme_bedeli", "ortalama_indirim_orani", "il_adi", "son_sozlesme_tarihi",
    ]
    # `uyeleri_cozumlendi=False` → ortak girişim üyeleri güvenle ayrıştırılamadı,
    # elle inceleme bekliyor (bkz. contractors.split_joint_venture yazma politikası).
    list_filter = ["kind", "uyeleri_cozumlendi", "tuzel_tip"]
    search_fields = ["kanonik_ad", "kanonik_anahtar", "arama_norm", "aliaslar__ham_ad"]
    readonly_fields = [
        "kanonik_anahtar", "arama_norm", "sozlesme_sayisi", "ihale_sayisi", "idare_sayisi",
        "toplam_sozlesme_bedeli", "ilk_sozlesme_tarihi", "son_sozlesme_tarihi",
        "ortalama_indirim_orani", "indirim_orani_ornek_sayisi", "uye_sayisi",
        "ortak_girisim_sayisi", "agrega_guncelleme", "ilk_gorulme", "updated_at",
    ]
    inlines = [ContractorAliasInline, ContractorMembershipInline]

    @admin.display(description="Yüklenici")
    def kanonik_ad_kisa(self, obj):
        return (obj.kanonik_ad or "")[:60]


@admin.register(ContractorAlias)
class ContractorAliasAdmin(admin.ModelAdmin):
    list_display = ["ham_ad", "contractor", "kaynak", "son_gorulme"]
    list_filter = ["kaynak"]
    search_fields = ["ham_ad", "ham_ad_norm", "contractor__kanonik_ad"]
    raw_id_fields = ["contractor"]


@admin.register(ContractorMembership)
class ContractorMembershipAdmin(admin.ModelAdmin):
    list_display = ["ortak_girisim", "uye", "sira", "pilot", "guven"]
    list_filter = ["guven", "pilot"]
    search_fields = ["ortak_girisim__kanonik_ad", "uye__kanonik_ad"]
    raw_id_fields = ["ortak_girisim", "uye"]


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
