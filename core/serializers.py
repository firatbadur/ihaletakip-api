"""Core serializer'ları."""
from rest_framework import serializers

from .models import Detsis, SupportTicket


class SupportTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = ["id", "email", "phone", "message", "status", "reply", "created_at"]
        read_only_fields = ["id", "status", "reply", "created_at"]


class DetsisSerializer(serializers.ModelSerializer):
    class Meta:
        model = Detsis
        fields = ["detsis_id", "ad"]
