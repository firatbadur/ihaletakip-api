"""Core admin kayıtları."""
from django.contrib import admin

from .models import AppSetting, SupportTicket


@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    list_display = ["key", "description", "updated_at"]
    search_fields = ["key", "description"]


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ["email", "status", "user", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["email", "phone", "message"]
    list_editable = ["status"]
    readonly_fields = ["created_at", "updated_at"]
