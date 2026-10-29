"""Core view'ları — health, destek."""
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, serializers, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from accounts.premium import MSG_SUPPORT, require_premium

from . import support
from .models import AppConfig, SupportTicket
from .serializers import (
    SupportReplySerializer,
    SupportTicketDetailSerializer,
    SupportTicketSerializer,
)


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


@extend_schema(
    tags=["app"],
    summary="Uygulama durumu (bakım + zorunlu güncelleme)",
    description=(
        "Mobil uygulamanın açılışta okuduğu genel durum bilgisi. Kimlik "
        "doğrulaması **istemez** (giriş öncesi de çağrılır).\n\n"
        "- `maintenance.active` **true** ise uygulama yalnızca bakım ekranını "
        "gösterir.\n"
        "- İstemci, cihaz sürümünü `version.min_ios` / `version.min_android` "
        "ile karşılaştırır; altındaysa zorunlu güncelleme ekranı gösterir ve "
        "ilgili mağaza bağlantısına yönlendirir."
    ),
    auth=[],
    responses={
        200: inline_serializer(
            name="AppStatus",
            fields={
                "maintenance": inline_serializer(
                    name="AppStatusMaintenance",
                    fields={
                        "active": serializers.BooleanField(),
                        "title": serializers.CharField(),
                        "message": serializers.CharField(),
                    },
                ),
                "version": inline_serializer(
                    name="AppStatusVersion",
                    fields={
                        "min_ios": serializers.CharField(),
                        "min_android": serializers.CharField(),
                        "ios_store_url": serializers.CharField(),
                        "android_store_url": serializers.CharField(),
                        "update_title": serializers.CharField(),
                        "update_message": serializers.CharField(),
                    },
                ),
            },
        )
    },
)
@api_view(["GET"])
@permission_classes([permissions.AllowAny])
@authentication_classes([])
def app_status(request):
    """Bakım modu + zorunlu güncelleme bilgisini döner (public)."""
    cfg = AppConfig.load()
    return Response(
        {
            "maintenance": {
                "active": cfg.maintenance_active,
                "title": cfg.maintenance_title,
                "message": cfg.maintenance_message,
            },
            "version": {
                "min_ios": cfg.min_ios_version,
                "min_android": cfg.min_android_version,
                "ios_store_url": cfg.ios_store_url,
                "android_store_url": cfg.android_store_url,
                "update_title": cfg.update_title,
                "update_message": cfg.update_message,
            },
        }
    )


@extend_schema_view(
    get=extend_schema(
        tags=["support"],
        summary="Destek taleplerini listele",
        description=(
            "Kullanıcının destek talepleri, son mesaja göre yeniden eskiye. Her satırda "
            "`unread` (görülmemiş destek yanıtı var mı) ve `last_message` önizlemesi bulunur."
        ),
    ),
    post=extend_schema(
        tags=["support"],
        summary="Destek talebi aç",
        description=(
            "Yeni bir destek yazışması başlatır; `message` ilk mesajdır.\n\n"
            "**Pro özellik:** Destek talebi OLUŞTURMA yalnızca Pro üyelere açıktır. "
            "Free üye **403** alır (`errors.code = premium_required`). Listeleme, "
            "okuma ve mevcut talebe YANIT her üyeye açıktır (süresi biten Pro "
            "yazışmasını sürdürebilsin)."
        ),
        examples=[
            OpenApiExample(
                "Destek talebi aç",
                request_only=True,
                description=(
                    "`email` gönderilmezse hesap e-postası kullanılır. `category` "
                    "isteğe bağlıdır (vars. `diger`); `meta` yalnızca izinli anahtarları "
                    "saklar (`app_version`, `build`, `platform`, `os_version`, `device_model`)."
                ),
                value={
                    "category": "bildirim",
                    "phone": "05551234567",
                    "message": "Kayıtlı aramamın bildirimi gelmiyor, yardımcı olabilir misiniz?",
                    "meta": {"app_version": "2.4.0", "platform": "ios", "os_version": "26.0"},
                },
            )
        ],
        responses={201: SupportTicketDetailSerializer},
    ),
)
class SupportTicketView(generics.ListCreateAPIView):
    """Kullanıcının destek taleplerini listele / yeni talep oluştur."""

    serializer_class = SupportTicketSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return SupportTicket.objects.filter(user=self.request.user).prefetch_related("messages")

    def create(self, request, *args, **kwargs):
        # Destek talebi oluşturma Pro'ya özeldir (listeleme / yanıt herkese açık).
        require_premium(request.user, MSG_SUPPORT)
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        ticket = support.talep_ac(
            request.user,
            message=v["message"],
            category=v.get("category") or "diger",
            email=v.get("email") or "",
            phone=v.get("phone") or "",
            meta=v.get("meta") or {},
        )
        return Response(
            SupportTicketDetailSerializer(ticket).data, status=status.HTTP_201_CREATED
        )


def _kendi_talebi(request, pk):
    return get_object_or_404(
        SupportTicket.objects.prefetch_related("messages"), pk=pk, user=request.user
    )


@extend_schema(
    tags=["support"],
    summary="Destek yazışmasını getir",
    description=(
        "Talep + tüm mesajlar (eskiden yeniye). `messages[].sender`: `user` | `staff`.\n\n"
        "⚠️ Bu çağrı talebi **okundu** sayar (`unread=false`)."
    ),
    responses={200: SupportTicketDetailSerializer},
)
@api_view(["GET"])
def support_ticket_detail(request, pk):
    ticket = _kendi_talebi(request, pk)
    if ticket.user_unread:
        SupportTicket.objects.filter(pk=ticket.pk).update(user_unread=False)
        ticket.user_unread = False
    return Response(SupportTicketDetailSerializer(ticket).data)


@extend_schema(
    tags=["support"],
    summary="Destek talebine yanıt yaz",
    description=(
        "Kullanıcı mesajı ekler; talep **Bekliyor**'a döner (kapalıysa yeniden açılır). "
        "Pro şartı YOKTUR. Yanıt olarak güncel yazışma döner."
    ),
    request=SupportReplySerializer,
    responses={201: SupportTicketDetailSerializer},
)
@api_view(["POST"])
def support_ticket_reply(request, pk):
    ticket = _kendi_talebi(request, pk)
    ser = SupportReplySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    support.kullanici_yaniti(ticket, ser.validated_data["body"])
    ticket = _kendi_talebi(request, pk)
    return Response(SupportTicketDetailSerializer(ticket).data, status=status.HTTP_201_CREATED)


@extend_schema(
    tags=["support"],
    summary="Destek talebini kapat",
    description="Talebi **Kapandı** yapar. Kullanıcı sonradan yazarsa yeniden açılır.",
    request=None,
    responses={200: SupportTicketDetailSerializer},
)
@api_view(["POST"])
def support_ticket_close(request, pk):
    ticket = _kendi_talebi(request, pk)
    support.kapat(ticket)
    return Response(SupportTicketDetailSerializer(_kendi_talebi(request, pk)).data)


@extend_schema(
    tags=["support"],
    summary="Okunmamış destek yanıtı sayısı",
    description="Görülmemiş destek yanıtı olan talep sayısı (menü rozeti için).",
    responses={200: inline_serializer(name="SupportUnread", fields={"unread": serializers.IntegerField()})},
)
@api_view(["GET"])
def support_unread_count(request):
    n = SupportTicket.objects.filter(user=request.user, user_unread=True).count()
    return Response({"unread": n})
