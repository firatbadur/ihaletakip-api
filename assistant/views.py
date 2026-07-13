"""İhale Asistanı view'ları — profil, sohbet, öneriler."""
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    inline_serializer,
)
from rest_framework import permissions, serializers
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from core.response import api_response

from .models import ChatMessage, CompanyProfile, TenderRecommendation
from .serializers import (
    ChatMessageSerializer,
    ChatSendSerializer,
    CompanyProfileSerializer,
    TenderRecommendationSerializer,
)

_TASK_RESPONSE = inline_serializer(
    name="AssistantTaskAccepted",
    fields={"task_id": serializers.CharField()},
)

_PROFILE_EXAMPLE = OpenApiExample(
    "Firma profili kaydet",
    request_only=True,
    value={
        "company_name": "Örnek İnşaat Ltd. Şti.",
        "sector": "İnşaat / Yapım",
        "activity_areas": "Yol, altyapı ve bina inşaatı; asfalt serimi",
        "cities": [251, 284],
        "tender_types": [2],
        "budget_min": 1000000,
        "budget_max": 50000000,
        "past_works": ["2023 Ankara Çankaya yol yapımı", "2022 okul binası onarımı"],
    },
)


def _paginate(request, qs, serializer_class, default_page_size=30):
    """ekap.TenderListView ile aynı manuel sayfalama zarfı: {list, totalCount, page}."""
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(100, max(1, int(request.query_params.get("page_size", default_page_size))))
    except (TypeError, ValueError):
        page_size = default_page_size

    total = qs.count()
    start = (page - 1) * page_size
    data = serializer_class(qs[start : start + page_size], many=True).data
    return api_response(data={"list": data, "totalCount": total, "page": page})


# ── Firma Profili ──────────────────────────────────────
@extend_schema(tags=["assistant"])
class ProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Firma profilini getir",
        description=(
            "Kullanıcının firma profilini döner. Profil yoksa **404** döner — "
            "mobil uygulama bunu 'onboarding gerekli' olarak yorumlar."
        ),
        responses={200: CompanyProfileSerializer},
    )
    def get(self, request):
        profile = CompanyProfile.objects.filter(user=request.user).first()
        if not profile:
            raise NotFound("Firma profili bulunamadı.")
        return Response(CompanyProfileSerializer(profile).data)

    @extend_schema(
        summary="Firma profilini kaydet (upsert)",
        description=(
            "Profili oluşturur veya günceller ve **profil haritası üretimini** arka "
            "planda başlatır. Yanıttaki `task_id` mevcut `GET /ai/tasks/{task_id}/` "
            "ucu ile sorgulanır; tamamlanınca `analysis` alanı profil haritasıdır."
        ),
        examples=[_PROFILE_EXAMPLE],
        request=CompanyProfileSerializer,
        responses={
            202: inline_serializer(
                name="ProfileAccepted",
                fields={
                    "task_id": serializers.CharField(),
                    "profile": CompanyProfileSerializer(),
                },
            )
        },
    )
    def put(self, request):
        serializer = CompanyProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        profile, _created = CompanyProfile.objects.update_or_create(
            user=request.user, defaults=serializer.validated_data
        )

        from .tasks import generate_profile_map_task

        task = generate_profile_map_task.delay(request.user.id)
        return api_response(
            data={"task_id": task.id, "profile": CompanyProfileSerializer(profile).data},
            message="Profil kaydedildi, profil haritası üretiliyor.",
            status=202,
        )


# ── Sohbet ─────────────────────────────────────────────
@extend_schema(
    tags=["assistant"],
    summary="Sohbet geçmişini listele",
    description=(
        "Asistan sohbet mesajlarını **en yeniden eskiye** döner (mobil inverted liste "
        "için). Yanıt: `data.list`, `data.totalCount`, `data.page`."
    ),
    parameters=[
        OpenApiParameter("page", int, description="Sayfa numarası (1'den başlar)."),
        OpenApiParameter("page_size", int, description="Sayfa boyutu (varsayılan 30, en çok 100)."),
    ],
    responses={
        200: inline_serializer(
            name="ChatMessagePage",
            fields={
                "list": ChatMessageSerializer(many=True),
                "totalCount": serializers.IntegerField(),
                "page": serializers.IntegerField(),
            },
        )
    },
)
class ChatMessageListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = ChatMessage.objects.filter(user=request.user)
        return _paginate(request, qs, ChatMessageSerializer)


@extend_schema(
    tags=["assistant"],
    summary="Asistana mesaj gönder",
    description=(
        "Mesajı kaydeder ve yanıtı arka planda üretir. Dönen `task_id`, mevcut "
        "`GET /ai/tasks/{task_id}/` ucu ile sorgulanır; tamamlanınca `analysis` "
        "alanı asistan mesajıdır: `{id, role, content, tender_cards, created_at}`."
    ),
    request=ChatSendSerializer,
    responses={202: _TASK_RESPONSE},
    examples=[
        OpenApiExample("Mesaj gönder", request_only=True, value={"message": "Bana uygun ihale var mı?"})
    ],
)
class ChatSendView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChatSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not CompanyProfile.objects.filter(user=request.user).exists():
            return api_response(
                data=None,
                message="Önce firma profilinizi oluşturun.",
                success=False,
                status=400,
            )

        msg = ChatMessage.objects.create(
            user=request.user,
            role=ChatMessage.Role.USER,
            content=serializer.validated_data["message"],
        )

        from .tasks import assistant_chat_task

        task = assistant_chat_task.delay(request.user.id, msg.id)
        return api_response(
            data={"task_id": task.id},
            message="Asistan yanıtlıyor.",
            status=202,
        )


# ── Öneriler ───────────────────────────────────────────
@extend_schema(
    tags=["assistant"],
    summary="İhale önerilerini listele",
    description=(
        "Günlük eşleştirme görevinin ürettiği kişisel ihale önerilerini döner. "
        "`days` parametresi kaç günlük önerinin listeleneceğini belirler."
    ),
    parameters=[
        OpenApiParameter("days", int, description="Kaç günlük öneri (varsayılan 7)."),
        OpenApiParameter("page", int, description="Sayfa numarası."),
        OpenApiParameter("page_size", int, description="Sayfa boyutu."),
    ],
    responses={
        200: inline_serializer(
            name="RecommendationPage",
            fields={
                "list": TenderRecommendationSerializer(many=True),
                "totalCount": serializers.IntegerField(),
                "page": serializers.IntegerField(),
            },
        )
    },
)
class RecommendationListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone

        try:
            days = min(90, max(1, int(request.query_params.get("days", 7))))
        except (TypeError, ValueError):
            days = 7

        cutoff = timezone.localdate() - timedelta(days=days)
        qs = (
            TenderRecommendation.objects.filter(user=request.user, date__gte=cutoff)
            .select_related("tender")
            .order_by("-date", "-score")
        )
        return _paginate(request, qs, TenderRecommendationSerializer)


@extend_schema(
    tags=["assistant"],
    summary="Öneriyi görüldü işaretle",
    description="Öneri kaydını görüldü yapar. Kayıt yoksa `data.updated` 0 döner.",
    request=None,
    responses={
        200: inline_serializer(
            name="RecommendationSeen", fields={"updated": serializers.IntegerField()}
        )
    },
)
class RecommendationSeenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        updated = TenderRecommendation.objects.filter(user=request.user, id=pk).update(seen=True)
        return Response({"updated": updated})
