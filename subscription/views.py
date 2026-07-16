"""
Abonelik view'ları — RevenueCat doğrulama + webhook.

- `POST /api/v1/subscription/verify/` (mobil→backend, JWT, gövde boş): kullanıcının
  RC aboneliğini **senkron** çeker, katmanı günceller, güncel `user` objesini döner.
  Mobil satın alma sonrası bunu çağırıp anında Pro'ya geçtiğini görür.
- `POST /api/v1/subscription/revenuecat-webhook/` (RC→backend): Authorization başlığını
  `REVENUECAT_WEBHOOK_AUTH` ile doğrular, event'ten kullanıcıyı çözer, senkronu Celery'ye
  atar ve **hızlıca 200** döner.
"""
import logging

from django.conf import settings
from drf_spectacular.utils import OpenApiExample, extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.views import APIView

from accounts.serializers import UserSerializer
from core.response import api_response

logger = logging.getLogger("ihaletakip")


@extend_schema(
    tags=["subscription"],
    summary="Aboneliği doğrula (RevenueCat)",
    description=(
        "Kullanıcının RevenueCat aboneliğini **senkron** doğrular ve katmanını günceller. "
        "`app_user_id = str(user.id)` ile RC v2 `active_entitlements` sorgulanır; aktif "
        "`pro` entitlement varsa `subscription_tier=pro` yapılır (expiry RC'den alınır), "
        "yoksa `free`'ye çekilir.\n\n"
        "**Gövde boştur** (`{}`). Yanıt, login/profile ile aynı şekildeki güncel kullanıcı "
        "objesidir: `data.user`. Mobil satın alma tamamlanınca bunu çağırır.\n\n"
        "Sunucuda `REVENUECAT_SECRET_KEY` tanımlı değilse veya RC'ye ulaşılamazsa **502** döner."
    ),
    request=None,
    responses={
        200: inline_serializer(
            name="SubscriptionVerified", fields={"user": UserSerializer()}
        ),
        502: inline_serializer(
            name="SubscriptionVerifyError", fields={"detail": serializers.CharField()}
        ),
    },
    examples=[OpenApiExample("Boş gövde", request_only=True, value={})],
)
class SubscriptionVerifyView(APIView):
    """POST /subscription/verify — RC aboneliğini senkron doğrula (JWT)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from .services.revenuecat import RevenueCatError, sync_user_subscription

        try:
            sync_user_subscription(request.user)
        except RevenueCatError as e:
            logger.warning("subscription verify: RC hatası uid=%s: %s", request.user.pk, e)
            return api_response(
                message="Abonelik şu an doğrulanamadı, lütfen tekrar deneyin.",
                success=False,
                status=502,
            )
        return api_response(
            data={"user": UserSerializer(request.user).data},
            message="Abonelik durumu güncellendi.",
        )


@extend_schema(
    tags=["subscription"],
    summary="RevenueCat webhook",
    auth=[],
    description=(
        "RevenueCat sunucu-sunucu webhook'u. `Authorization` başlığı sunucudaki "
        "`REVENUECAT_WEBHOOK_AUTH` ile birebir eşleşmeli (aksi halde **401**). Event'ten "
        "kullanıcı çözülür (`app_user_id`/`aliases` → sayısal `user.id`); senkron Celery'ye "
        "atılır ve **200** döner. Kullanıcı eşleşmezse (ör. anonim id) yine 200 döner "
        "(`handled=false`) — RC gereksiz yere tekrar denemesin.\n\n"
        "Bu uç kimlik doğrulaması (JWT) İSTEMEZ; RC'nin gönderdiği Authorization "
        "başlığıyla korunur."
    ),
    request=inline_serializer(
        name="RevenueCatWebhookEvent",
        fields={"event": serializers.JSONField()},
    ),
    responses={
        200: inline_serializer(
            name="WebhookHandled", fields={"handled": serializers.BooleanField()}
        ),
        401: inline_serializer(
            name="WebhookUnauthorized", fields={"detail": serializers.CharField()}
        ),
    },
    examples=[
        OpenApiExample(
            "RC event",
            request_only=True,
            value={
                "event": {
                    "type": "INITIAL_PURCHASE",
                    "app_user_id": "42",
                    "aliases": ["42", "$RCAnonymousID:abc123"],
                    "entitlement_ids": ["pro"],
                    "product_id": "pro_monthly",
                    "expiration_at_ms": 1794835200000,
                    "store": "APP_STORE",
                    "environment": "SANDBOX",
                }
            },
        )
    ],
)
class RevenueCatWebhookView(APIView):
    """POST /subscription/revenuecat-webhook — RC event'ini işle (auth: shared secret)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []  # RC JWT göndermez; Authorization = paylaşılan sır

    def post(self, request):
        from .services.revenuecat import resolve_user_from_event
        from .tasks import sync_subscription_task

        expected = getattr(settings, "REVENUECAT_WEBHOOK_AUTH", "")
        provided = request.headers.get("Authorization", "")
        if not expected or provided != expected:
            logger.warning("revenuecat webhook: yetkisiz istek (Authorization eşleşmedi)")
            return api_response(message="Yetkisiz.", success=False, status=401)

        event = (request.data or {}).get("event") or {}
        user = resolve_user_from_event(event)
        if user is None:
            logger.info(
                "revenuecat webhook: kullanıcı eşleşmedi app_user_id=%s type=%s",
                event.get("app_user_id"), event.get("type"),
            )
            return api_response(data={"handled": False}, message="Kullanıcı eşleşmedi.")

        # Senkronu arka plana at → hızlıca 200 dön. RC ulaşılamazsa görev event'e düşer.
        sync_subscription_task.delay(user.id, event=event)
        return api_response(data={"handled": True}, message="Alındı.")
