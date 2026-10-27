"""E-posta ile kayıt + şifre sıfırlama — /api/v1/auth/email/... ve /auth/password/...

Kod gönderimi ve doğrulaması `accounts/services/eposta_kod.py`'de; buradaki görünümler
yalnızca akışı kurar. İstemci sözleşmesi: `docs/mobil-eposta-kayit.md`,
`docs/web-eposta-kayit.md`.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.db import IntegrityError, transaction
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.response import api_response

from .models import EpostaKodu
from .serializers import (
    CodeSentSerializer,
    EmailCodeSerializer,
    EmailOnlySerializer,
    EmailRegisterStartSerializer,
    PasswordResetConfirmSerializer,
    ResetTokenSerializer,
    TokenPairSerializer,
    _sifre_dogrula,
    issue_tokens,
)
from .services.eposta_kod import (
    EpostaHatasi,
    EpostaIpThrottle,
    dogrula,
    kod_ayarlari,
    kod_gonder,
    normalize_email,
)

User = get_user_model()
logger = logging.getLogger("ihaletakip")

MSG_KAYITLI = (
    "Bu e-posta adresiyle zaten bir hesap var. Giriş yapın ya da "
    "\"Şifremi unuttum\" ile şifre belirleyin."
)
MSG_KOD_GONDERILDI = "Doğrulama kodu e-posta adresinize gönderildi."
# ⚠️ Kayıtlı olsun olmasın AYNI metin: hangi adreslerin kayıtlı olduğu bu uçtan
# öğrenilemesin.
MSG_SIFRE_KOD = (
    "Bu e-posta adresiyle kayıtlı bir hesap varsa şifre sıfırlama kodu gönderildi."
)
MSG_TOKEN_GECERSIZ = (
    "Şifre sıfırlama oturumunun süresi doldu. Lütfen yeni bir kod isteyin."
)
_SIFRE_SALT = "sifre-sifirla-v1"


def _kod_yaniti(email):
    return {"email": email, **kod_ayarlari()}


def _hata_ornegi(ad, kod, mesaj, **ek):
    return OpenApiExample(ad, response_only=True, status_codes=[
        "429" if kod in ("code_cooldown", "code_limit") else
        "503" if kod == "send_failed" else "400"],
        value={"success": False, "message": mesaj, "data": None,
               "errors": {"code": kod, "detail": mesaj, **ek}})


class _EpostaUcu(APIView):
    permission_classes = [permissions.AllowAny]
    # ⚠️ Varsayılan `KapsamliThrottle` (gözlem modu) yerine her zaman uygulanan sınır.
    throttle_classes = [EpostaIpThrottle]


@extend_schema(
    tags=["auth"],
    summary="E-posta ile kayıt — kod gönder",
    auth=[],
    description=(
        "Kaydın **1. adımı**: e-posta + şifre (+ isteğe bağlı ad) doğrulanır ve e-postaya "
        "**6 haneli kod** gönderilir. Hesap bu adımda **açılmaz**; `register/verify/` ile "
        "kod girilince açılır.\n\n"
        "Kodu yeniden göndermek için bu uç **aynı gövdeyle** tekrar çağrılır "
        "(`tekrar_gonderim_sn` dolmadan → 429 `code_cooldown`). Her yeni kod öncekini "
        "geçersiz kılar.\n\n"
        "Şifre kuralları: en az **8** karakter, yalnız rakam olamaz, çok yaygın bir şifre "
        "olamaz, e-postaya/ada çok benzeyemez. İhlal → 400 `errors.password` (liste).\n\n"
        "Hata kodları (`errors.code`): `email_exists` (400), `code_cooldown` / "
        "`code_limit` (429, `retry_after` sn), `send_failed` (503)."
    ),
    request=EmailRegisterStartSerializer,
    responses={200: CodeSentSerializer},
    examples=[
        OpenApiExample("İstek", request_only=True, value={
            "email": "ahmet@firma.com.tr", "password": "Guclu.Sifre42",
            "display_name": "Ahmet Yılmaz"}),
        OpenApiExample("Kod gönderildi", response_only=True, value={
            "success": True, "message": MSG_KOD_GONDERILDI, "data": {
                "email": "ahmet@firma.com.tr", "gecerlilik_sn": 600,
                "tekrar_gonderim_sn": 60}}),
        _hata_ornegi("Kayıtlı e-posta", "email_exists", MSG_KAYITLI),
        _hata_ornegi("Bekleme", "code_cooldown", "Yeni kod istemeden önce biraz bekleyin.",
                     retry_after=42),
    ],
)
class EmailRegisterStartView(_EpostaUcu):
    eposta_kapsam = "kod_gonder"

    def post(self, request):
        s = EmailRegisterStartSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = s.validated_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise EpostaHatasi(MSG_KAYITLI, "email_exists")
        kod_gonder(email, EpostaKodu.Amac.KAYIT, veri={
            "sifre": make_password(s.validated_data["password"]),
            "display_name": (s.validated_data.get("display_name") or "").strip(),
        })
        return api_response(_kod_yaniti(email), message=MSG_KOD_GONDERILDI)


@extend_schema(
    tags=["auth"],
    summary="E-posta ile kayıt — kodu doğrula",
    auth=[],
    description=(
        "Kaydın **2. adımı**: kod doğruysa hesap açılır ve `register`/`login` ile aynı "
        "şekilde `access` + `refresh` + `user` döner (`created: true`, HTTP 201).\n\n"
        "Kod 10 dk geçerli, tek kullanımlık; **5 hatalı denemeden** sonra yeni kod "
        "gerekir.\n\n"
        "Hata kodları: `code_invalid` (400, `kalan_deneme`), `code_expired` (400), "
        "`code_attempts` (400), `email_exists` (400)."
    ),
    request=EmailCodeSerializer,
    responses={201: TokenPairSerializer},
    examples=[
        OpenApiExample("İstek", request_only=True,
                       value={"email": "ahmet@firma.com.tr", "code": "482913"}),
        _hata_ornegi("Hatalı kod", "code_invalid",
                     "Kod hatalı. Lütfen e-postanızdaki 6 haneli kodu kontrol edin.",
                     kalan_deneme=3),
    ],
)
class EmailRegisterVerifyView(_EpostaUcu):
    eposta_kapsam = "kod_dogrula"

    def post(self, request):
        s = EmailCodeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = normalize_email(s.validated_data["email"])
        kayit = dogrula(email, EpostaKodu.Amac.KAYIT, s.validated_data["code"])
        veri = kayit.veri or {}
        if not veri.get("sifre"):
            raise EpostaHatasi("Kayıt bilgisi bulunamadı. Lütfen kayıt adımını tekrarlayın.",
                               "code_expired")
        try:
            with transaction.atomic():
                if User.objects.filter(email__iexact=email).exists():
                    raise EpostaHatasi(MSG_KAYITLI, "email_exists")
                user = User(
                    username=User.objects._unique_username(email),
                    email=email,
                    display_name=veri.get("display_name", ""),
                    provider=User.Provider.EMAIL,
                    email_verified_at=timezone.now(),
                )
                user.password = veri["sifre"]  # 1. adımda `make_password` ile özetlendi
                user.save()
        except IntegrityError as e:  # eşzamanlı ikinci doğrulama / sosyal kayıt yarışı
            raise EpostaHatasi(MSG_KAYITLI, "email_exists") from e
        # Bekleyen şifre özeti artık hesapta; kod satırında tutmanın sebebi kalmadı.
        EpostaKodu.objects.filter(pk=kayit.pk).update(veri={})
        logger.info("e-posta ile kayıt tamamlandı: uid=%s", user.pk)
        return Response(issue_tokens(user, created=True), status=status.HTTP_201_CREATED)


@extend_schema(
    tags=["auth"],
    summary="Şifre sıfırlama — kod gönder",
    auth=[],
    description=(
        "Hesap bu e-postayla kayıtlıysa 6 haneli sıfırlama kodu gönderilir. ⚠️ Yanıt "
        "**kayıtlı olsun olmasın aynıdır** (hangi adreslerin kayıtlı olduğu öğrenilemesin); "
        "istemci her durumda kod ekranına geçer.\n\n"
        "Google/Apple ile açılmış hesaplar da bu yolla şifre belirleyip e-posta + şifreyle "
        "girebilir.\n\n"
        "Yeniden gönderim: aynı uç tekrar çağrılır; bekleme süresi dolmadan yapılan "
        "çağrıda kod gönderilmez ama yanıt yine 200'dür (geri sayımı istemci tutar). "
        "Yalnızca gönderim altyapısı arızasında 503 `send_failed` döner."
    ),
    request=EmailOnlySerializer,
    responses={200: CodeSentSerializer},
    examples=[
        OpenApiExample("İstek", request_only=True, value={"email": "ahmet@firma.com.tr"}),
        OpenApiExample("Yanıt", response_only=True, value={
            "success": True, "message": MSG_SIFRE_KOD, "data": {
                "email": "ahmet@firma.com.tr", "gecerlilik_sn": 600,
                "tekrar_gonderim_sn": 60}}),
    ],
)
class PasswordResetStartView(_EpostaUcu):
    eposta_kapsam = "kod_gonder"

    def post(self, request):
        s = EmailOnlySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = normalize_email(s.validated_data["email"])
        if User.objects.filter(email__iexact=email, is_active=True).exists():
            try:
                kod_gonder(email, EpostaKodu.Amac.SIFRE)
            except EpostaHatasi as e:
                # Bekleme/tavan sessiz: 429 dönmek adresin kayıtlı olduğunu söylerdi.
                if e.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
                    raise
        return api_response(_kod_yaniti(email), message=MSG_SIFRE_KOD)


@extend_schema(
    tags=["auth"],
    summary="Şifre sıfırlama — kodu doğrula",
    auth=[],
    description=(
        "Kod doğruysa kısa ömürlü (15 dk) bir `reset_token` döner; yeni şifre "
        "`password/reset/confirm/` ile bu token'la kaydedilir. Kod burada tüketilir.\n\n"
        "Hata kodları: `code_invalid` (`kalan_deneme`), `code_expired`, `code_attempts`."
    ),
    request=EmailCodeSerializer,
    responses={200: ResetTokenSerializer},
    examples=[
        OpenApiExample("İstek", request_only=True,
                       value={"email": "ahmet@firma.com.tr", "code": "482913"}),
        OpenApiExample("Yanıt", response_only=True, value={
            "success": True, "message": "", "data": {
                "reset_token": "MTIzOmM4ZC1hYmM:1tXyZ…", "gecerlilik_sn": 900}}),
    ],
)
class PasswordResetVerifyView(_EpostaUcu):
    eposta_kapsam = "kod_dogrula"

    def post(self, request):
        s = EmailCodeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        email = normalize_email(s.validated_data["email"])
        dogrula(email, EpostaKodu.Amac.SIFRE, s.validated_data["code"])
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:  # kod gönderildikten sonra hesap kapatılmış
            raise EpostaHatasi(MSG_TOKEN_GECERSIZ, "code_expired")
        # Django'nun sıfırlama token'ı şifre özetine bağlıdır → şifre değişince kendiliğinden
        # geçersizleşir (tek kullanımlık). Dış imza süreyi 15 dk'ya indirir.
        deger = f"{user.pk}:{default_token_generator.make_token(user)}"
        return api_response({
            "reset_token": signing.TimestampSigner(salt=_SIFRE_SALT).sign(deger),
            "gecerlilik_sn": settings.EPOSTA_SIFRE_TOKEN_SURE_SN,
        })


def _oturumlari_kapat(user) -> int:
    """Kullanıcının süresi dolmamış tüm refresh token'larını kara listeye alır.

    ⚠️ Access token'lar süreleri (1 gün) bitene kadar geçerli kalır — SimpleJWT
    access'i kara listede aramaz. Şifresi çalınan hesapta saldırgan en geç ertesi gün
    düşer; yenileyemez.
    """
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )

    acik = OutstandingToken.objects.filter(
        user=user, expires_at__gt=timezone.now(), blacklistedtoken__isnull=True)
    yeni = [BlacklistedToken(token=t) for t in acik]
    BlacklistedToken.objects.bulk_create(yeni, ignore_conflicts=True)
    return len(yeni)


@extend_schema(
    tags=["auth"],
    summary="Şifre sıfırlama — yeni şifreyi kaydet",
    auth=[],
    description=(
        "`reset_token` + `new_password` ile şifre değişir; kullanıcı **giriş yapmış olur** "
        "(`login` ile aynı yanıt, `created: false`).\n\n"
        "⚠️ Hesabın diğer cihazlardaki oturumları kapatılır (refresh token'ları kara "
        "listeye alınır).\n\n"
        "Şifre kuralları kayıttakiyle aynı (400 `errors.new_password`). Token süresi "
        "dolmuş / kullanılmış → 400 `reset_token_invalid`."
    ),
    request=PasswordResetConfirmSerializer,
    responses={200: TokenPairSerializer},
    examples=[OpenApiExample("İstek", request_only=True, value={
        "reset_token": "MTIzOmM4ZC1hYmM:1tXyZ…", "new_password": "Yeni.Sifre2027"})],
)
class PasswordResetConfirmView(_EpostaUcu):
    eposta_kapsam = "kod_dogrula"

    def post(self, request):
        s = PasswordResetConfirmSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            deger = signing.TimestampSigner(salt=_SIFRE_SALT).unsign(
                s.validated_data["reset_token"], max_age=settings.EPOSTA_SIFRE_TOKEN_SURE_SN)
            uid, token = deger.split(":", 1)
            user = User.objects.get(pk=int(uid), is_active=True)
        except (signing.BadSignature, ValueError, User.DoesNotExist):
            user = token = None
        if user is None or not default_token_generator.check_token(user, token):
            raise EpostaHatasi(MSG_TOKEN_GECERSIZ, "reset_token_invalid")
        try:
            _sifre_dogrula(s.validated_data["new_password"], user)
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"new_password": e.detail}) from e

        user.set_password(s.validated_data["new_password"])
        alanlar = ["password"]
        if user.email_verified_at is None:
            user.email_verified_at = timezone.now()
            alanlar.append("email_verified_at")
        user.save(update_fields=alanlar)
        kapanan = _oturumlari_kapat(user)
        logger.info("şifre sıfırlandı: uid=%s kapatılan oturum=%s", user.pk, kapanan)
        return Response(issue_tokens(user))


@csrf_exempt  # imzalı token kimliktir; Gmail'in tek tık POST'u CSRF token'ı taşımaz
def eposta_abonelik(request):
    """`/eposta/abonelik/?t=<imza>` — günlük e-posta bülteninden çıkma (DRF DEĞİL, HTML).

    - **GET**: onay sayfası (düğme POST eder). ⚠️ GET tercihi DEĞİŞTİRMEZ: kurumsal
      posta tarayıcıları ve bağlantı önizleyicileri e-postadaki her linki açar; GET'te
      çıkarmak kullanıcıyı haberi olmadan abonelikten düşürürdü.
    - **POST** (form ya da `List-Unsubscribe-Post` tek tık): `email_notifications=False`.
      `islem=geri` yeniden açar.
    """
    from tenders.services.eposta_bulten import abonelik_coz

    uid = abonelik_coz(request.GET.get("t") or request.POST.get("t") or "")
    user = User.objects.filter(pk=uid).first() if uid else None
    if user is None:
        return render(request, "eposta/abonelik.html", {"durum": "gecersiz"}, status=400)
    if request.method == "POST":
        acik = request.POST.get("islem") == "geri"
        User.objects.filter(pk=user.pk).update(email_notifications=acik)
        logger.info("e-posta bülteni tercihi: uid=%s acik=%s (bağlantı)", user.pk, acik)
        durum = "acildi" if acik else "cikildi"
    else:
        durum = "onay" if user.email_notifications else "zaten"
    return render(request, "eposta/abonelik.html", {
        "durum": durum, "email": user.email, "token": request.GET.get("t") or request.POST.get("t"),
    })
