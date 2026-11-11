"""
Asistan ekleri uçları — /api/v1/assistant/attachments/... (docs/plan-asistan-ekler.md 1.3).

Yükleme ile sohbete gönderme bilinçli olarak AYRI uçlar: kullanıcı soruyu yazarken dosya
hazırlanır, "çok uzun" gibi hatalar göndermeden önce görünür, ilerleme / yeniden deneme /
tekilleştirme mümkün olur. Sözleşme: docs/mobil-asistan-ekler.md.
"""
import hashlib
import os
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import permissions, serializers
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.premium import MSG_CHAT, require_premium

from .models import AssistantAttachment, CompanyProfile
from .services import attachments as ek_servis
from .services.ekler import (
    EkIstekHatasi,
    ek_etkin,
    ek_sozlugu,
    gunluk_kalan,
    kopya_olustur,
    kopyalanabilir_mi,
    kota_denetle,
    limitler,
)


class EkYukleSerializer(serializers.Serializer):
    dosya = serializers.FileField()
    ad = serializers.CharField(max_length=255, required=False, allow_blank=True)
    # İstemcinin iddiası; imzayla çelişirse İMZA kazanır.
    tur = serializers.ChoiceField(choices=AssistantAttachment.Tur.choices, required=False)
    kaynak = serializers.ChoiceField(choices=AssistantAttachment.Kaynak.choices,
                                     default=AssistantAttachment.Kaynak.CIHAZ)
    ikn = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    ekap_id = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    dtn = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    parca = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")


_EK_ORNEK = {
    "id": "6f1c2b8e-1d2a-4c55-9b0e-2f7e1a9c3d41", "tur": "dokuman", "kaynak": "ihale_dokumani",
    "ad": "TEKNİK ŞARTNAME.docx", "uzanti": "docx", "boyut": 482113, "durum": "hazirlaniyor",
    "hata_kodu": None, "hata_mesaji": None, "mod": None, "sayfa_sayisi": None, "sira": None,
    "ikn": "2026/123456", "ekap_id": "mobil:2026-123456", "dtn": None,
    "parca": "Teknik Şartname", "kucuk_resim": None,
    "created_at": "2026-11-11T14:32:05+03:00", "expires_at": None,
}


def _hak_ve_ek(user, ek, status):
    return Response({"ek": ek_sozlugu(ek), "gunluk_kalan": gunluk_kalan(user)}, status=status)


@extend_schema(
    tags=["assistant"],
    summary="Ek sınırları ve kalan hak",
    description=(
        "Mobil türleri, boyut sınırlarını, mesaj/sohbet başı sınırları, bugünkü kalan hakkı "
        "ve yoklama aralığını buradan okur — **sayılar koda yazılmaz**. `etkin` kullanıcı "
        "bazındadır (`ASSISTANT_EK_KAPSAM`: kapali | staff | herkes); `false` ise '+' "
        "düğmesi gösterilmez ve mesajlarda `yetenekler:[\"ek\"]` GÖNDERİLMEZ."
    ),
    responses={200: OpenApiTypes.OBJECT},
    examples=[OpenApiExample("Sınırlar", response_only=True, value={
        "etkin": True, "azami_bayt": 20971520, "foto_azami_bayt": 10485760,
        "turler": [{"uzanti": "pdf", "tur": "dokuman"}, {"uzanti": "jpg", "tur": "fotograf"}],
        "mesaj_azami": {"dokuman": 2, "fotograf": 4},
        "sohbet_azami": {"dokuman": 5, "fotograf": 12},
        "gunluk_kalan": {"dokuman": 18, "fotograf": 40}, "saklama_gun": 30,
        "yoklama_aralik_ms": 1500, "yoklama_azami_sn": 120,
    })],
)
class EkSinirlariView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(limitler(request.user))


@extend_schema(
    tags=["assistant"],
    summary="Sohbete ek yükle (doküman ya da fotoğraf)",
    description=(
        "Pro. Gövde `multipart/form-data`. Dosya hemen **202** + `ek` (`durum: "
        "hazirlaniyor`) döner; hazırlık arka planda yapılır, durum "
        "`GET /assistant/attachments/<id>/` ile yoklanır.\n\n"
        "**Tekilleştirme:** aynı dosya (sha256) daha önce hazırlandıysa **200** + "
        "`durum: hazir` döner, hak harcanmaz — istemci yoklamaz.\n\n"
        "**Tür imzadan belirlenir** (uzantı ipucudur): EKAP'ın içi HTML olan `.doc`'ları "
        "kabul edilir; `.pdf` diye gelen HTML `gecersiz_dosya`. HEIC ve arşivler "
        "`desteklenmeyen_tur`.\n\n"
        "Hatalar `errors.code` ile: `premium_required` (403), `profil_yok` (400), "
        "`dosya_buyuk` / `desteklenmeyen_tur` / `gecersiz_dosya` (400), `ek_kotasi` "
        "(429, `errors.sifirlanma` + `errors.limit`), `ek_gecici_kapali` (503). "
        "⚠️ 20 MB'ı aşan istek nginx'te **413 HTML** döner — boyutu önceden denetleyin."
    ),
    request={"multipart/form-data": {
        "type": "object",
        "properties": {
            "dosya": {"type": "string", "format": "binary"},
            "ad": {"type": "string", "description": "Türkçe orijinal ad (dosya yolu ASCII olabilir)."},
            "tur": {"type": "string", "enum": ["dokuman", "fotograf"]},
            "kaynak": {"type": "string",
                       "enum": ["ihale_dokumani", "arsiv", "cihaz", "kamera", "galeri"]},
            "ikn": {"type": "string"}, "ekap_id": {"type": "string"},
            "dtn": {"type": "string"}, "parca": {"type": "string"},
        },
        "required": ["dosya"],
    }},
    responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT,
               403: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    examples=[OpenApiExample("Kabul edildi", response_only=True, status_codes=["202"],
                             value={"ek": _EK_ORNEK,
                                    "gunluk_kalan": {"dokuman": 17, "fotograf": 40}})],
)
class EkYukleView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        user = request.user
        require_premium(user, MSG_CHAT)
        if not ek_etkin(user):
            raise EkIstekHatasi("ek_gecici_kapali", 503, "Dosya ekleme şu an kapalı.")
        if not CompanyProfile.objects.filter(user=user).exists():
            raise EkIstekHatasi("profil_yok", 400, "Önce firma profilinizi oluşturun.")

        ser = EkYukleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        dosya = v["dosya"]
        ad = (v.get("ad") or dosya.name or "dosya").strip()[:255]
        uzanti = os.path.splitext(ad)[1].lstrip(".").lower()[:16]

        self._boyut(dosya.size, settings.ASSISTANT_EK_AZAMI_BAYT)
        yol, sha = self._yaz(dosya)
        try:
            tur = self._tur(yol, uzanti)
            if tur == "fotograf":
                self._boyut(dosya.size, settings.ASSISTANT_EK_FOTO_AZAMI_BAYT)
            ust_veri = {"kaynak": v["kaynak"], "ikn": v["ikn"], "ekap_id": v["ekap_id"],
                        "dtn": v["dtn"], "parca": v["parca"], "ad": ad, "uzanti": uzanti}

            tekrar = self._tekrar(user, sha, ust_veri)
            if tekrar is not None:
                return _hak_ve_ek(user, tekrar, 200)  # yeni ham dosya `finally`'de silinir

            kota_denetle(user, tur)
            with transaction.atomic():
                ek = AssistantAttachment.objects.create(
                    user=user, tur=tur, mime=getattr(dosya, "content_type", "") or "",
                    boyut=dosya.size, sha256=sha, depo_yolu=str(yol), **ust_veri)
                from .tasks import ek_hazirla_task

                ek_id = str(ek.id)
                transaction.on_commit(lambda: ek_hazirla_task.delay(ek_id))
            yol = None  # artık görevin sorumluluğunda
            return _hak_ve_ek(user, ek, 202)
        finally:
            if yol:
                try:
                    os.remove(yol)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _boyut(boyut, azami):
        if boyut > azami:
            mb = round(boyut / 1024 / 1024)
            raise EkIstekHatasi(
                "dosya_buyuk", 400,
                f"Dosya çok büyük ({mb} MB). En fazla {azami // (1024 * 1024)} MB.",
                limit=azami)

    @staticmethod
    def _yaz(dosya):
        """`chunks()` ile ek dizinine yazar, yazarken sha256 hesaplar."""
        os.makedirs(settings.ASSISTANT_EK_DIR, exist_ok=True)
        yol = os.path.join(settings.ASSISTANT_EK_DIR, uuid.uuid4().hex)
        ozet = hashlib.sha256()
        with open(yol, "wb") as hedef:
            for parca in dosya.chunks():
                ozet.update(parca)
                hedef.write(parca)
        return yol, ozet.hexdigest()

    @staticmethod
    def _tur(yol, uzanti):
        with open(yol, "rb") as f:
            tani = ek_servis.imza_tani(f.read(8192))
        try:
            if tani == "zip":
                tani = ek_servis.zip_tani(yol)
        except ek_servis.EkHatasi as e:
            raise EkIstekHatasi(e.kod, 400)
        if not ek_servis.uzanti_tutarli_mi(uzanti, tani):
            raise EkIstekHatasi("gecersiz_dosya", 400)
        tur = ek_servis.tur_sec(tani)
        if tur is None:
            raise EkIstekHatasi("desteklenmeyen_tur", 400)
        return tur

    @staticmethod
    def _tekrar(user, sha, ust_veri):
        """
        Aynı kullanıcı + sha256 için hazır ek varsa: bağlanmamışsa aynısı, başka sohbete
        bağlıysa içeriği kopyalanmış yeni satır (hak yok). Kalan ömrü kısaysa None
        (yeniden yüklenir — Files API süresi uzatılamaz).
        """
        adaylar = AssistantAttachment.objects.filter(
            user=user, sha256=sha, durum="hazir", expires_at__gt=timezone.now()
        ).order_by("-created_at")
        for ek in adaylar:
            if not kopyalanabilir_mi(ek):
                continue
            if ek.conversation_id is None:
                return ek
            return kopya_olustur(ek, **ust_veri)
        return None


@extend_schema(tags=["assistant"])
class EkDetayView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _ek(self, request, ek_id):
        ek = AssistantAttachment.objects.filter(user=request.user, id=ek_id).first()
        if not ek:
            raise EkIstekHatasi("ek_bulunamadi", 404, "Dosya bulunamadı.")
        return ek

    @extend_schema(
        summary="Ek durumu",
        description=("Hazırlık durumunu döner (`hazirlaniyor` → `hazir` | `hata`). Mobil "
                     "`limits.yoklama_aralik_ms`'de bir, en çok `limits.yoklama_azami_sn` yoklar. "
                     "Hata metni `ek.hata_mesaji`'ndadır."),
        responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
        examples=[OpenApiExample("Hazır", response_only=True, value={"ek": {
            **_EK_ORNEK, "durum": "hazir", "mod": "metin", "sayfa_sayisi": 14,
            "expires_at": "2026-12-11T14:32:09+03:00"}})],
    )
    def get(self, request, ek_id):
        return Response({"ek": ek_sozlugu(self._ek(request, ek_id))})

    @extend_schema(
        summary="Eki tepsiden çıkar",
        description=("Yalnızca sohbete bağlanmamış ek silinir; günlük hak iade edilir. "
                     "Bağlıysa **409** `ek_bagli`."),
        responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT},
    )
    def delete(self, request, ek_id):
        ek = self._ek(request, ek_id)
        if ek.conversation_id is not None:
            raise EkIstekHatasi("ek_bagli", 409, "Bu dosya sohbete gönderildi; silinemez.")
        ek.delete()  # post_delete sinyali ham dosyayı ve Files API dosyasını siler
        return Response({"silindi": True, "gunluk_kalan": gunluk_kalan(request.user)})
