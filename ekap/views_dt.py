"""
Doğrudan temin (DT) uçları — `/api/v1/ekap/dogrudan-temin/...`.

Veri `ekap.mobil.dt` şeridinin topladığı `DogrudanTemin` tablosundan servis edilir;
yalnızca **detay** ve **doküman** talep üzerine EKAP'a gider (kullanıcı şeridi:
`EkapMobilClient(butce="kullanici")`, ayrı pencere + ayrı günlük rezerv).

Mobil entegrasyon notu: `docs/mobil-dogrudan-temin.md` · web: `docs/web-dogrudan-temin-kurul.md`.

⚠️⚠️ **Doğrulanmış arama botu EKAP'ı TETİKLEYEMEZ.** Bot hız sınırından muaftır
(`core.throttling`: 429 Google'ın tarama bütçesini düşürür). Detay ve doküman ise ilk
istekte EKAP'a gider ve mobilin belge indirmesiyle paylaşılan günlük kullanıcı
bütçesini harcar → web portalının DT sayfalarını tarayan bir bot o bütçeyi tüketip
mobil kullanıcıya "kota doldu" dedirtirdi. Bota yalnızca kayıtlı ayrıntı
verilir (içerik aynı, yalnızca canlı istek yok); doküman bota kapalıdır.

DT ayrıntısı ilk açılışta bir kez alınır ve `DogrudanTemin.detail_raw`'da kalıcı durur;
sonraki açılışlar (bot dahil) kayıttan döner.
"""
import logging
import mimetypes
import re

from django.core.cache import cache
from django.utils import timezone
from django.utils.http import content_disposition_header
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample, OpenApiParameter, extend_schema, inline_serializer,
)
from rest_framework import permissions, serializers
from rest_framework.views import APIView

from core.response import api_response
from core.throttling import istemci

from . import dt_sorgu
from .mobil import adapt
from .mobil import constants as C
from .mobil.dt import detaydan_guncelle, dtn_parcala
from .models import DogrudanTemin
from .serializers import DogrudanTeminSerializer

logger = logging.getLogger("ihaletakip")

_DOK_YOK_TTL = 24 * 3600
_DTN_PARAM = OpenApiParameter(
    name="dtn", location=OpenApiParameter.PATH, type=str, required=True,
    description="Doğrudan temin numarası.",
    examples=[OpenApiExample("DTN", value="26DT2112816")],
)
_DT_SAYFA = inline_serializer(
    name="DogrudanTeminSayfa",
    fields={
        "list": DogrudanTeminSerializer(many=True),
        "totalCount": serializers.IntegerField(),
        "page": serializers.IntegerField(),
    },
)


def _dt(dtn):
    return DogrudanTemin.objects.filter(dtn=(dtn or "").strip().upper()).first()


def _taze_enc(cli, obj):
    """
    Saklı `encID` çalışmazsa listeden tazesini alır (1 istek). Bulamazsa `None`.

    ⚠️ `dtnYil` **dört hanelidir** ("26DT…" → "2026"). Ölçüldü (2026-10-26, üretim):
    `dtnYil=26` → "Kayit bulunamadı", `dtnYil=2026` → kayıt geldi. Bulunamazsa `None`;
    çağıran hatayı kullanıcıya iletir, uydurma bir kayıt dönmez.
    """
    yil, sayi = dtn_parcala(obj.dtn)
    if not yil:
        return None
    satirlar = cli.dt_liste(cli.dt_liste_govdesi(
        dtnYil=yil, dtnSayi=sayi, topRowCnt=10, pageIndex=1,
    ))
    satir = next((s for s in satirlar if (s.get("dtn") or "").upper() == obj.dtn), None)
    if not satir or not satir.get("encID"):
        return None
    obj.enc_id = str(satir["encID"])[:128]
    obj.enc_id_at = timezone.now()
    obj.save(update_fields=["enc_id", "enc_id_at", "updated_at"])
    return obj.enc_id


def _enc_ile(cli, obj, cagir):
    """`cagir(enc_id)`; bayat encID'de **bir kez** tazesiyle tekrar dener."""
    from .mobil.client import (
        MobilButceError, MobilCaptchaError, MobilError, MobilSlotError, MobilYokError,
    )

    try:
        return cagir(obj.enc_id)
    except (MobilYokError, MobilButceError, MobilCaptchaError, MobilSlotError):
        raise
    except MobilError as e:
        logger.info("DT %s encID ile açılmadı, taze encID deneniyor: %s", obj.dtn, e)
        yeni = _taze_enc(cli, obj)
        if not yeni:
            raise
        return cagir(yeni)


def _hata_yaniti(e, *, varsayilan):
    """Mobil istemci hatası → kullanıcıya dürüst Türkçe yanıt (TenderDocumentView ile aynı)."""
    from .mobil.client import MobilButceError, MobilCaptchaError, MobilSlotError, MobilYokError

    if isinstance(e, MobilYokError):
        return api_response(message=varsayilan, success=False, status=404)
    if isinstance(e, MobilButceError):
        return api_response(message="Bugünkü EKAP sorgu kotası doldu; yarın tekrar deneyin.",
                            success=False, status=503)
    if isinstance(e, MobilCaptchaError):
        return api_response(message="EKAP doğrulama istedi ve otomatik çözülemedi; "
                                    "lütfen birazdan tekrar deneyin.",
                            success=False, status=503)
    if isinstance(e, MobilSlotError):
        return api_response(message="EKAP şu an yanıt vermiyor; lütfen birazdan tekrar deneyin.",
                            success=False, status=503)
    return None


@extend_schema(
    tags=["dogrudan-temin"],
    auth=[],
    operation_id="ekap_dogrudan_temin_list",
    summary="Açık doğrudan temin alımları",
    description=(
        "Şu an **teklif verilebilir** doğrudan temin alımları (son teklif anı geçmemiş, "
        "önceki günlerde yayımlananlar dahil). Kapananlar ve iptal edilenler listeden "
        "kendiliğinden düşer.\n\n"
        "Veri kendi toplayıcımızdan gelir: yeni ilanlar mesai içinde **5 dakikada bir** "
        "yoklanır.\n\n"
        "Yanıt: `data.list`, `data.totalCount`, `data.page`. `kalan_dakika` sunucu "
        "saatine göredir."
    ),
    parameters=[
        OpenApiParameter("q", str, description="Alım adı / idare adı (Türkçe-i güvenli).",
                         examples=[OpenApiExample("Ad", value="mazot")]),
        OpenApiParameter("il_id", str, description="İl id listesi (virgülle) — "
                         "`/ekap/cities/`'teki `ekap_il_id`, ihale filtresiyle aynı."),
        OpenApiParameter("tur", str, description="Tür listesi: 1 Mal, 2 Yapım, 3 Hizmet, "
                         "4 Danışmanlık."),
        OpenApiParameter("madde_kod", str, description="4734/22 maddesi kodları "
                         "(`/ekap/dogrudan-temin/filtreler/`)."),
        OpenApiParameter("idare_detsis", str, description="DETSIS düğümleri (virgülle); "
                         "alt birimler dahil."),
        OpenApiParameter("son_teklif_min", str, description="ISO damga ya da YYYY-MM-DD."),
        OpenApiParameter("son_teklif_max", str, description="ISO damga ya da YYYY-MM-DD "
                         "(gün verilirse gün sonu dahil)."),
        OpenApiParameter("dokuman_var", bool, description="Yalnızca dokümanı olanlar."),
        OpenApiParameter("dtn", str, description="DTN listesi (virgülle) — bildirim "
                         "derin bağlantısı (`dtn_liste`)."),
        OpenApiParameter("kayitli_filtre", int, description="DT kayıtlı filtresi id'si "
                         "(yalnızca sahibine; giriş gerekir, anonimse 400)."),
        OpenApiParameter("order", str, enum=list(dt_sorgu.SIRALAMALAR),
                         default="son_teklif",
                         description="`son_teklif`: en acil önce · `yeni`: en son yayımlanan önce."),
        OpenApiParameter("page", int, default=1),
        OpenApiParameter("page_size", int, default=20, description="En fazla 100."),
    ],
    responses={200: _DT_SAYFA},
)
class DogrudanTeminListView(APIView):
    """GET /ekap/dogrudan-temin/ — açık DT listesi."""

    permission_classes = [permissions.AllowAny]
    throttle_kapsam = "ihale_arama"

    def get(self, request):
        qp = request.query_params
        qs = dt_sorgu.acik_qs()

        kf = qp.get("kayitli_filtre")
        if kf:
            # ⚠️ Kayıtlı filtre kullanıcıya özeldir → anonim istek 400 (401 değil:
            # mobil 401'de oturumu kapatır, bir query hatası çıkış yaptırmamalı —
            # `TenderListView`'daki `kayitli_filtreler` gerekçesiyle aynı).
            if not request.user.is_authenticated:
                return api_response(message="Kayıtlı filtre için giriş yapın.",
                                    success=False, status=400)
            from tenders.models import DtKayitliFiltre

            filt = DtKayitliFiltre.objects.filter(user=request.user, pk=kf).first()
            if filt is None:
                return api_response(message="Kayıtlı filtre bulunamadı.",
                                    success=False, status=404)
            qs = dt_sorgu.filtrele(qs, filt.filters or {})

        qs = dt_sorgu.sirala(dt_sorgu.filtrele(qs, qp), qp.get("order"))

        try:
            page = max(1, int(qp.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            boyut = min(100, max(1, int(qp.get("page_size", 20))))
        except (TypeError, ValueError):
            boyut = 20
        # ⚠️ `_cached_count` KULLANILMAZ: liste 5 dk'da bir değişiyor ve küçük bir
        # tablo — 10 dk'lık sayı önbelleği, ekrandaki listeyle tutmayan bir sayı verirdi.
        toplam = qs.count()
        bas = (page - 1) * boyut
        veri = DogrudanTeminSerializer(qs[bas:bas + boyut], many=True,
                                       context={"request": request}).data
        return api_response(data={"list": veri, "totalCount": toplam, "page": page})


def _detay_ozeti(ham: dict) -> dict:
    """EKAP detay yanıtı → istemci şekli (snake_case). Duyuru HTML'i normalize edilir."""
    b = ham.get("dogrudanTeminBilgileri") or {}
    i = ham.get("idareBilgileri") or {}
    ilan = ham.get("ilanBilgileri") or {}

    def _ilanlar(anahtar, tur):
        return [{"tur": tur, "tarih": x.get("ilanTarihi") or "",
                 "html": adapt.html_normalize(x.get("ilanHTML") or "")}
                for x in (ilan.get(anahtar) or [])]

    return {
        "kapsam": b.get("dogrudanTeminKapsami") or "",
        "ilan_sekli": b.get("ilaninSekli") or "",
        "kismi_teklif": b.get("kismiTeklif") or "",
        "kisim_sayisi": b.get("kisimSayisi"),
        "istisna_dayanagi": b.get("istisnaAliminDayanagi") or "",
        "mevzuat_dayanagi": b.get("mevzuatDayanagi") or "",
        "iptal_nedeni": b.get("iptalNedeni") or "",
        "iptal_tarihi": b.get("iptalTarihi") or "",
        "idare": {"en_ust": i.get("enUstIdare") or "", "ust": i.get("ustIdare") or "",
                  "ad": i.get("idare") or "", "il": i.get("ili") or ""},
        "ilanlar": _ilanlar("dogrudanTeminIlanListesi", "dogrudan_temin")
        + _ilanlar("sonucIlanListesi", "sonuc"),
        "sozlesmeler": [
            {"istekli": s.get("istekliAdi") or "", "bedel": s.get("sozlesmeBedeli") or "",
             "tarih": s.get("sozlesmeTarihi") or ""}
            for s in ((ham.get("sozlesmeBilgileri") or {}).get("sozlesmeBilgisiList") or [])
        ],
    }


@extend_schema(
    tags=["dogrudan-temin"],
    auth=[],
    parameters=[_DTN_PARAM],
    operation_id="ekap_dogrudan_temin_detail",
    summary="Doğrudan temin detayı",
    description=(
        "Liste alanları + EKAP'tan **talep üzerine** alınan ayrıntı (`detay`): idare "
        "hiyerarşisi, duyuru metni (HTML; teklif verilecek yer, iletişim, son tarih), "
        "kısmi teklif, iptal bilgisi, sözleşmeler.\n\n"
        "Ayrıntı ilk açılışta EKAP'tan bir kez alınıp kalıcı saklanır; sonraki açılışlar "
        "kayıttan döner. EKAP o an yanıt vermezse `detay: null` + `mesaj` döner — liste "
        "alanları yine gelir.\n\n"
        "`birim_fiyat_cetveli_url`: EKAP'ın kendi adresi; mobil tarayıcıda açar (dosya "
        "sunucumuzdan geçmez). `dokuman_url`: `dokuman_var=true` ise doküman indirme ucu."
    ),
    responses={200: OpenApiTypes.OBJECT, 404: None},
)
class DogrudanTeminDetailView(APIView):
    """GET /ekap/dogrudan-temin/<dtn>/ — DB + talep üzerine EKAP ayrıntısı."""

    permission_classes = [permissions.AllowAny]
    # Canlı EKAP isteği yapabilen uç → belge uçlarıyla aynı kapsam (günlük bütçe).
    throttle_kapsam = "belge"

    def get(self, request, dtn):
        from .mobil.client import EkapMobilClient, MobilError

        obj = _dt(dtn)
        if obj is None:
            return api_response(message="Doğrudan temin bulunamadı.", success=False, status=404)

        # ⚠️ Ayrıntı bir kez alınır ve `detail_raw`'da KALICI saklanır; sonraki her açılış
        # kayıttan döner, EKAP'a gidilmez (ürün kararı 2026-10-29: DT ayrıntısı yayımdan
        # sonra değişmiyor, 30 dk'lık önbellek her açılışta kullanıcı bütçesini yakıyordu).
        # Durum değişimi (iptal/sonuç) tam taramanın doğrulama adımıyla `detail_raw`'a yazılır.
        detay = _detay_ozeti(obj.detail_raw) if isinstance(obj.detail_raw, dict) else None
        mesaj = ""
        if detay is None:
            if istemci(request).bot:
                # Bot canlı istek tetiklemez (modül notu). İçerik farkı değil, bütçe koruması.
                mesaj = "Ayrıntı henüz alınmadı."
            elif obj.enc_id:
                cli = EkapMobilClient(butce="kullanici")
                try:
                    ham = _enc_ile(cli, obj, cli.dt_detay)
                    detay = _detay_ozeti(ham)
                    # ⚠️ Yalnızca KESİN cevap kaydedilir ("bilmiyorum" ≠ "yok").
                    detaydan_guncelle(obj, ham)
                except MobilError as e:
                    logger.warning("DT detayı alınamadı (%s): %s", obj.dtn, str(e)[:150])
                    mesaj = "EKAP'tan ayrıntı şu an alınamadı; lütfen birazdan tekrar deneyin."
            else:
                mesaj = "Bu kayıt için EKAP ayrıntısı henüz alınamıyor."

        veri = DogrudanTeminSerializer(obj, context={"request": request}).data
        veri["detay"] = detay
        veri["birim_fiyat_cetveli_url"] = (
            C.DT_BIRIM_FIYAT_URL.format(enc_id=obj.enc_id) if obj.enc_id else None
        )
        veri["mesaj"] = mesaj
        return api_response(data=veri)


@extend_schema(
    tags=["dogrudan-temin"],
    auth=[],
    operation_id="ekap_dogrudan_temin_filtreler",
    summary="Doğrudan temin filtre seçenekleri",
    description="Tür, 4734/22 maddesi ve sıralama seçenekleri (EKAP `FiltreEnumlari`).",
    responses={200: OpenApiTypes.OBJECT},
)
class DogrudanTeminFiltrelerView(APIView):
    """GET /ekap/dogrudan-temin/filtreler/ — sabit seçenekler (koddan, EKAP'a gitmez)."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return api_response(data={
            "turler": [{"kod": k, "ad": v} for k, v in C.DT_TUR.items()],
            "maddeler": [{"kod": k, "ad": v} for k, v in C.DT_MADDE.items()],
            "siralamalar": [{"kod": "son_teklif", "ad": "Son teklif tarihi (en yakın)"},
                            {"kod": "yeni", "ad": "En yeni ilan"}],
        })


_DOSYA_ADI = re.compile(r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""", re.I)
_MIME_EK = {".rar": "application/vnd.rar", ".7z": "application/x-7z-compressed"}


def _dosya_adi(resp, dtn):
    """
    `content-disposition`'dan dosya adı (ölçüldü: `filename="25DT2095274_DT_dokumani_…rar"`).
    Yol ayırıcıları ve tırnaklar atılır; yoksa `<dtn>_dokuman`.
    """
    from urllib.parse import unquote

    m = _DOSYA_ADI.search(resp.headers.get("content-disposition") or "")
    ad = unquote(m.group(1)).strip() if m else ""
    ad = re.sub(r'[\\/"\r\n]+', "_", ad).strip(" ._")
    return ad or f"{dtn}_dokuman"


def _icerik_tipi(ad, resp):
    ek = ("." + ad.rsplit(".", 1)[-1].lower()) if "." in ad else ""
    return (_MIME_EK.get(ek) or mimetypes.guess_type(ad)[0]
            or resp.headers.get("content-type") or "application/octet-stream")


@extend_schema(
    tags=["dogrudan-temin"],
    auth=[],
    parameters=[_DTN_PARAM],
    operation_id="ekap_dogrudan_temin_dokuman",
    summary="Doğrudan temin dokümanını indir",
    description=(
        "DT dokümanını EKAP'tan **akış (streaming)** olarak aktarır. Tek dosyadır, "
        "biçimi idarenin yüklediği dosyadır (RAR/ZIP/PDF/DOCX…); ad ve tür "
        "`Content-Disposition` / `Content-Type` başlıklarındadır.\n\n"
        "`dokuman_var=false` kayıtta EKAP'a gidilmeden **404** döner. Kota/doğrulama/"
        "EKAP yanıtsızlığında **503** + Türkçe mesaj."
    ),
    responses={200: OpenApiTypes.BINARY, 404: None, 502: None, 503: None},
)
class DogrudanTeminDokumanView(APIView):
    """GET /ekap/dogrudan-temin/<dtn>/dokuman/ — mobil API'den akış indirme."""

    permission_classes = [permissions.AllowAny]
    throttle_kapsam = "belge"

    def get(self, request, dtn):
        from django.http import StreamingHttpResponse

        from .mobil.client import EkapMobilClient, MobilError

        yok = "Bu doğrudan temin için EKAP'ta yayımlanmış doküman yok."
        if istemci(request).bot:
            return api_response(message="Doküman indirme bu istemciye kapalı.",
                                success=False, status=403)
        obj = _dt(dtn)
        if obj is None:
            return api_response(message="Doğrudan temin bulunamadı.", success=False, status=404)
        if not obj.dokuman_var or not obj.enc_id:
            return api_response(message=yok, success=False, status=404)
        yok_anahtar = f"dt:dokyok:{obj.dtn}"
        if cache.get(yok_anahtar):
            return api_response(message=yok, success=False, status=404)

        cli = EkapMobilClient(butce="kullanici")
        try:
            resp = _enc_ile(cli, obj, cli.dt_dokuman_indir)
        except MobilError as e:
            from .mobil.client import MobilYokError

            if isinstance(e, MobilYokError):
                # Kesin cevap → 24 sa hatırlanır; geçici hatalar hatırlanmaz.
                cache.set(yok_anahtar, 1, timeout=_DOK_YOK_TTL)
            yanit = _hata_yaniti(e, varsayilan=yok)
            if yanit is not None:
                return yanit
            logger.warning("DT dokümanı indirilemedi (%s): %s", obj.dtn, e)
            return api_response(message="Belge indirilemedi.", success=False, status=502)

        ad = _dosya_adi(resp, obj.dtn)
        cikti = StreamingHttpResponse(resp.iter_content(chunk_size=64 * 1024),
                                      content_type=_icerik_tipi(ad, resp))
        cikti["Content-Disposition"] = content_disposition_header(True, ad)
        if resp.headers.get("content-length"):
            cikti["Content-Length"] = resp.headers["content-length"]
        return cikti
