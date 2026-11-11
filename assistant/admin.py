from django.contrib import admin

from .models import (
    AssistantAction,
    AssistantAttachment,
    ChatConversation,
    ChatMessage,
    CompanyProfile,
    TenderRecommendation,
)


@admin.register(CompanyProfile)
class CompanyProfileAdmin(admin.ModelAdmin):
    list_display = ("company_name", "user", "sector", "is_active", "profile_map_generated_at")
    list_filter = ("is_active", "sector")
    search_fields = ("company_name", "user__email")
    # ⚠️ raw_id_fields ŞART. `contractor` 100k+ satırlık `ekap.Contractor`'a bakıyor;
    # admin'in varsayılan <select> widget'ı TÜM yüklenicileri tek sayfaya basmaya
    # çalıştığı için değişiklik sayfası açılmıyordu. raw_id_fields yalnızca id kutusu
    # basar, adı yanında gösterir ve arama açılır pencereden yapılır.
    # `user` de aynı sınıf sorundur (kullanıcı sayısı büyüdükçe aynı yavaşlık) —
    # profiller uygulamadan oluşuyor, admin'den elle kullanıcı seçme akışı yok.
    raw_id_fields = ("contractor", "user")
    readonly_fields = (
        "contractor_bilgi",
        "profile_map",
        "profile_map_generated_at",
        "created_at",
        "updated_at",
    )
    # Liste sayfasında `user` için satır başına ek sorgu atılmasın
    list_select_related = ("user", "contractor")

    @admin.display(description="Bağlı EKAP firması")
    def contractor_bilgi(self, obj):
        """Firma adını okunur biçimde gösterir — raw_id kutusunda yalnızca id görünür."""
        c = obj.contractor if obj.pk else None
        if not c:
            return "— (EKAP kaydına bağlı değil; bilgiler elle girilmiş)"
        parcalar = [c.kanonik_ad, f"{c.sozlesme_sayisi} sözleşme"]
        if c.il_adi:
            parcalar.append(c.il_adi)
        return " · ".join(parcalar)


@admin.register(TenderRecommendation)
class TenderRecommendationAdmin(admin.ModelAdmin):
    list_display = ("user", "tender", "score", "date", "seen")
    list_filter = ("date", "seen")
    search_fields = ("user__email", "tender__ikn", "tender__ihale_adi")
    raw_id_fields = ("tender",)


@admin.register(ChatConversation)
class ChatConversationAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "kind", "created_at", "updated_at")
    list_filter = ("kind",)
    search_fields = ("user__email", "title")


class SohbetSorunuFilter(admin.SimpleListFilter):
    """
    Okunması gereken mesajlar (sohbet analizi 2026-11-04 bu ayrımı elle yapmak zorundaydı).

    - cevapsız: kullanıcı mesajı, görev hata işareti koymuş (`payload.hata`)
    - tavan: asistan cevabı araç hakkı dolunca yazılmış (`usage.tur_sayisi` > tavan)
    """

    title = "sorun"
    parameter_name = "sorun"

    def lookups(self, request, model_admin):
        return [("cevapsiz", "Cevapsız (hata)"), ("tavan", "Araç hakkı doldu"),
                ("olumsuz", "👎 olumsuz geri bildirim")]

    def queryset(self, request, queryset):
        from django.conf import settings

        if self.value() == "cevapsiz":
            return queryset.filter(role="user", payload__has_key="hata")
        if self.value() == "tavan":
            return queryset.filter(usage__tur_sayisi__gt=settings.ASSISTANT_MAX_TURNS)
        if self.value() == "olumsuz":
            return queryset.filter(geri_bildirim=-1)
        return queryset


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("user", "conversation", "role", "short_content", "token_ozet",
                    "geri_bildirim", "created_at")
    list_filter = ("role", "geri_bildirim", SohbetSorunuFilter)
    search_fields = ("user__email", "content")
    # `user` de raw_id: bu tablo en hızlı büyüyen tablolardan; varsayılan <select>
    # tüm kullanıcıları sayfaya basar (bkz. CompanyProfileAdmin aynı gerekçe).
    raw_id_fields = ("conversation", "user")
    list_select_related = ("user", "conversation")

    @admin.display(description="İçerik")
    def short_content(self, obj):
        return obj.content[:80]

    @admin.display(description="Token (giriş/çıkış/cache)")
    def token_ozet(self, obj):
        """Mesaj başına LLM maliyetinin okunur özeti — kademe/tavan kararının verisi."""
        u = obj.usage or {}
        if not u:
            return "—"
        return (
            f"{u.get('input_tokens', 0)} / {u.get('output_tokens', 0)}"
            f" / {u.get('cache_read_input_tokens', 0)}"
        )


@admin.register(AssistantAction)
class AssistantActionAdmin(admin.ModelAdmin):
    list_display = ("user", "tur", "durum", "ozet", "created_at", "executed_at")
    list_filter = ("tur", "durum")
    search_fields = ("user__email", "ozet")
    readonly_fields = ("params", "sonuc", "created_at", "executed_at")
    raw_id_fields = ("user", "message")
    list_select_related = ("user",)


@admin.register(AssistantAttachment)
class AssistantAttachmentAdmin(admin.ModelAdmin):
    """
    Salt okunur: satırlar yükleme ucu + hazırlama görevi tarafından yazılır.

    ⚠️ `metin` ve `anthropic_file_id` listede GÖSTERİLMEZ (kullanıcı dokümanının içeriği
    ve sunucu tarafı dosya anahtarı); ayrıntı sayfasında okunur. Silme serbest — satır
    silinince `post_delete` sinyali yerel dosyayı ve Files API dosyasını da siler.
    """

    list_display = ("ad", "user", "tur", "mod", "durum", "hata_kodu", "token_sayisi",
                    "boyut", "sira", "created_at")
    list_filter = ("tur", "mod", "durum", "kaynak", "hata_kodu")
    search_fields = ("user__email", "ad", "ikn")
    list_select_related = ("user",)
    raw_id_fields = ("user", "conversation", "message")
    readonly_fields = [f.name for f in AssistantAttachment._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
