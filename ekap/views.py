"""
ekap servis view'ları — /api/v1/ekap/...

Hepsi kendi DB'mizden okur (hızlı, rate-limit yok). Global zarf (core renderer)
otomatik uygulanır. Detay/belge-url için gerekirse EKAP'a canlı düşülür.
"""
import logging

from django.core.cache import cache
from django.db.models import Q
from rest_framework import permissions
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


class CityListView(APIView):
    """GET /ekap/cities/ — il listesi."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return api_response(data=CitySerializer(City.objects.all(), many=True).data)
