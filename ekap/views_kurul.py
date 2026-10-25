"""
KİK kurul karar tutanakları (uyuşmazlık) uçları — `/api/v1/ekap/kurul-kararlari/...`.

Veri `ekap.mobil.kurul` görevinin topladığı `KurulKarari` tablosundan servis edilir;
uçlar EKAP'a hiç gitmez. Herkese açık (alarm Pro'dur, bkz. `TenderAlarm.kurul_karar`).

Mobil entegrasyon notu: `docs/mobil-kurul-karar.md`.
"""
from django.db.models import Count
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample, OpenApiParameter, extend_schema, inline_serializer,
)
from rest_framework import permissions, serializers
from rest_framework.views import APIView

from core.response import api_response

from .models import KurulKarari, Tender
from .serializers import KurulKarariSerializer
from .utils import normalize_tr

_SAYFA = inline_serializer(
    name="KurulKarariSayfa",
    fields={
        "list": KurulKarariSerializer(many=True),
        "totalCount": serializers.IntegerField(),
        "page": serializers.IntegerField(),
    },
)


def ekap_id_haritasi(kararlar) -> dict:
    """{ikn: ekap_id} — tek sorgu (serializer'da satır başına sorgu N+1 olurdu)."""
    iknler = {k.ikn for k in kararlar if k.ikn}
    if not iknler:
        return {}
    return dict(Tender.objects.filter(ikn__in=iknler).order_by()
                .values_list("ikn", "ekap_id"))


def ihale_kararlari(ikn: str) -> list:
    """Bir ihalenin kararları (yeniden eskiye) — ihale detay yanıtına da gömülür."""
    if not ikn:
        return []
    return list(KurulKarari.objects.filter(ikn=ikn).order_by("-toplanti_tarihi", "sira_no"))


@extend_schema(
    tags=["kurul-kararlari"],
    auth=[],
    operation_id="ekap_kurul_kararlari_list",
    summary="KİK uyuşmazlık kararları",
    description=(
        "Kamu İhale Kurulu'nun **uyuşmazlık** kararları (itirazen şikâyet başvuruları), "
        "yeniden eskiye. Kurul çoğunlukla Çarşamba toplanır, kararlar Perşembe 00:00'da "
        "yayımlanır; veri her sabah kontrol edilir.\n\n"
        "`ekap_id` doluysa ihale bizde vardır → `/ekap/tenders/{ekap_id}/` açılabilir."
    ),
    parameters=[
        OpenApiParameter("ikn", str, description="İKN (YYYY/NNNNNNN).",
                         examples=[OpenApiExample("İKN", value="2026/1676818")]),
        OpenApiParameter("q", str, description="İhale adı / idare / başvuru sahibi / karar no."),
        OpenApiParameter("toplanti", str, description="Toplantı günü (YYYY-MM-DD)."),
        OpenApiParameter("page", int, default=1),
        OpenApiParameter("page_size", int, default=20, description="En fazla 100."),
    ],
    responses={200: _SAYFA},
)
class KurulKarariListView(APIView):
    """GET /ekap/kurul-kararlari/"""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        qp = request.query_params
        qs = KurulKarari.objects.all()
        if qp.get("ikn"):
            qs = qs.filter(ikn=qp["ikn"].strip())
        if qp.get("toplanti"):
            qs = qs.filter(toplanti_tarihi__date=qp["toplanti"].strip())
        q = (qp.get("q") or "").strip()
        if q:
            qs = qs.filter(arama_norm__contains=normalize_tr(q))
        qs = qs.order_by("-toplanti_tarihi", "sira_no", "pk")
        try:
            page = max(1, int(qp.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            boyut = min(100, max(1, int(qp.get("page_size", 20))))
        except (TypeError, ValueError):
            boyut = 20
        toplam = qs.count()
        sayfa = list(qs[(page - 1) * boyut: page * boyut])
        veri = KurulKarariSerializer(sayfa, many=True,
                                     context={"ekap_idler": ekap_id_haritasi(sayfa)}).data
        return api_response(data={"list": veri, "totalCount": toplam, "page": page})


@extend_schema(
    tags=["kurul-kararlari"],
    auth=[],
    operation_id="ekap_kurul_toplantilar",
    summary="Kurul toplantıları",
    description="Kararı bulunan toplantılar (yeniden eskiye) ve karar sayıları.",
    responses={200: OpenApiTypes.OBJECT},
)
class KurulToplantiListView(APIView):
    """GET /ekap/kurul-kararlari/toplantilar/"""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        satirlar = (KurulKarari.objects.exclude(toplanti_tarihi=None).order_by()
                    .values("toplanti_tarihi", "kurul_yil", "kurul_sayi")
                    .annotate(karar_sayisi=Count("id")).order_by("-toplanti_tarihi"))
        return api_response(data=[
            {"toplanti_tarihi": s["toplanti_tarihi"],
             "kurul": f"{s['kurul_yil']} / {s['kurul_sayi']}" if s["kurul_yil"] else "",
             "karar_sayisi": s["karar_sayisi"]}
            for s in satirlar[:100]
        ])


@extend_schema(
    tags=["kurul-kararlari"],
    auth=[],
    parameters=[OpenApiParameter("key", str, OpenApiParameter.PATH,
                                 description="İhalenin `ekap_id`'si (İKN `/` içerir).")],
    operation_id="ekap_tender_kurul_kararlari",
    summary="İhalenin kurul kararları",
    description="Bu ihale hakkında verilmiş KİK uyuşmazlık kararları. Yoksa boş liste.",
    responses={200: KurulKarariSerializer(many=True)},
)
class TenderKurulKarariView(APIView):
    """GET /ekap/tenders/<key>/kurul-kararlari/"""

    permission_classes = [permissions.AllowAny]

    def get(self, request, key):
        from .views import _tender_by_key

        tender = _tender_by_key(key, defer_raw=True)
        if tender is None:
            return api_response(message="İhale bulunamadı.", success=False, status=404)
        kararlar = ihale_kararlari(tender.ikn)
        return api_response(data=KurulKarariSerializer(
            kararlar, many=True, context={"ekap_idler": {tender.ikn: tender.ekap_id}}).data)
