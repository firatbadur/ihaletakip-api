"""Core view'ları — health, destek, DETSIS arama."""
from django.db.models import Q
from rest_framework import generics, permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Detsis, SupportTicket
from .serializers import DetsisSerializer, SupportTicketSerializer


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


class DetsisSearchView(APIView):
    """DETSIS idare arama (ad içinde geçen)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Response([], status=status.HTTP_200_OK)

        limit = min(int(request.query_params.get("limit", 20)), 50)
        words = [w for w in q.split() if len(w) >= 2]

        qs = Detsis.objects.all()
        for w in words:
            qs = qs.filter(Q(ad__icontains=w))

        results = DetsisSerializer(qs[:limit], many=True).data
        return Response(results)
