"""Core view'ları — health, destek."""
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import generics, permissions, serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.premium import MSG_SUPPORT, require_premium

from .models import SupportTicket
from .serializers import SupportTicketSerializer


@extend_schema(
    tags=["health"],
    summary="Sağlık kontrolü",
    description=(
        "Load balancer / uptime izleme için basit sağlık kontrolü. Kimlik doğrulaması "
        "istemez ve `/api/v1` öneki **olmadan** `/health/` altındadır."
    ),
    auth=[],
    responses={
        200: inline_serializer(
            name="Health",
            fields={
                "status": serializers.CharField(),
                "service": serializers.CharField(),
            },
        )
    },
)
@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def health_check(request):
    """Basit sağlık kontrolü (load balancer / uptime için)."""
    return Response({"status": "ok", "service": "ihaletakip-api"})


@extend_schema_view(
    get=extend_schema(
        tags=["support"],
        summary="Destek taleplerini listele",
        description="Kullanıcının açtığı destek taleplerini ve varsa yanıtlarını döner.",
    ),
    post=extend_schema(
        tags=["support"],
        summary="Destek talebi aç",
        description=(
            "Yeni bir destek talebi oluşturur.\n\n"
            "**Pro özellik:** Destek talebi oluşturma yalnızca Pro üyelere açıktır. "
            "Free üye talep gönderirse **403** alır (`errors.code = premium_required`). "
            "Mevcut talepleri listeleme (GET) her üyeye açıktır."
        ),
        examples=[
            OpenApiExample(
                "Destek talebi aç",
                request_only=True,
                description=(
                    "`email` gönderilmezse kullanıcının hesap e-postası kullanılır. "
                    "`status` ve `reply` alanları yalnızca yönetici tarafından doldurulur."
                ),
                value={
                    "email": "test@ihaletakip.com",
                    "phone": "05551234567",
                    "message": "Analiz sonucu gelmiyor, yardımcı olabilir misiniz?",
                },
            )
        ],
    ),
)
class SupportTicketView(generics.ListCreateAPIView):
    """Kullanıcının destek taleplerini listele / yeni talep oluştur."""

    serializer_class = SupportTicketSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return SupportTicket.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        # Destek talebi oluşturma Pro'ya özeldir (listeleme herkese açık).
        require_premium(self.request.user, MSG_SUPPORT)
        serializer.save(
            user=self.request.user,
            email=self.request.data.get("email") or self.request.user.email,
        )
