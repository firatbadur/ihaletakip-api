"""Güvenlik uçları — web portalının insan doğrulaması (Turnstile)."""
from drf_spectacular.utils import OpenApiExample, extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.views import APIView

from .insan_dogrulama import MESAJ_GECERSIZ, sifirla, turnstile_dogrula
from .response import api_response
from .throttling import istemci, portal_dogrulandi


@extend_schema(
    tags=["guvenlik"],
    auth=[],
    operation_id="guvenlik_insan_dogrulama",
    summary="İnsan doğrulaması (Cloudflare Turnstile)",
    description=(
        "**Yalnızca web portalı** (`X-IT-Portal-Key` + `X-IT-Client-IP`; girişliyse "
        "`Authorization`). Bir uç **428** `errors.code=\"human_verification_required\"` "
        "döndüğünde web Turnstile kutusunu gösterir ve çıkan token'ı buraya iletir. Geçerse "
        "o kimliğin (hesap ya da IP) sayaçları sıfırlanır; asıl istek tekrarlanabilir.\n\n"
        "⚠️ Token tek kullanımlıktır (~300 sn). `X-IT-Client-IP` Cloudflare'e `remoteip` "
        "olarak gönderilir; yanlış IP doğrulamayı düşürebilir.\n\n"
        "Hatalar: 403 (portal anahtarı yok), 400 `human_verification_required` (token "
        "geçersiz/süresi dolmuş), 503 (Turnstile yapılandırılmamış)."
    ),
    request=inline_serializer("InsanDogrulamaIstek", fields={
        "token": serializers.CharField(help_text="Turnstile widget'ının ürettiği token."),
    }),
    responses={200: inline_serializer("InsanDogrulamaYanit", fields={
        "dogrulandi": serializers.BooleanField(),
    })},
    examples=[
        OpenApiExample("İstek", value={"token": "0.AbC…"}, request_only=True),
        OpenApiExample("Başarılı", value={"success": True, "message": "",
                                           "data": {"dogrulandi": True}},
                       response_only=True),
    ],
)
class InsanDogrulamaView(APIView):
    """POST /guvenlik/insan-dogrulama/ — Turnstile token'ını doğrular, sayacı sıfırlar."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        from django.conf import settings

        from .guvenlik import olay_yaz

        if not portal_dogrulandi(request):
            return api_response(message="Bu uç yalnızca web portalı içindir.",
                                success=False, status=403)
        if not getattr(settings, "TURNSTILE_SECRET_KEY", ""):
            return api_response(message="İnsan doğrulaması şu an yapılandırılmamış.",
                                success=False, status=503)
        ist = istemci(request)
        tamam, hatalar = turnstile_dogrula(str(request.data.get("token") or ""), ist.ip)
        if not tamam:
            olay_yaz("dogrulama_basarisiz", ist=ist, request=request,
                     ayrinti={"hatalar": hatalar[:5]})
            return api_response(
                message=MESAJ_GECERSIZ, success=False, status=400,
                errors={"code": "human_verification_required", "detail": MESAJ_GECERSIZ,
                        "nedenler": hatalar[:5]})
        sifirla(ist)
        olay_yaz("dogrulama_gecti", ist=ist, request=request)
        return api_response(data={"dogrulandi": True})
