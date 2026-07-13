"""assistant serializer'ları."""
from rest_framework import serializers

from .models import ChatMessage, CompanyProfile, TenderRecommendation


class CompanyProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyProfile
        fields = [
            "id",
            "company_name",
            "sector",
            "activity_areas",
            "cities",
            "tender_types",
            "budget_min",
            "budget_max",
            "past_works",
            "profile_map",
            "profile_map_generated_at",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "profile_map", "profile_map_generated_at", "created_at", "updated_at"]


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ["id", "role", "content", "payload", "created_at"]
        read_only_fields = fields


class ChatSendSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=2000, trim_whitespace=True)


class TenderRecommendationSerializer(serializers.ModelSerializer):
    ikn = serializers.CharField(source="tender.ikn", read_only=True)
    ekap_id = serializers.CharField(source="tender.ekap_id", read_only=True)
    ihale_adi = serializers.CharField(source="tender.ihale_adi", read_only=True)
    idare_adi = serializers.CharField(source="tender.idare_adi", read_only=True)
    il = serializers.CharField(source="tender.ihale_il_adi", read_only=True)
    ihale_tarihi = serializers.DateTimeField(source="tender.ihale_tarihi", read_only=True)
    ihale_tip = serializers.IntegerField(source="tender.ihale_tip", read_only=True)

    class Meta:
        model = TenderRecommendation
        fields = [
            "id",
            "score",
            "reasons",
            "date",
            "seen",
            "ikn",
            "ekap_id",
            "ihale_adi",
            "idare_adi",
            "il",
            "ihale_tarihi",
            "ihale_tip",
        ]
        read_only_fields = fields
