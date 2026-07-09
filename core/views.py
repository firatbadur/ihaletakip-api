"""Core view'ları — health, destek."""
from rest_framework import generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from .models import SupportTicket
from .serializers import SupportTicketSerializer


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def health_check(request):
    """Basit sağlık kontrolü (load balancer / uptime için)."""
    return Response({"status": "ok", "service": "ihaletakip-api"})


class SupportTicketView(generics.ListCreateAPIView):
    """Kullanıcının destek taleplerini listele / yeni talep oluştur."""

    serializer_class = SupportTicketSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return SupportTicket.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(
            user=self.request.user,
            email=self.request.data.get("email") or self.request.user.email,
        )
