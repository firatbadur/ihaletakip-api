from django.contrib import admin

from .models import ChatConversation, ChatMessage, CompanyProfile, TenderRecommendation


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


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("user", "conversation", "role", "short_content", "created_at")
    list_filter = ("role",)
    search_fields = ("user__email", "content")
    raw_id_fields = ("conversation",)

    @admin.display(description="İçerik")
    def short_content(self, obj):
        return obj.content[:80]
