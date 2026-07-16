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

from .constants import OZELLIK_MAP
from .detsis_tree import annotate_paths, descendant_idare_ids
from .models import Announcement, Authority, City, OkasCode, Tender
from .serializers import (
    AuthorityNodeSerializer,
    CitySerializer,
    EkapAnnouncementSerializer,
    EkapTenderListSerializer,
    OkasCodeSerializer,
)
from .utils import normalize_tr, parse_ekap_datetime

logger = logging.getLogger("ihaletakip")


def _int_list(raw):
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


def _str_list(raw):
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _as_int_list(value):
    """list ([1,2]) ya da virgüllü string ('1,2') → [int]. `SavedFilter.filters`
    JSON'da gerçek liste, query param'da virgüllü string gelir; ikisini de destekler."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for x in value:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    return _int_list(str(value))


def _as_str_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return _str_list(str(value))


def _tender_idare_id_set():
    """
    İhalede **gerçekten geçen** benzersiz `idare_id` kümesi (cache'li, 5 dk TTL).
    İdare ağaç seçimi büyük bakanlıklarda on binlerce alt birime açılabildiğinden
    (okullar vb.) genişletilen küme bununla kesiştirilir → küçük, hızlı IN listesi.
    Kısa TTL: yeni bir idarenin ilk ihalesi en fazla 5 dk gecikmeyle filtrelenebilir.
    """
    ids = cache.get("tender_idare_id_set")
    if ids is None:
        ids = set(
            Tender.objects.exclude(idare_id="").values_list("idare_id", flat=True).distinct()
        )
        cache.set("tender_idare_id_set", ids, 300)
    return ids


def apply_tender_filters(qs, params):
    """
    Tender queryset'ine filtre uygular ve queryset döner. Parametre adları **Tender model
    alan adlarıdır** (tek adlandırma): `ihale_adi`, `ikn`, `ikn_yil`, `ikn_sayi`, `il_id`,
    `ihale_tip`, `ihale_usul`, `ihale_durum`, `yasa_kapsami`, `idare_id`, `idare_detsis`,
    `ihale_tarihi_min/max`, `ilan_tarihi_min/max`, `okas_kod`, `okas_adi`, `ozellik`.

    `idare_id` doğrudan `Tender.idare_id` ile eşleşir (yaprak seçim). `idare_detsis`
    ise DETSIS ağaç düğümlerinin `detsis_no`'sudur; üst düğüm seçilince tüm alt
    birimlerin `idare_id`'lerine genişletilir (bkz. `detsis_tree.descendant_idare_ids`).

    `params` `.get(key)` destekleyen bir nesnedir: DRF `query_params` (liste alanları
    virgüllü string) ya da `SavedFilter.filters` gibi düz dict (gerçek liste). Liste
    alanları ikisini de kabul eder. Sıralama/pagination bu fonksiyonda değildir.
    """
    needs_distinct = False

    # ── Anahtar kelime (ihale adı + kurum adı + İKN) — mobil "Anahtar Kelime" kutusu ──
    # İhale adı/kurum adı **Türkçe-i güvenli** normalize sütunlar üzerinden aranır
    # (bkz. normalize_tr); İKN ASCII olduğundan icontains yeterli.
    q = params.get("q")
    if q and str(q).strip():
        nq = normalize_tr(q)
        qs = qs.filter(
            Q(ihale_adi_norm__contains=nq) | Q(idare_adi_norm__contains=nq) | Q(ikn__icontains=str(q).strip())
        )

    # ── Alan-özel metin arama (normalize edilmiş, Türkçe-i güvenli) ──
    ad = params.get("ihale_adi")
    if ad and str(ad).strip():
        qs = qs.filter(ihale_adi_norm__contains=normalize_tr(ad))
    idare_adi = params.get("idare_adi")
    if idare_adi and str(idare_adi).strip():
        qs = qs.filter(idare_adi_norm__contains=normalize_tr(idare_adi))
    ikn = params.get("ikn")
    if ikn and str(ikn).strip():
        qs = qs.filter(ikn__icontains=str(ikn).strip())

    # ── İKN yıl / sayı (ikn = "YIL/SAYI") ──
    ikn_yil = params.get("ikn_yil")
    if ikn_yil:
        qs = qs.filter(ikn__startswith=f"{str(ikn_yil).strip()}/")
    ikn_sayi = params.get("ikn_sayi")
    if ikn_sayi:
        qs = qs.filter(ikn__endswith=f"/{str(ikn_sayi).strip()}")

    # ── Liste filtreleri ──
    il_id = _as_int_list(params.get("il_id"))
    if il_id:
        qs = qs.filter(il_id__in=il_id)
    ihale_tip = _as_int_list(params.get("ihale_tip"))
    if ihale_tip:
        qs = qs.filter(ihale_tip__in=ihale_tip)
    ihale_usul = _as_int_list(params.get("ihale_usul"))
    if ihale_usul:
        qs = qs.filter(ihale_usul__in=ihale_usul)
    ihale_durum = _as_int_list(params.get("ihale_durum"))
    if ihale_durum:
        qs = qs.filter(ihale_durum__in=ihale_durum)
    idare_id = _as_str_list(params.get("idare_id"))
    if idare_id:
        qs = qs.filter(idare_id__in=idare_id)

    # ── İdare ağaç seçimi (detsis_no) → tüm alt birimlerin idare_id'lerine genişlet ──
    # Kullanıcı ağaçta bir ÜST düğüm (örn. bakanlık) seçince alt birimlerin
    # ihaleleri de gelsin diye seçilen detsis_no'ları descendant idare_id'lere açarız.
    idare_detsis = _as_str_list(params.get("idare_detsis"))
    if idare_detsis:
        expanded = descendant_idare_ids(idare_detsis)
        # Büyük bakanlıklar on binlerce alt birime (örn. okullar) açılır; bunların
        # çoğunun hiç ihalesi yoktur. IN listesini küçültmek ve sorguyu hızlandırmak
        # için yalnızca **ihalede gerçekten geçen** idare_id'lerle kesiştir.
        if expanded:
            expanded &= _tender_idare_id_set()
        # Hiç idare_id'e çözülmezse (bozuk ağaç / hiç ihalesi yok) yanlışlıkla TÜM
        # ihaleleri döndürmemek için boş küme uygula.
        qs = qs.filter(idare_id__in=expanded) if expanded else qs.none()

    # ── Yasa kapsamı — null-inclusive: detayı gelmemiş (yasa_kapsami=None) ihaleyi dışlama ──
    yasa_kapsami = _as_int_list(params.get("yasa_kapsami"))
    if yasa_kapsami:
        qs = qs.filter(Q(yasa_kapsami__in=yasa_kapsami) | Q(yasa_kapsami__isnull=True))

    # ── OKAS branş kodu / adı (ihaleye özel OkasItem üzerinden) ──
    okas_kod = _as_str_list(params.get("okas_kod"))
    if okas_kod:
        cond = Q()
        for kod in okas_kod:
            cond |= Q(okas_kalemleri__kodu__startswith=kod)
        qs = qs.filter(cond)
        needs_distinct = True
    okas_adi = _as_str_list(params.get("okas_adi"))
    if okas_adi:
        cond = Q()
        for adi in okas_adi:
            cond |= Q(okas_kalemleri__adi__icontains=adi)
        qs = qs.filter(cond)
        needs_distinct = True

    # ── Özellikler: OZELLIK_MAP anahtarları (virgülle) → ozellikler JSON etiketi ──
    for app_key in _as_str_list(params.get("ozellik")):
        tag = OZELLIK_MAP.get(app_key)
        if tag:
            qs = qs.filter(ozellikler__contains=[tag])

    # ── Tarih aralıkları (alan_min / alan_max) ──
    for field in ("ihale_tarihi", "ilan_tarihi"):
        mn = params.get(f"{field}_min")
        if mn:
            d = parse_ekap_datetime(str(mn))
            if d:
                qs = qs.filter(**{f"{field}__gte": d})
        mx = params.get(f"{field}_max")
        if mx:
            d = parse_ekap_datetime(str(mx))
            if d:
                qs = qs.filter(**{f"{field}__lte": d})

    if needs_distinct:
        qs = qs.distinct()
    return qs


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
        "**Filtre parametre adları = `Tender` model alan adlarıdır** (tek adlandırma). "
        "Liste alanları (`il_id`, `ihale_tip`, `ihale_usul`, `ihale_durum`, `yasa_kapsami`, "
        "`idare_id`, `okas_kod`, `okas_adi`, `ozellik`) **virgülle ayrılmış** verilir "
        "(ör. `ihale_tip=1,3`).\n\n"
        "**İhale türü (`ihale_tip`):** 1 Mal Alımı · 2 Yapım · 3 Hizmet · 4 Danışmanlık\n\n"
        "**İhale usulü (`ihale_usul`):** 1 Açık İhale · 2 Belli İstekliler Arasında · "
        "3 Pazarlık (MD 21 F) · 4 Doğrudan Temin\n\n"
        "**İhale durumu (`ihale_durum`):** 1 Taslak · 2/3 Katılıma Açık · "
        "4 Değerlendirme Tamamlanmış · 5 Değerlendirmede · 6/10 İptal Edilmiş · "
        "15 Sonuç İlanı Yayımlanmış · 20 Sözleşme İmzalanmış\n\n"
        "**İl id'leri (`il_id`)** EKAP'ın kendi il kodlarıdır (plaka değil!) — "
        "`GET /api/v1/ekap/cities/` ile alın. Örn. Ankara `251`, İstanbul `284`.\n\n"
        "Gelişmiş alanlar (`il_id`, `ihale_usul`, `yasa_kapsami`, `ozellik`, `okas_*`) "
        "detay senkronunda dolar; henüz detayı gelmemiş ihaleler bu filtrelerde eşleşmeyebilir."
    ),
    parameters=[
        OpenApiParameter(
            "q", str,
            description="Anahtar kelime — ihale adı + **kurum adı** + İKN içinde arar "
            "(mobil 'Anahtar Kelime' kutusu). Alan-özel arama için `ihale_adi`/`idare_adi`/`ikn`.",
            examples=[OpenApiExample("Anahtar kelime", value="siber güvenlik")],
        ),
        OpenApiParameter(
            "ihale_adi", str, description="Yalnızca ihale adında geçen metin (kısmi eşleşme).",
            examples=[OpenApiExample("Metin arama", value="bilgisayar")],
        ),
        OpenApiParameter(
            "idare_adi", str, description="Kurum (idare) adında geçen metin (kısmi eşleşme).",
            examples=[OpenApiExample("Kurum", value="belediyesi")],
        ),
        OpenApiParameter(
            "ikn", str, description="İKN'de geçen metin (kısmi eşleşme).",
            examples=[OpenApiExample("İKN parçası", value="2026/271")],
        ),
        OpenApiParameter(
            "ikn_yil", str, description="İKN yılı — İKN `YIL/...` ile başlayanlar.",
            examples=[OpenApiExample("2026 yılı", value="2026")],
        ),
        OpenApiParameter(
            "ikn_sayi", str, description="İKN sıra sayısı — İKN `.../SAYI` ile bitenler.",
            examples=[OpenApiExample("Sıra no", value="271215")],
        ),
        OpenApiParameter(
            "il_id", str,
            description="İl id listesi (virgülle). `GET /ekap/cities/` ile alın.",
            examples=[OpenApiExample("Ankara + İstanbul", value="251,284")],
        ),
        OpenApiParameter(
            "ihale_tip", str, description="İhale türü id listesi: 1 Mal, 2 Yapım, 3 Hizmet, 4 Danışmanlık.",
            examples=[OpenApiExample("Mal + Hizmet", value="1,3")],
        ),
        OpenApiParameter(
            "ihale_usul", str, description="İhale usulü id listesi (1-4).",
            examples=[OpenApiExample("Açık ihale", value="1")],
        ),
        OpenApiParameter(
            "ihale_durum", str, description="İhale durumu id listesi.",
            examples=[OpenApiExample("Katılıma açık", value="2,3")],
        ),
        OpenApiParameter(
            "idare_id", str, description="İdare (kurum) id listesi (virgülle) — "
            "doğrudan `Tender.idare_id` ile eşleşir (yaprak seçim).",
        ),
        OpenApiParameter(
            "idare_detsis", str,
            description="İdare DETSIS ağaç düğümü `detsis_no` listesi (virgülle). Üst "
            "düğüm (örn. bakanlık) seçilince altındaki tüm idarelerin ihaleleri gelir. "
            "Ağaç uçları: `GET /ekap/authorities/tree/`, `GET /ekap/authorities/search/`.",
            examples=[OpenApiExample("Bakanlık düğümü", value="24308110")],
        ),
        OpenApiParameter(
            "yasa_kapsami", str,
            description="Yasa kapsamı id listesi (1=4734, 2=4734 dışı, 3=istisna). "
            "Detayı gelmemiş ihaleler (kapsam boş) dışlanmaz.",
        ),
        OpenApiParameter(
            "okas_kod", str,
            description="OKAS branş kodu listesi (virgülle). Kod ile başlayan kalemleri olan ihaleler.",
            examples=[OpenApiExample("OKAS kodu", value="72415000")],
        ),
        OpenApiParameter(
            "okas_adi", str,
            description="OKAS branş adı listesi (virgülle, kısmi eşleşme).",
            examples=[OpenApiExample("OKAS adı", value="yazılım")],
        ),
        OpenApiParameter(
            "ozellik", str,
            description="Özellik anahtarları (virgülle): eIhale, kismiTeklifMi, "
            "altYukleniciCalistirilabilirMi, fiyatFarkiVerilecekMi, isDeneyimiGosterenBelgelerIsteniyorMu, "
            "meslekiTeknikYeterlilikBelgeleriIsteniyorMu, yabanciIsteklilereIzinVeriliyorMu, "
            "yerliIstekliyeFiyatAvantajiUgulaniyorMu, ekonomikVeMaliYeterlilikBelgeleriIsteniyorMu.",
        ),
        OpenApiParameter(
            "ihale_tarihi_min", str, description=f"İhale tarihi alt sınırı. {_DATE_HINT}",
            examples=[OpenApiExample("Tarih", value="01.01.2026")],
        ),
        OpenApiParameter(
            "ihale_tarihi_max", str, description=f"İhale tarihi üst sınırı. {_DATE_HINT}",
            examples=[OpenApiExample("Tarih", value="31.12.2026")],
        ),
        OpenApiParameter(
            "ilan_tarihi_min", str, description=f"İlan (yayın) tarihi alt sınırı. {_DATE_HINT}",
            examples=[OpenApiExample("Tarih", value="01.01.2026")],
        ),
        OpenApiParameter(
            "ilan_tarihi_max", str, description=f"İlan (yayın) tarihi üst sınırı. {_DATE_HINT}",
            examples=[OpenApiExample("Tarih", value="31.12.2026")],
        ),
        OpenApiParameter(
            "order", str, enum=["ihale_tarihi", "ilan_tarihi"], default="ihale_tarihi",
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

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

    def get(self, request):
        qp = request.query_params
        qs = apply_tender_filters(Tender.objects.all(), qp)

        # Sıralama (model alan adları)
        order = qp.get("order", "ihale_tarihi")
        direction = qp.get("siralamaTipi", "desc")
        field = "ilan_tarihi" if order == "ilan_tarihi" else "ihale_tarihi"
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

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

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

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

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

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

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

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        take = min(int(request.query_params.get("take", 50)), 200)
        qs = OkasCode.objects.all()
        if q:
            # Türkçe-i güvenli: normalize edilmiş Türkçe ad + İngilizce ad + kod öneki
            qs = qs.filter(
                Q(adi_norm__contains=normalize_tr(q)) | Q(adi_eng__icontains=q) | Q(kod__startswith=q)
            )
        return api_response(data=OkasCodeSerializer(qs[:take], many=True).data)


@extend_schema(
    tags=["ekap"],
    summary="İdare ağacı (gözat)",
    description=(
        "DETSIS **kurum ağacını** gözatma için döner (lazy). `parent` verilmezse "
        "**kök düğümler** (bakanlıklar + üst kategoriler) döner; `parent=<detsis_no>` "
        "verilince o düğümün **doğrudan çocukları** döner.\n\n"
        "Her düğüm: `detsis_no` (ağaç anahtarı), `idare_id` (ihale filtre anahtarı; "
        "dal düğümünde `null`), `ad`, `has_items` (çocuğu var mı → expand göster), "
        "`seviye`, `parent_detsis`. Seçilen düğümlerle ihale filtrelemek için "
        "`GET /ekap/tenders/?idare_detsis=<detsis_no,...>` kullanın."
    ),
    parameters=[
        OpenApiParameter(
            "parent", str,
            description="Üst düğümün `detsis_no`'su. Boşsa kök düğümler döner.",
            examples=[OpenApiExample("Bir bakanlığın çocukları", value="24308110")],
        ),
    ],
    responses={200: AuthorityNodeSerializer(many=True)},
    auth=[],
)
class AuthorityTreeView(APIView):
    """GET /ekap/authorities/tree/?parent= — DETSIS ağacında gözat (lazy)."""

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

    # Bir dalın döneceği azami çocuk sayısı. Bağlı ağaçta en kalabalık düğüm ~900
    # çocuk; yalnızca "Bağlantısız Kurumlar" (~87k) bunu aşar → orada ilk dilim döner,
    # kullanıcı arama ile bulur (aksi halde devasa payload).
    TREE_CHILD_LIMIT = 2000

    def get(self, request):
        parent = (request.query_params.get("parent") or "").strip()
        # parent boş → kökler (parent_detsis == ""); doluysa o düğümün çocukları
        qs = Authority.objects.filter(parent_detsis=parent).order_by("ad")[: self.TREE_CHILD_LIMIT]
        return api_response(data=AuthorityNodeSerializer(qs, many=True).data)


@extend_schema(
    tags=["ekap"],
    summary="İdare (kurum) ara",
    description=(
        "DETSIS kurum ağacında **ad ile** arar (kısmi eşleşme). Sonuçlar ağaç düğümüdür; "
        "her düğümde `detsis_no`, `idare_id` (filtre anahtarı), `has_items` ve `path` "
        "(kök→ebeveyn ata adları, breadcrumb) bulunur. Filtreleme için seçilen düğümlerin "
        "`detsis_no`'sunu `GET /ekap/tenders/?idare_detsis=` ile gönderin."
    ),
    parameters=[
        OpenApiParameter(
            "q", str, description="İdare adında geçen metin (kısmi eşleşme). En az 2 karakter.",
            examples=[OpenApiExample("Kurum adı", value="ankara büyükşehir")],
        ),
        OpenApiParameter(
            "take", int, default=50,
            description="Dönecek kayıt sayısı. **En fazla 200**.",
        ),
        OpenApiParameter(
            "only_with_tenders", bool, default=True,
            description="Varsayılan `true`: yalnızca **ihalesi olan** idareler + dal "
            "düğümleri döner (aynı kurumun ihalesiz kopyaları elenir → çıkmaz sokak yok). "
            "`false` verilirse ağaçtaki tüm eşleşen düğümler döner.",
        ),
    ],
    responses={200: AuthorityNodeSerializer(many=True)},
    auth=[],
)
class AuthoritySearchView(APIView):
    """GET /ekap/authorities/search?q= — DB'den kurum ağacı araması (ata yoluyla)."""

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        take = min(int(request.query_params.get("take", 50)), 200)
        only_useful = (request.query_params.get("only_with_tenders", "true")).lower() not in (
            "0", "false", "no",
        )
        qs = Authority.objects.all()
        if q:
            # Türkçe-i güvenli: normalize edilmiş ad + idare_id öneki
            qs = qs.filter(Q(ad_norm__contains=normalize_tr(q)) | Q(idare_id__startswith=q))
        if only_useful:
            # DETSIS'te aynı kurum (ör. ASKİ) birçok kez farklı idareId ile var; ihaleler
            # yalnızca birinin idareId'i altında. İhalesi OLMAYAN yaprak kopyalar kullanıcıyı
            # çıkmaza sokuyordu → yalnızca ihalesi geçen idareler + dal düğümlerini (seçilince
            # alt birimlere genişler) bırak.
            tender_ids = _tender_idare_id_set()
            qs = qs.filter(Q(has_items=True) | Q(idare_id__in=tender_ids))
        nodes = list(qs.order_by("ad")[:take])
        # Seçilebilir (idare_id dolu) düğümler önce gelsin; sonra ada göre (kararlı)
        nodes.sort(key=lambda a: (a.idare_id == "", a.ad))
        paths = annotate_paths(nodes)
        ser = AuthorityNodeSerializer(nodes, many=True, context={"paths": paths})
        return api_response(data=ser.data)


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

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

    def get(self, request):
        return api_response(data=CitySerializer(City.objects.all(), many=True).data)
