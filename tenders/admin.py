"""tenders admin kayıtları."""
from django.contrib import admin

from .models import Favorite, Notification, SavedFilter, SavedTender, TenderAlarm


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ["tender_title", "tender_id", "user", "source", "added_at"]
    list_filter = ["source", "added_at"]
    search_fields = ["tender_title", "tender_id", "user__username", "user__email"]
    raw_id_fields = ["user"]


@admin.register(SavedFilter)
class SavedFilterAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "created_at"]
    search_fields = ["name", "user__username", "user__email"]
    raw_id_fields = ["user"]


@admin.register(SavedTender)
class SavedTenderAdmin(admin.ModelAdmin):
    list_display = ["tender_title", "tender_ikn", "user", "tender_status", "saved_at"]
    list_filter = ["tender_status", "saved_at"]
    search_fields = ["tender_title", "tender_ikn", "user__username"]
    raw_id_fields = ["user"]


@admin.register(TenderAlarm)
class TenderAlarmAdmin(admin.ModelAdmin):
    list_display = ["tender_title", "tender_id", "user", "reminder_day", "document_change", "completed"]
    list_filter = ["reminder_day", "document_change", "completed"]
    search_fields = ["tender_title", "tender_id", "user__username"]
    raw_id_fields = ["user"]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["title", "type", "user", "read", "created_at"]
    list_filter = ["type", "read", "created_at"]
    search_fields = ["title", "body", "user__username"]
    raw_id_fields = ["user"]
