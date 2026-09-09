"""
Doğrulama çerezi alma ucu — tarayıcı eklentisinden gelen çerezi kaydeder.

⚠️ **Neden var**: EKAP 2026-09-08'de Cloudflare Turnstile insan doğrulaması
koydu (ömür ~8 dk) ve sunucudaki otomasyon tarayıcısını **açıkça reddediyor**
(ölçüldü: "doğrulama başarısız"). Buna karşılık gerçek bir insanın gerçek
tarayıcısı doğrulamayı geçiyor ve EKAP arayüzü o oturumu **kendi kendine**
tazeliyor (`scheduleRefresh`).

Bu yüzden doğrulamayı geçen taraf **insanın kendi tarayıcısıdır**; buradaki uç
yalnızca o oturumun çerezini alıp toplayıcının kullanımına verir. CAPTCHA
çözülmez, otomatik tıklanmaz, tarayıcı gizlenmez.

⚠️ **Kimlik = paylaşılan sır** (`EKAP_COOKIE_PUSH_TOKEN`), JWT değil: eklenti
bir kullanıcı adına değil, operasyon adına konuşuyor. Sır tanımlı değilse uç
**kapalıdır** (403) — varsayılan olarak açık bırakmak, çerez yazma yetkisini
internete açmak olurdu.
"""
import hmac
import logging

from django.conf import settings
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.response import api_response

from . import session as ekap_session

logger = logging.getLogger("ihaletakip")


class DogrulamaCereziSerializer(serializers.Serializer):
    cookie = serializers.CharField(help_text="ekapv2.kik.gov.tr için `Cookie` başlığı")


@extend_schema(
    summary="EKAP doğrulama çerezini kaydet",
    description=(
        "Tarayıcı eklentisi, kullanıcının doğrulanmış EKAP oturumunun çerezini "
        "buraya gönderir. Kimlik doğrulama `X-Ekap-Token` başlığındaki paylaşılan "
        "sırla yapılır (JWT değil)."
    ),
    request=DogrulamaCereziSerializer,
    responses={200: None, 403: None},
    auth=[],
    examples=[OpenApiExample("Örnek", value={"cookie": "ekap.human-verification=...; TS0...=..."})],
)
class DogrulamaCereziView(APIView):
    permission_classes = [AllowAny]
    # ⚠️ **Oturum kimliği KAPALI.** Bu uç paylaşılan sırla konuşur; varsayılan
    # `SessionAuthentication` açık kalırsa istekle birlikte gelen admin oturum
    # çerezi CSRF kontrolünü tetikler ve tarayıcı eklentisinden gelen istek
    # `CSRF Failed: Origin checking failed - chrome-extension://…` ile 403 alır.
    # Üretimde yaşandı (2026-09-09): sır doğruyken saatlerce 403 sanıldı.
    authentication_classes = []

    def post(self, request):
        sir = getattr(settings, "EKAP_COOKIE_PUSH_TOKEN", "")
        if not sir:
            return api_response(None, "Bu uç yapılandırılmamış.", success=False,
                                status=status.HTTP_403_FORBIDDEN)
        gelen = request.headers.get("X-Ekap-Token", "")
        # ⚠️ Sabit zamanlı karşılaştırma: düz `!=` sırrı karakter karakter
        # tahmin etmeye açık bırakır.
        if not hmac.compare_digest(gelen, sir):
            return api_response(None, "Yetkisiz.", success=False,
                                status=status.HTTP_403_FORBIDDEN)

        ser = DogrulamaCereziSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            temiz = ekap_session.kaydet(ser.validated_data["cookie"])
        except ValueError as e:
            return api_response(None, str(e), success=False,
                                status=status.HTTP_400_BAD_REQUEST)

        dogrulandi = "ekap.human-verification" in temiz
        if not dogrulandi:
            logger.warning("Gelen çerezde ekap.human-verification yok (%s)",
                           ekap_session.maskele(temiz))
        return api_response(
            {"kaydedildi": True, "dogrulama_cerezi_var": dogrulandi},
            "Çerez kaydedildi." if dogrulandi else
            "Çerez kaydedildi ama doğrulama çerezi içermiyor.",
        )
