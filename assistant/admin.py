from django.contrib import admin

from .models import ChatMessage, CompanyProfile, TenderRecommendation


@admin.register(CompanyProfile)
class CompanyProfileAdmin(admin.ModelAdmin):
    list_display = ("company_name", "user", "sector", "is_active", "profile_map_generated_at")
    list_filter = ("is_active", "sector")
    search_fields = ("company_name", "user__email")
    readonly_fields = ("profile_map", "profile_map_generated_at", "created_at", "updated_at")


@admin.register(TenderRecommendation)
class TenderRecommendationAdmin(admin.ModelAdmin):
    list_display = ("user", "tender", "score", "date", "seen")
    list_filter = ("date", "seen")
    search_fields = ("user__email", "tender__ikn", "tender__ihale_adi")
    raw_id_fields = ("tender",)


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "short_content", "created_at")
    list_filter = ("role",)
    search_fields = ("user__email", "content")

    @admin.display(description="İçerik")
    def short_content(self, obj):
        return obj.content[:80]
