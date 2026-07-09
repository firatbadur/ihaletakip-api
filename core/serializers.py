"""Core serializer'ları."""
from rest_framework import serializers

from .models import SupportTicket


class SupportTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = ["id", "email", "phone", "message", "status", "reply", "created_at"]
        read_only_fields = ["id", "status", "reply", "created_at"]
