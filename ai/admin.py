"""ai admin kayıtları."""
from django.contrib import admin

from .models import AnalysisCache


@admin.register(AnalysisCache)
class AnalysisCacheAdmin(admin.ModelAdmin):
    list_display = ["ikn", "analysis_type", "created_at", "updated_at"]
    list_filter = ["analysis_type", "created_at"]
    search_fields = ["ikn"]
    readonly_fields = ["created_at", "updated_at"]
