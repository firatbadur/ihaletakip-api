"""
ekap servis view'ları — /api/v1/ekap/...

Hepsi kendi DB'mizden okur (hızlı, rate-limit yok). Global zarf (core renderer)
otomatik uygulanır. Detay/belge-url için gerekirse EKAP'a canlı düşülür.
"""
import logging

from django.core.cache import cache
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    inline_serializer,
)
from rest_framework import permissions, serializers
from rest_framework.views import APIView

from core.response import api_response

from .models import Announcement, Authority, City, OkasCode, Tender
from .serializers import (
    AuthoritySerializer,
    CitySerializer,
    EkapAnnouncementSerializer,
    EkapTenderListSerializer,
    OkasCodeSerializer,
)
from .utils import parse_ekap_datetime

logger = logging.getLogger("ihaletakip")


def _int_list(raw):
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


# Tarih parametreleri `DD.MM.YYYY [HH:mm]` veya ISO-8601 kabul eder (bkz. utils.parse_ekap_datetime).
# Çözümlenemeyen tarih **sessizce yok sayılır** — filtre uygulanmaz.
_DATE_HINT = "`GG.AA.YYYY`, `GG.AA.YYYY SS:dd` veya ISO-8601. Geçersiz tarih yok sayılır."

_TENDER_KEY_PARAM = OpenApiParameter(
    name="key",
    location=OpenApiParameter.PATH,
    type=str,
    required=True,
    description=(
        "**EKAP iç kimliği (`ekap_id`) kullanın** — ör. `1234567`.\n\n"
        "View, İKN ile de arama yapar; ancak bu rota `/` içeren bir değeri "
        "eşleştiremez ve `%2F` ile kodlamak da işe yaramaz (sunucu yolu çözerken "
        "geri `/` yapar). Dolayısıyla `2025/1234567` biçimindeki bir İKN burada "
        "**404 döner**; slash içermeyen `ekap_id` kullanın."
    ),
    examples=[OpenApiExample("EKAP iç kimliği", value="1234567")],
)


@extend_schema(
    tags=["ekap"],
    summary="İhale ara / listele",
    description=(
        "İhaleleri **kendi veritabanımızdan** arar — EKAP'a istek gitmez, rate limit "
        "yoktur. Alan adları EKAP'ın kendi adlandırmasıyla döner.\n\n"
        "Yanıt: `data.list` (ihaleler), `data.totalCount` (toplam kayıt), `data.page`.\n\n"
        "`il`, `tur`, `usul` ve `durum` **virgülle ayrılmış id listesi** alır "
        "(ör. `tur=1,3`). Sayı olmayan değerler yok sayılır.\n\n"
        "**İhale türü (`tur`):** 1 Mal Alımı · 2 Yapım · 3 Hizmet · 4 Danışmanlık\n\n"
        "**İhale usulü (`usul`):** 1 Açık İhale · 2 Belli İstekliler Arasında · "
        "3 Pazarlık (MD 21 F) · 4 Doğrudan Temin\n\n"
        "**İhale durumu (`durum`):** 1 Taslak · 2/3 Katılıma Açık · "
        "4 Değerlendirme Tamamlanmış · 5 Değerlendirmede · 6/10 İptal Edilmiş · "
        "15 Sonuç İlanı Yayımlanmış · 20 Sözleşme İmzalanmış\n\n"
        "**İl id'leri** EKAP'ın kendi il kodlarıdır (plaka değil!) — "
        "`GET /api/v1/ekap/cities/` ile alın. Örn. Ankara `251`, İstanbul `284`."
    ),
    parameters=[
        OpenApiParameter(
            "q", str, description="İhale adında veya İKN'de geçen metin (kısmi eşleşme).",
            examples=[OpenApiExample("Metin arama", value="bilgisayar")],
        ),
        OpenApiParameter(
            "il", str,
            description="EKAP il id listesi (virgülle). `GET /ekap/cities/` ile alın.",
            examples=[OpenApiExample("Ankara + İstanbul", value="251,284")],
        ),
        OpenApiParameter(
            "tur", str, description="İhale türü id listesi: 1 Mal, 2 Yapım, 3 Hizmet, 4 Danışmanlık.",
            examples=[OpenApiExample("Mal + Hizmet", value="1,3")],
        ),
        OpenApiParameter(
            "usul", str, description="İhale usulü id listesi (1-4).",
            examples=[OpenApiExample("Açık ihale", value="1")],
        ),
        OpenApiParameter(
            "durum", str, description="İhale durumu id listesi.",
            examples=[OpenApiExample("Katılıma açık", value="2,3")],
        ),
        OpenApiParameter(
            "ihale_baslangic", str, description=f"İhale tarihi alt sınırı. {_DATE_HINT}",
            examples=[OpenApiExample("Tarih", value="01.01.2026")],
        ),
        OpenApiParameter(
            "ihale_bitis", str, description=f"İhale tarihi üst sınırı. {_DATE_HINT}",
            examples=[OpenApiExample("Tarih", value="31.12.2026")],
        ),
        OpenApiParameter(
            "ilan_baslangic", str, description=f"İlan tarihi alt sınırı. {_DATE_HINT}",
            examples=[OpenApiExample("Tarih", value="01.01.2026")],
        ),
        OpenApiParameter(
            "ilan_bitis", str, description=f"İlan tarihi üst sınırı. {_DATE_HINT}",
            examples=[OpenApiExample("Tarih", value="31.12.2026")],
        ),
        OpenApiParameter(
            "order", str, enum=["ihaleTarihi", "ilanTarihi"], default="ihaleTarihi",
            description="Sıralama alanı.",
        ),
        OpenApiParameter(
            "siralamaTipi", str, enum=["desc", "asc"], default="desc",
            description="Sıralama yönü.",
        ),
        OpenApiParameter("page", int, default=1, description="Sayfa numarası (1'den başlar)."),
        OpenApiParameter(
            "page_size", int, default=10,
            description="Sayfa boyutu. **En fazla 100** (aşan değer 100'e kırpılır).",
        ),
    ],
    responses={200: EkapTenderListSerializer(many=True)},
)
class TenderListView(APIView):
    """GET /ekap/tenders/ — DB'den arama/filtre/sıralama/pagination."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qp = request.query_params
        qs = Tender.objects.all()

        q = (qp.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(ihale_adi__icontains=q) | Q(ikn__icontains=q))

        if qp.get("il"):
            qs = qs.filter(il_id__in=_int_list(qp.get("il")))
        if qp.get("tur"):
            qs = qs.filter(ihale_tip__in=_int_list(qp.get("tur")))
        if qp.get("usul"):
            qs = qs.filter(ihale_usul__in=_int_list(qp.get("usul")))
        if qp.get("durum"):
            qs = qs.filter(ihale_durum__in=_int_list(qp.get("durum")))

        for field, gte, lte in (
            ("ihale_tarihi", "ihale_baslangic", "ihale_bitis"),
            ("ilan_tarihi", "ilan_baslangic", "ilan_bitis"),
        ):
            if qp.get(gte):
                d = parse_ekap_datetime(qp.get(gte))
                if d:
                    qs = qs.filter(**{f"{field}__gte": d})
            if qp.get(lte):
                d = parse_ekap_datetime(qp.get(lte))
                if d:
                    qs = qs.filter(**{f"{field}__lte": d})

        # Sıralama
        order = qp.get("order", "ihaleTarihi")
        direction = qp.get("siralamaTipi", "desc")
        field = "ilan_tarihi" if order == "ilanTarihi" else "ihale_tarihi"
        prefix = "" if direction == "asc" else "-"
        qs = qs.order_by(f"{prefix}{field}")

        # Pagination
        try:
            page = max(1, int(qp.get("page", 1)))
            page_size = min(100, max(1, int(qp.get("page_size", 10))))
        except (TypeError, ValueError):
            page, page_size = 1, 10

        total = qs.count()
        start = (page - 1) * page_size
        items = qs[start:start + page_size]
        data = EkapTenderListSerializer(items, many=True).data
        return api_response(data={"list": data, "totalCount": total, "page": page})


@extend_schema(
    tags=["ekap"],
    summary="İhale detayı",
    description=(
        "İhale detayını EKAP'ın ham detay şekliyle döner (`item` açılmış olarak).\n\n"
        "Detay veritabanında varsa oradan servis edilir; bayatsa arka planda yenilenir "
        "ve eldeki sürüm hemen döner. Detay hiç çekilmemişse EKAP'a **canlı** gidilir "
        "(bu istek yavaş olabilir) ve sonuç saklanır.\n\n"
        "EKAP'a ulaşılamaz ve elde eski bir kopya varsa `200` + "
        "`message: \"Detay güncel değil.\"` döner. Hiç kayıt yoksa `404`.\n\n"
        "Yanıt `ilanList` alanını zaten içerir — ilanlar için ayrıca "
        "`/announcements/` çağırmanız gerekmez."
    ),
    parameters=[_TENDER_KEY_PARAM],
    # Detay EKAP'ın ham şeklidir; sabit bir serializer'a bağlanmaz.
    responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
)
class TenderDetailView(APIView):
    """GET /ekap/tenders/{key}/ — İKN veya ekap_id ile detay (EKAP şeklinde)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, key):
        tender = Tender.objects.filter(Q(ikn=key) | Q(ekap_id=key)).first()

        # DB'de var ve detayı çekilmiş → saklanan ham detayı (item açılmış) dön
        if tender and tender.detail_synced_at and tender.detail_raw:
            # Bayatsa arka planda yenile, eldekini dön
            self._maybe_refresh(tender)
            raw = tender.detail_raw
            data = raw.get("item", raw) if isinstance(raw, dict) else raw
            return api_response(data=data)

        # Detay yoksa → canlı çek (lazy sync)
        ekap_id = tender.ekap_id if tender else key
        try:
            from .client import EkapV2Client
            from . import sync as sync_mod

            client = EkapV2Client()
            detail = client.get_detail(ekap_id)
            sync_mod.upsert_tender_detail(ekap_id, detail)
            return api_response(data=detail.get("item", detail) if isinstance(detail, dict) else detail)
        except Exception as e:
            logger.warning("Canlı detay çekilemedi (%s): %s", key, e)
            if tender:
                return api_response(data=tender.detail_raw or {}, message="Detay güncel değil.")
            return api_response(message="İhale bulunamadı.", success=False, status=404)

    @staticmethod
    def _maybe_refresh(tender):
        from . import sync as sync_mod

        try:
            if sync_mod.should_refresh_detail(tender):
                from .tasks import sync_detail
                sync_detail.delay(tender.ekap_id)
        except Exception:
            pass  # Celery/redis yoksa sessiz geç


@extend_schema(
    tags=["ekap"],
    summary="İhale ilanları",
    description=(
        "İhaleye ait ilanları veritabanından döner (`data.list`). İhale bulunamazsa "
        "hata değil, boş liste döner.\n\n"
        "**İlan tipleri:** 1 İhale İlanı · 2 Düzeltme İlanı · 3 İptal İlanı · "
        "4 Sonuç İlanı · 5 Ön İlan · 10 Ön Yeterlik İlanı"
    ),
    parameters=[_TENDER_KEY_PARAM],
    responses={200: EkapAnnouncementSerializer(many=True)},
)
class TenderAnnouncementsView(APIView):
    """GET /ekap/tenders/{key}/announcements/ — DB'deki ilanlar."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, key):
        tender = Tender.objects.filter(Q(ikn=key) | Q(ekap_id=key)).first()
        if not tender:
            return api_response(data={"list": []})
        ilanlar = Announcement.objects.filter(tender=tender)
        data = EkapAnnouncementSerializer(ilanlar, many=True).data
        return api_response(data={"list": data})


@extend_schema(
    tags=["ekap"],
    summary="Belge indirme bağlantısı",
    description=(
        "İhale dokümanının indirme bağlantısını döner (`data.url`). Bu URL EKAP "
        "tarafında **dinamik üretilir ve kısa ömürlüdür**, bu yüzden istek anında "
        "canlı olarak EKAP'a gidilir. Sonuç 5 dakika önbelleklenir.\n\n"
        "EKAP'a ulaşılamazsa `502` döner."
    ),
    parameters=[
        OpenApiParameter(
            name="ekap_id",
            location=OpenApiParameter.PATH,
            type=str,
            required=True,
            description="EKAP iç kimliği (İKN değil).",
            examples=[OpenApiExample("EKAP iç kimliği", value="1234567")],
        ),
        OpenApiParameter(
            "islemId", str, default="1",
            description="EKAP belge işlem kimliği (`1` = ihale dokümanı).",
        ),
    ],
    responses={
        200: inline_serializer(
            name="DocumentUrl", fields={"url": serializers.CharField(allow_null=True)}
        )
    },
)
class DocumentUrlView(APIView):
    """GET /ekap/tenders/{ekap_id}/document-url/ — canlı proxy (dinamik URL)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ekap_id):
        islem_id = request.query_params.get("islemId", "1")
        cache_key = f"ekap:docurl:{ekap_id}:{islem_id}"
        cached = cache.get(cache_key)
        if cached:
            return api_response(data={"url": cached})
        try:
            from .client import EkapV2Client

            resp = EkapV2Client().get_document_url(ekap_id, islem_id)
            url = resp.get("url") if isinstance(resp, dict) else None
            if url:
                cache.set(cache_key, url, timeout=300)
            return api_response(data={"url": url})
        except Exception as e:
            logger.warning("Belge URL alınamadı (%s): %s", ekap_id, e)
            return api_response(message="Belge bağlantısı alınamadı.", success=False, status=502)


@extend_schema(
    tags=["ekap"],
    summary="OKAS kodu ara",
    description=(
        "OKAS (Ortak Kamu Alım Sözlüğü) kodlarını arar. `q` koda **önek** olarak, "
        "Türkçe/İngilizce adlara **kısmi** olarak uygulanır. `q` boşsa ilk kayıtlar döner."
    ),
    parameters=[
        OpenApiParameter(
            "q", str, description="Kod öneki ya da ad içinde geçen metin.",
            examples=[OpenApiExample("Ad ile", value="bilgisayar"),
                      OpenApiExample("Kod öneki ile", value="30200000")],
        ),
        OpenApiParameter(
            "take", int, default=50,
            description="Dönecek kayıt sayısı. **En fazla 200**.",
        ),
    ],
    responses={200: OkasCodeSerializer(many=True)},
)
class OkasSearchView(APIView):
    """GET /ekap/okas/search?q= — DB'den OKAS arama."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        take = min(int(request.query_params.get("take", 50)), 200)
        qs = OkasCode.objects.all()
        if q:
            qs = qs.filter(Q(adi__icontains=q) | Q(adi_eng__icontains=q) | Q(kod__startswith=q))
        return api_response(data=OkasCodeSerializer(qs[:take], many=True).data)


@extend_schema(
    tags=["ekap"],
    summary="İdare (kurum) ara",
    description=(
        "İhaleyi açan idareleri arar. `q` idare adında **kısmi**, idare kodunda "
        "**önek** olarak eşleşir. `q` boşsa ilk kayıtlar döner."
    ),
    parameters=[
        OpenApiParameter(
            "q", str, description="İdare adı ya da idare kodu öneki.",
            examples=[OpenApiExample("Kurum adı", value="ankara büyükşehir")],
        ),
        OpenApiParameter(
            "take", int, default=50,
            description="Dönecek kayıt sayısı. **En fazla 200**.",
        ),
    ],
    responses={200: AuthoritySerializer(many=True)},
)
class AuthoritySearchView(APIView):
    """GET /ekap/authorities/search?q= — DB'den kurum arama."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        take = min(int(request.query_params.get("take", 50)), 200)
        qs = Authority.objects.all()
        if q:
            qs = qs.filter(Q(ad__icontains=q) | Q(idare_kod__startswith=q))
        return api_response(data=AuthoritySerializer(qs[:take], many=True).data)


@extend_schema(
    tags=["ekap"],
    summary="İl listesi",
    description=(
        "81 ili döner. Buradaki `id` alanı **EKAP'ın il kodudur** (plaka değil) ve "
        "`GET /ekap/tenders/` ucundaki `il` filtresinde kullanılır. Plaka ayrı bir alandır."
    ),
    responses={200: CitySerializer(many=True)},
)
class CityListView(APIView):
    """GET /ekap/cities/ — il listesi."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return api_response(data=CitySerializer(City.objects.all(), many=True).data)
