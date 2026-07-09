"""tenders serializer'ları."""
from rest_framework import serializers

from .models import Favorite, Notification, SavedFilter, SavedTender, TenderAlarm


class FavoriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Favorite
        fields = [
            "id",
            "tender_id",
            "tender_title",
            "tender_type",
            "source",
            "added_at",
        ]
        read_only_fields = ["id", "added_at"]


class SavedFilterSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavedFilter
        fields = ["id", "name", "filters", "tags", "alarm", "created_at"]
        read_only_fields = ["id", "created_at"]


class SavedTenderSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavedTender
        fields = [
            "id",
            "tender_id",
            "tender_ikn",
            "tender_title",
            "tender_type",
            "tender_status",
            "tender_city",
            "tender_date",
            "institution",
            "saved_at",
        ]
        read_only_fields = ["id", "saved_at"]


class TenderAlarmSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenderAlarm
        fields = [
            "id",
            "tender_id",
            "tender_ikn",
            "tender_title",
            "institution",
            "reminder_day",
            "document_change",
            "completed",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id",
            "type",
            "title",
            "body",
            "tender_id",
            "tender_title",
            "tender_ikn",
            "institution",
            "read",
            "created_at",
        ]
        read_only_fields = fields
