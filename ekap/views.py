"""
ekap servis view'ları — /api/v1/ekap/...

Hepsi kendi DB'mizden okur (hızlı, rate-limit yok). Global zarf (core renderer)
otomatik uygulanır. Detay/belge-url için gerekirse EKAP'a canlı düşülür.
"""
import hashlib
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    inline_serializer,
)
from rest_framework import permissions, serializers
from rest_framework.views import APIView

from accounts.premium import (
    MSG_IDARE_PROFIL,
    MSG_PRO_FILTRE,
    MSG_TEKRAR,
    require_premium,
)
from core.response import api_response

from . import authority_profile, benchmark as benchmark_mod

from .constants import CITIES, DURUM_IPTAL, IHALE_TURU, OZELLIK_MAP
from .detsis_tree import annotate_paths, descendant_idare_ids, tender_idare_id_set
from .models import (
    RecurringTenderSeries,
    Announcement,
    Authority,
    City,
    Contract,
    Contractor,
    OkasCode,
    OkasItem,
    Tender,
)
from .serializers import (
    AuthorityNodeSerializer,
    CitySerializer,
    ContractorListSerializer,
    ContractSerializer,
    EkapAnnouncementSerializer,
    EkapTenderListSerializer,
    OkasCodeSerializer,
    TenderContractSerializer,
    _dec,
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


_TRUE = {"1", "true", "evet", "yes", "on"}
_FALSE = {"0", "false", "hayir", "hayır", "no", "off"}


def _as_bool(value):
    """
    Üç değerli çözümleme: `True` / `False` / `None` (parametre verilmemiş ya da anlamsız).

    ⚠️ `None` ile `False` **ayrı** tutulmalı: `sonuclanmis=false` "sözleşmesi olmayanlar"
    demek, parametrenin hiç gelmemesi ise "filtreleme" demek. `bool(value)` kullanılsaydı
    ikisi aynı davranırdı.
    """
    if value is None or isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def _as_decimal(value):
    """Sayısal aralık parametresi → `Decimal`, çözümlenemezse `None` (filtre uygulanmaz)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


# `EkapTenderListSerializer`'ın okuduğu alanlar + sıralama alanları. `.only()` bununla
# daraltılır: `Tender.detail_raw`/`list_raw` (~40 KB/satır JSONB) liste yanıtında HİÇ
# kullanılmıyor ama `SELECT *` her satırda onları TOAST'tan açıp `json.loads` ediyordu.
# ⚠️ Serializer'a yeni alan eklerseniz buraya da ekleyin, yoksa deferred alan başına
# ek sorgu atılır (sessiz N+1).
_LIST_FIELDS = (
    "ekap_id",
    "ikn",
    "ihale_adi",
    "idare_adi",
    "ihale_il_adi",
    "ihale_tarih_saat",
    "ihale_tip",
    "ihale_tipi_aciklama",
    "ihale_usul_aciklama",
    "ihale_durum",
    "ihale_durum_aciklama",
    "dokuman_sayisi",
    "ilan_var_mi",
    "ihale_tarihi",  # sıralama
    "ilan_tarihi",  # sıralama
)

# `Tender`'ın ağır JSONB sütunları (~40 KB/satır). `select_related("tender")` yapan
# sözleşme uçlarında bunlar HİÇ kullanılmıyor ama `SELECT *` her satırda TOAST'tan açıp
# `json.loads` ediyordu (`page_size=20` → istek başına ~800 KB boşuna I/O).
# ⚠️ Burada `.only()` DEĞİL `.defer()` kullanılır: `ContractSerializer` ~20 `Contract`
# alanı + 9 `Tender` alanı okuyor; allow-list tutulsaydı serializer'a eklenen her alan
# sessiz N+1 doğururdu (`_LIST_FIELDS`'in yaşadığı risk). Deny-list kendini korur.
_TENDER_BLOB_FIELDS = ("tender__detail_raw", "tender__list_raw")

# ── Pro'ya kilitli arama parametreleri ────────────────────────────────────────
# Bunlardan biri istekte varsa `TenderListView` 403 `premium_required` döner. Temel
# arama (q, il, tür, tarih, OKAS, idare…) herkese açık kalır.
# ⚠️ Bu küme `_COUNT_IGNORED_PARAMS` ile KARIŞTIRILMAMALI: oradaki liste cache
# anahtarından DIŞLANANLARDIR; buraya yazılanlar sonucu değiştiren gerçek filtrelerdir
# ve cache anahtarına GİRMELİDİR.
_PRO_PARAMS = frozenset({
    "yaklasik_maliyet_min", "yaklasik_maliyet_max",
    "sozlesme_bedeli_min", "sozlesme_bedeli_max",
    "istekli_sayisi_min", "istekli_sayisi_max",
    "teklif_sayisi_min", "teklif_sayisi_max",
    "indirim_orani_min", "indirim_orani_max",
    "sonuclanmis", "iptal", "e_ihale",
    "itirazen_sikayet_var", "idareye_sikayet_var", "sikayet_dilekce_var",
    "fiyat_disi_unsur_var", "e_eksiltme_yapilacak", "duzeltme_ilani_var",
    "kismi_ihale", "ilansiz_mi",
    "okas_ana_kod", "en_ust_idare_kod", "seri_anahtar",
})

# Kapsamı kısmi olan kolonlarda aralık filtresi kullanılınca istemciye dönen uyarı.
# NULL hiçbir aralık koşuluna girmez → değeri bilinmeyen ihaleler sessizce elenir.
_UYARI_YM = (
    "Yaklaşık maliyet yalnızca Sonuç İlanı yayımlanmış ihalelerde bilinir; "
    "bu filtre maliyeti bilinmeyen ihaleleri kapsam dışı bırakır."
)
_UYARI_TEKLIF = (
    "Teklif sayısı yalnızca Sonuç İlanı ayrıştırılabilen ihalelerde bilinir; "
    "bu filtre teklif sayısı bilinmeyen ihaleleri kapsam dışı bırakır."
)
_UYARI_ISTEKLI = (
    "İstekli sayısı ihale değerlendirmesi bittikten sonra oluşur; "
    "bu filtre henüz sonuçlanmamış ihaleleri kapsam dışı bırakır."
)
_KISMI_KAPSAM_UYARI = {
    "yaklasik_maliyet_min": _UYARI_YM, "yaklasik_maliyet_max": _UYARI_YM,
    "indirim_orani_min": _UYARI_YM, "indirim_orani_max": _UYARI_YM,
    "teklif_sayisi_min": _UYARI_TEKLIF, "teklif_sayisi_max": _UYARI_TEKLIF,
    "istekli_sayisi_min": _UYARI_ISTEKLI, "istekli_sayisi_max": _UYARI_ISTEKLI,
}

# Sayfalama/sıralama COUNT sonucunu değiştirmez → cache anahtarından dışlanır.
_COUNT_IGNORED_PARAMS = frozenset({"page", "page_size", "order", "siralamaTipi", "format"})
# COUNT soğukken pahalıdır (500k satır + GIN indeksleri; ölçüm: 3 GB'lık makinede
# buffer cache boşken 15 sn'ye kadar). `totalCount` bir ilerleme göstergesidir ve arşiv
# yavaş değişir → uzun TTL güvenli, soğuk yola düşme sıklığını doğrudan azaltır.
# Ayarla oynanabilsin diye env'den okunur (deploy gerektirmez).
_COUNT_TTL = getattr(settings, "SEARCH_COUNT_CACHE_TTL", 600)


def _cached_count(qs, params, scope="tender"):
    """
    Toplam sonuç sayısı — filtre imzasına göre kısa süreli cache'li.

    `COUNT(*)` filtreli 500k satırda tam tarama demek ve **her sayfa isteğinde** yeniden
    hesaplanıyordu; oysa aynı aramanın 2., 3., 4. sayfası aynı sayıyı ister. Anahtar
    yalnızca **filtre** parametrelerinden üretilir (sayfalama/sıralama dışlanır) → sayfa
    gezinmesi tek COUNT'a iner. Bayatlık kabul edilebilir: totalCount bir ilerleme
    göstergesidir, sayfa içeriği her zaman canlı sorgudan gelir (bkz. `_COUNT_TTL`).

    `scope` farklı uçların anahtarlarını ayırır (aynı query string'le çağrılan firma
    listesi ile sözleşme listesi aynı sayıyı paylaşmasın).

    ⚠️ Yok sayılan parametreler **deny-list**tir (allow-list DEĞİL): yeni bir filtre
    eklenip buraya yazılmayı unutulursa en kötü ihtimalle cache ıskalanır. Allow-list
    olsaydı iki FARKLI arama aynı anahtara düşüp **yanlış** sayı dönerdi.
    """
    sig = sorted(
        (k, ",".join(sorted(params.getlist(k))) if hasattr(params, "getlist") else str(params[k]))
        for k in params
        if k not in _COUNT_IGNORED_PARAMS
    )
    key = "cnt:" + hashlib.sha1(f"{scope}|{sig!r}".encode()).hexdigest()
    total = cache.get(key)
    if total is None:
        # `.order_by()`: COUNT'ta ORDER BY anlamsız ama planner'ı gereksiz sort'a itebilir.
        total = qs.order_by().count()
        cache.set(key, total, _COUNT_TTL)
    return total


def _tender_by_key(key, *, defer_raw=False):
    """
    İhaleyi `ikn` ya da `ekap_id` ile bulur (detay/ilan/sözleşme uçlarının ortak girişi).

    `defer_raw=True` → `detail_raw`/`list_raw` (~40 KB×2) çekilmez. İlan ve sözleşme
    uçları ihaleyi yalnızca **FK filtresi** olarak kullanıyor; ham JSONB'yi okumak
    boşuna TOAST I/O'su. Detay ucu tersine onlara muhtaç → varsayılan `False`.

    ⚠️ **`.order_by()` ŞART — kaldırmayın.** `Tender.Meta.ordering = ["-ihale_tarihi"]`
    olduğu için `.first()` sorguya `ORDER BY ihale_tarihi DESC LIMIT 1` ekler; planlayıcı
    bunu görünce `ikn`/`ekap_id` unique indekslerini bırakıp **tarih indeksini geriye
    doğru tarayıp filtrelemeyi** seçebiliyor. Yalnızca 1 satır eşleştiği için bu, 500k
    satırın taranması demektir (aynı plan tuzağı `q` aramasında da görüldü). Sıralamayı
    düşürünce unique indeksler kullanılır — zaten en çok 1 satır döner, sıralamanın
    anlamı da yoktur.
    """
    qs = Tender.objects.filter(Q(ikn=key) | Q(ekap_id=key)).order_by()
    if defer_raw:
        qs = qs.defer("detail_raw", "list_raw")
    return qs.first()


def _okas_exists(cond):
    """
    İhalenin OKAS kalemlerinde `cond`'a uyan **en az bir** satır var mı (semi-join).
    `cond` alanları `OkasItem` alanlarıdır (`kodu`, `adi`) — `okas_kalemleri__` öneki YOK.
    Bkz. `apply_tender_filters` docstring'i: JOIN+`DISTINCT` yerine `Exists` kullanılır.
    """
    return Exists(OkasItem.objects.filter(cond, tender_id=OuterRef("pk")))


def _contract_exists(cond):
    """
    İhalenin sözleşmelerinde `cond`'a uyan **en az bir** satır var mı (semi-join).
    `cond` alanları `Contract` alanlarıdır (`yuklenici`, `yuklenici_adi_norm`, ...) —
    `sozlesmeler__` öneki YOK.
    """
    return Exists(Contract.objects.filter(cond, tender_id=OuterRef("pk")))


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

    ⚠️ **Çoklu-satır ilişkilerde `.filter(ilişki__…)` DEĞİL `Exists()` kullanılır.**
    OKAS kalemi ve sözleşme birden çok satır olduğu için JOIN'li filtre ihaleyi çoğaltır
    ve eskiden `.distinct()` ile toplanırdı. `DISTINCT` **`LIMIT`'ten önce** çalıştığı
    için sayfa boyutu maliyeti kurtarmıyordu: Postgres eşleşen TÜM satırları (üstelik
    `detail_raw`/`list_raw` JSONB'leriyle) sort/hash'ten geçirmek zorundaydı. `Exists`
    semi-join'i satır çoğalmasını hiç doğurmaz → `DISTINCT` gerekmez, ilk N satır bulunca
    durabilir. Yeni bir çoklu-satır filtresi eklerken aynı deseni izleyin.
    """

    # ── Anahtar kelime (ihale adı + kurum adı + İKN) — mobil "Anahtar Kelime" kutusu ──
    # İhale adı/kurum adı **Türkçe-i güvenli** normalize sütunlar üzerinden aranır
    # (bkz. normalize_tr).
    #
    # ⚠️ İKN'de `icontains` DEĞİL `contains` — kritik. Django Postgres'te `icontains`'i
    # `UPPER(ikn::text) LIKE UPPER(%s)` diye derler; kolonun üstündeki `UPPER()` yüzünden
    # `ikn` üzerindeki trigram indeksi KULLANILAMAZ. Üstelik bu üç koşul OR'lu: Postgres
    # BitmapOr'u ancak **her dal** indekslenebiliyorsa kurar → tek indekssiz dal, diğer
    # iki trigram indeksini de devre dışı bırakıp tüm tabloyu taratırdı. İKN saf ASCII
    # (rakam + `/`) olduğu için `contains` ≡ `icontains`, davranış birebir aynı.
    q = params.get("q")
    if q and str(q).strip():
        nq = normalize_tr(q)
        qs = qs.filter(
            Q(ihale_adi_norm__contains=nq) | Q(idare_adi_norm__contains=nq) | Q(ikn__contains=str(q).strip())
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
        qs = qs.filter(ikn__contains=str(ikn).strip())  # ⚠️ icontains DEĞİL — bkz. `q` notu

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
            expanded &= tender_idare_id_set()
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
            cond |= Q(kodu__startswith=kod)
        qs = qs.filter(_okas_exists(cond))
    okas_adi = _as_str_list(params.get("okas_adi"))
    if okas_adi:
        cond = Q()
        for adi in okas_adi:
            cond |= Q(adi__icontains=adi)
        qs = qs.filter(_okas_exists(cond))

    # ── Yüklenici (sözleşme imzalayan firma) ──
    # `ortakliklari_dahil_et` (vars. açık): firma bir ORTAK GİRİŞİMİN üyesi olarak da
    # iş almış olabilir → "hangi firma hangi işi aldı" semantiği bunu kapsamalı.
    ortakliklar_dahil = str(
        params.get("ortakliklari_dahil_et", "true")
    ).lower() not in ("0", "false", "no", "hayir")

    yuklenici_id = _as_int_list(params.get("yuklenici_id"))
    if yuklenici_id:
        cond = Q(yuklenici_id__in=yuklenici_id)
        if ortakliklar_dahil:
            cond |= Q(yuklenici__uyelikler__uye_id__in=yuklenici_id)
        qs = qs.filter(_contract_exists(cond))

    yuklenici = params.get("yuklenici")
    if yuklenici and str(yuklenici).strip():
        nq = normalize_tr(yuklenici)
        # ⚠️ **OR'un iki dalını tek `Exists` içinde BİRLEŞTİRMEYİN** — ölçülmüş tuzak.
        # `arama_norm` `ekap_contractor`'da, `yuklenici_adi_norm` `ekap_contract`'ta;
        # OR farklı tablolara yayılınca Postgres hiçbir indeks kullanamaz, önce 840k
        # sözleşmeyi 94k firmayla join edip SONRA filtreler (ölçüm: ~1.2 sn, 79 satır
        # bulmak için 840k satır tarama). Aynı dersin ikinci kez öğrenildiği yer: bkz.
        # `q` filtresindeki BitmapOr notu.
        #
        # Çözüm: iki dalı **ayrı ayrı indeksten** çözüp `UNION` ile birleştir. Her dal
        # kendi trigram indeksini kullanır (ölçüm: 222 ms, ~5 kat).
        # ⚠️ `pk__in=A|pk__in=B` (iki ayrı IN'i OR'lamak) DENENDİ ve **çok daha kötüydü**
        # (109 sn): planlayıcı ihale başına korelasyonlu tarama seçiyor. UNION kalsın.
        by_ad = Contract.objects.filter(yuklenici_adi_norm__contains=nq).values("tender_id")
        by_firma = Contract.objects.filter(yuklenici__arama_norm__contains=nq).values("tender_id")
        qs = qs.filter(pk__in=by_ad.union(by_firma))

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

    # ── Pro filtreler ─────────────────────────────────────────────────────────
    # Uçta `_PRO_PARAMS` kapısı bunları Pro'ya kilitler (bkz. TenderListView). Burada
    # koşulsuz uygulanır çünkü `apply_tender_filters` bildirim görevlerinde de kullanılıyor
    # ve orada premium kontrolü zaten görev içinde yapılıyor (tenders/tasks.py).
    #
    # ⚠️ Hepsi **AND** ile bağlanır → yeni OR dalı YOK, dolayısıyla "OR'un her dalı
    # indekslenebilir ve aynı tabloda olmalı" kuralı kendiliğinden sağlanır. Buraya bir OR
    # eklerseniz her iki dal da indeksli `Tender` kolonu olmalı; değilse `yuklenici`
    # filtresindeki UNION desenini kullanın.

    # Sayısal aralıklar — hepsi tek değerli `Tender` kolonu (Exists gerekmez).
    # `sozlesme_bedeli`/`sonuclanmis` `Contract` semi-join'i yerine denormalize
    # `Tender.sozlesme_sayisi`/`toplam_sozlesme_bedeli` kullanır: korelasyonlu alt sorgu
    # yerine düz indekslenebilir yordam (bkz. models.py'deki gerekçe).
    for param, field in (
        ("yaklasik_maliyet", "yaklasik_maliyet_num"),
        ("sozlesme_bedeli", "toplam_sozlesme_bedeli"),
        ("istekli_sayisi", "istekli_sayisi"),
    ):
        mn = _as_decimal(params.get(f"{param}_min"))
        if mn is not None:
            qs = qs.filter(**{f"{field}__gte": mn})
        mx = _as_decimal(params.get(f"{param}_max"))
        if mx is not None:
            qs = qs.filter(**{f"{field}__lte": mx})

    # Rekabet/indirim aralıkları `Contract`'ta → semi-join (satır çoğaltmaz).
    for param, field in (
        ("teklif_sayisi", "teklif_sayisi"),
        ("indirim_orani", "indirim_orani"),
    ):
        mn = _as_decimal(params.get(f"{param}_min"))
        if mn is not None:
            qs = qs.filter(_contract_exists(Q(**{f"{field}__gte": mn})))
        mx = _as_decimal(params.get(f"{param}_max"))
        if mx is not None:
            qs = qs.filter(_contract_exists(Q(**{f"{field}__lte": mx})))

    # Sonuçlanma / iptal
    sonuclanmis = _as_bool(params.get("sonuclanmis"))
    if sonuclanmis is True:
        qs = qs.filter(sozlesme_sayisi__gt=0)
    elif sonuclanmis is False:
        qs = qs.filter(sozlesme_sayisi=0)
    iptal = _as_bool(params.get("iptal"))
    if iptal is True:
        qs = qs.filter(ihale_durum__in=sorted(DURUM_IPTAL))
    elif iptal is False:
        qs = qs.exclude(ihale_durum__in=sorted(DURUM_IPTAL))

    # e-ihale: ⚠️ `Tender.e_ihale` boolean'ı DEĞİL, mevcut JSONB etiketi kullanılır.
    # O kolon indekssiz ve tutarsız doluyor (`sync.py` yalnızca payload'da varsa yazıyor);
    # `ozellikler` ise GIN indeksli ve liste senkronundan itibaren dolu.
    e_ihale = _as_bool(params.get("e_ihale"))
    if e_ihale is True:
        qs = qs.filter(ozellikler__contains=[OZELLIK_MAP["eIhale"]])
    elif e_ihale is False:
        qs = qs.exclude(ozellikler__contains=[OZELLIK_MAP["eIhale"]])

    # Üç değerli sinyal bayrakları. ⚠️ `False` istendiğinde `exclude(True)` DEĞİL
    # `filter(False)` kullanılır: `exclude` NULL'ları (detayı gelmemiş ihaleler) da
    # toplardı ve "itirazsız ihaleler" listesi bilinmeyenlerle şişerdi.
    for param in (
        "itirazen_sikayet_var", "idareye_sikayet_var", "sikayet_dilekce_var",
        "fiyat_disi_unsur_var", "e_eksiltme_yapilacak", "duzeltme_ilani_var",
        "kismi_ihale", "ilansiz_mi",
    ):
        val = _as_bool(params.get(param))
        if val is not None:
            qs = qs.filter(**{param: val})

    # Kategori / bakanlık (Adım 1 kolonları)
    okas_ana = _as_str_list(params.get("okas_ana_kod"))
    if okas_ana:
        qs = qs.filter(okas_ana_kod__in=okas_ana)
    bakanlik = _as_str_list(params.get("en_ust_idare_kod"))
    if bakanlik:
        qs = qs.filter(en_ust_idare_kod__in=bakanlik)
    seri = params.get("seri_anahtar")
    if seri and str(seri).strip():
        qs = qs.filter(seri_anahtar=str(seri).strip())

    return qs


# Tarih parametreleri `DD.MM.YYYY [HH:mm]` veya ISO-8601 kabul eder (bkz. utils.parse_ekap_datetime).
# Çözümlenemeyen tarih **sessizce yok sayılır** — filtre uygulanmaz.
_DATE_HINT = "`GG.AA.YYYY`, `GG.AA.YYYY SS:dd` veya ISO-8601. Geçersiz tarih yok sayılır."

# Pro filtrelerin OpenAPI/Postman tanımı. ⚠️ `_PRO_PARAMS` ile aynı adları taşımalı —
# biri güncellenip diğeri unutulursa uç ya belgelenmemiş bir filtre kabul eder ya da
# belgelenen bir filtre 403 vermez.
_PRO_ETIKET = "🔒 **Pro** — bu parametre kullanılırsa Free/anonim istek `403 premium_required` döner."
_PRO_SCHEMA_PARAMS = [
    OpenApiParameter("yaklasik_maliyet_min", str,
                     description=f"Yaklaşık maliyet alt sınırı (TL). {_PRO_ETIKET} "
                                 "Yalnızca Sonuç İlanı olan ihalelerde bilinir → değeri "
                                 "bilinmeyenler kapsam dışı kalır (yanıt `uyari` taşır)."),
    OpenApiParameter("yaklasik_maliyet_max", str, description=f"Yaklaşık maliyet üst sınırı (TL). {_PRO_ETIKET}"),
    OpenApiParameter("sozlesme_bedeli_min", str,
                     description=f"İhalenin toplam sözleşme bedeli alt sınırı (TL). {_PRO_ETIKET}"),
    OpenApiParameter("sozlesme_bedeli_max", str, description=f"Toplam sözleşme bedeli üst sınırı (TL). {_PRO_ETIKET}"),
    OpenApiParameter("teklif_sayisi_min", int,
                     description=f"Rekabet: en az bu kadar teklif verilmiş ihaleler. {_PRO_ETIKET}"),
    OpenApiParameter("teklif_sayisi_max", int, description=f"En çok bu kadar teklif. {_PRO_ETIKET}"),
    OpenApiParameter("istekli_sayisi_min", int,
                     description=f"Katılan istekli sayısı alt sınırı. {_PRO_ETIKET} "
                                 "İstekli sayısı değerlendirme bitince oluşur → açık "
                                 "ihaleler kapsam dışı kalır."),
    OpenApiParameter("istekli_sayisi_max", int, description=f"İstekli sayısı üst sınırı. {_PRO_ETIKET}"),
    OpenApiParameter("indirim_orani_min", str,
                     description=f"İndirim oranı alt sınırı (**oran**, yüzde değil: `0.20` = %20). {_PRO_ETIKET}"),
    OpenApiParameter("indirim_orani_max", str, description=f"İndirim oranı üst sınırı (oran). {_PRO_ETIKET}"),
    OpenApiParameter("sonuclanmis", bool,
                     description=f"`true` → sözleşmesi olan ihaleler, `false` → olmayanlar. {_PRO_ETIKET}"),
    OpenApiParameter("iptal", bool, description=f"İptal edilmiş ihaleler (durum 6/10). {_PRO_ETIKET}"),
    OpenApiParameter("e_ihale", bool, description=f"Elektronik ihale. {_PRO_ETIKET}"),
    OpenApiParameter("fiyat_disi_unsur_var", bool,
                     description="Fiyat dışı unsur içeren ihaleler — en düşük fiyat tek "
                                 f"başına kazandırmaz. {_PRO_ETIKET}"),
    OpenApiParameter("itirazen_sikayet_var", bool, description=f"İtirazen şikâyet başvurusu olanlar. {_PRO_ETIKET}"),
    OpenApiParameter("idareye_sikayet_var", bool, description=f"İdareye şikâyet başvurusu olanlar. {_PRO_ETIKET}"),
    OpenApiParameter("sikayet_dilekce_var", bool, description=f"Şikâyet dilekçesi olanlar. {_PRO_ETIKET}"),
    OpenApiParameter("e_eksiltme_yapilacak", bool, description=f"Elektronik eksiltme yapılacak ihaleler. {_PRO_ETIKET}"),
    OpenApiParameter("duzeltme_ilani_var", bool, description=f"Düzeltme ilanı bulunanlar. {_PRO_ETIKET}"),
    OpenApiParameter("kismi_ihale", bool, description=f"Kısımlı ihaleler. {_PRO_ETIKET}"),
    OpenApiParameter("ilansiz_mi", bool, description=f"İlansız ihaleler. {_PRO_ETIKET}"),
    OpenApiParameter("okas_ana_kod", str,
                     description=f"Birincil OKAS kodu (virgülle çoklu). {_PRO_ETIKET} "
                                 "Alt kalemleri de kapsayan arama için `okas_kod` kullanın."),
    OpenApiParameter("en_ust_idare_kod", str,
                     description=f"Bakanlık/üst kurum kodu (virgülle çoklu). {_PRO_ETIKET}",
                     examples=[OpenApiExample("Sağlık Bakanlığı", value="15")]),
    OpenApiParameter("seri_anahtar", str,
                     description="Tekrar eden ihale serisi anahtarı — aynı idarenin yıldan "
                                 f"yıla tekrarladığı işler. {_PRO_ETIKET}"),
]

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
        # ── Pro filtreler (403 premium_required) ──────────────────────────────
        *_PRO_SCHEMA_PARAMS,
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

        # ── Pro kapısı ────────────────────────────────────────────────────────
        # Uç `AllowAny` KALIR: temel arama herkese açık. Yalnızca gelişmiş parametreler
        # Pro'ya kilitlidir. Anonim kullanıcıda `is_premium` False → 403.
        # ⚠️ Parametreyi sessizce yok saymak YANLIŞ olurdu: kullanıcının istediğinden
        # DAHA FAZLA sonuç dönerdi — limit kılığına girmiş bir doğruluk hatası. 403 +
        # `errors.code=premium_required` ise mobilin zaten işlediği sözleşme.
        kullanilan_pro = _PRO_PARAMS & set(qp)
        if kullanilan_pro:
            require_premium(request.user, MSG_PRO_FILTRE)

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

        total = _cached_count(qs, qp)
        start = (page - 1) * page_size
        # ⚠️ `.only()` filtrelerden SONRA — `apply_tender_filters` başka alanlara bakabilir.
        items = qs.only(*_LIST_FIELDS)[start:start + page_size]
        data = EkapTenderListSerializer(items, many=True).data
        payload = {"list": data, "totalCount": total, "page": page}

        # Dürüstlük uyarısı: kapsamı kısmi olan kolonlarda aralık filtresi, değeri
        # BİLİNMEYEN ihaleleri de sessizce eler (NULL hiçbir aralığa girmez). Kullanıcı
        # "sonuç yok" ile "veri yok"u ayırt edebilmeli.
        uyarilar = [
            _KISMI_KAPSAM_UYARI[p] for p in _KISMI_KAPSAM_UYARI if p in qp
        ]
        if uyarilar:
            payload["uyari"] = " ".join(dict.fromkeys(uyarilar))
        return api_response(data=payload)


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
def _attach_idare_detsis(data, tender=None):
    """İhale detayının `idare` bloğuna `detsis_no` ekler (favori idare için gerekli).

    Ham EKAP detayı yalnızca `idare_id` taşır; favori idare uçları ise `detsis_no` ile
    anahtarlanır. `ekap.Authority` üzerinden idare_id → detsis_no çözer; eşleşme yoksa
    `None` yazar (mobil buna göre "İdareyi Kaydet"i pasife alır).

    idare_id kaynağı (ilki dolu olan): `idare.id` → `data.idareId` → `ihaleBilgi.idareId`
    → `tender.idare_id` (kolon). Ham blokta id bazen boş gelir; kolon güvenilirdir.
    """
    if not isinstance(data, dict):
        return data
    idare = data.get("idare")
    if not isinstance(idare, dict) or idare.get("detsis_no"):
        return data
    bilgi = data.get("ihaleBilgi") or {}
    idare_id = (
        idare.get("id")
        or data.get("idareId")
        or bilgi.get("idareId")
        or (tender.idare_id if tender else None)
    )
    if idare_id in (None, ""):
        return data
    idare["detsis_no"] = (
        Authority.objects.filter(idare_id=str(idare_id))
        .values_list("detsis_no", flat=True)
        .first()
    )
    return data


def _unwrap_item(raw):
    return raw.get("item", raw) if isinstance(raw, dict) else raw


class TenderDetailView(APIView):
    """GET /ekap/tenders/{key}/ — İKN veya ekap_id ile detay (EKAP şeklinde)."""

    permission_classes = [permissions.AllowAny]  # ihale tarama girişsiz

    def get(self, request, key):
        tender = _tender_by_key(key)

        # DB'de var ve detayı çekilmiş → saklanan ham detayı (item açılmış) dön
        if tender and tender.detail_synced_at and tender.detail_raw:
            # Bayatsa arka planda yenile, eldekini dön
            self._maybe_refresh(tender)
            data = _unwrap_item(tender.detail_raw)
            return api_response(data=_attach_idare_detsis(data, tender))

        # Detay yoksa → canlı çek (lazy sync)
        ekap_id = tender.ekap_id if tender else key
        try:
            from .client import EkapV2Client
            from . import sync as sync_mod

            client = EkapV2Client()
            detail = client.get_detail(ekap_id)
            sync_mod.upsert_tender_detail(ekap_id, detail)
            return api_response(data=_attach_idare_detsis(_unwrap_item(detail)))
        except Exception as e:
            logger.warning("Canlı detay çekilemedi (%s): %s", key, e)
            if tender:
                data = _attach_idare_detsis(_unwrap_item(tender.detail_raw or {}), tender)
                return api_response(data=data, message="Detay güncel değil.")
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
        tender = _tender_by_key(key, defer_raw=True)  # yalnızca FK filtresi olarak lazım
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
            tender_ids = tender_idare_id_set()
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


# ── Yüklenici (firma) uçları ───────────────────────────
def _paginate(request, qs, serializer_class, default_page_size=20, context=None):
    """assistant.views._paginate ile aynı zarf: {list, totalCount, page}."""
    qp = request.query_params
    try:
        page = max(1, int(qp.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(100, max(1, int(qp.get("page_size", default_page_size))))
    except (TypeError, ValueError):
        page_size = default_page_size

    # İhale listesiyle aynı gerekçe: COUNT filtreli büyük tabloda tam tarama demek ve
    # aynı aramanın her sayfası onu yeniden hesaplıyordu. Anahtara `request.path` de
    # girer — farklı uçlar (firma listesi / firma sözleşmeleri / ihale sözleşmeleri)
    # aynı query string'le çağrılabilir, sayıları karışmasın.
    total = _cached_count(qs, qp, scope=request.path)
    start = (page - 1) * page_size
    data = serializer_class(qs[start:start + page_size], many=True,
                            context=context or {}).data
    return api_response(data={"list": data, "totalCount": total, "page": page})


_CONTRACTOR_PK_PARAM = OpenApiParameter(
    name="pk", location=OpenApiParameter.PATH, type=int, required=True,
    description="Yüklenici (firma) kimliği.",
    examples=[OpenApiExample("Firma id", value=1)],
)

_CONTRACTOR_PAGE = inline_serializer(
    name="ContractorPage",
    fields={
        "list": ContractorListSerializer(many=True),
        "totalCount": serializers.IntegerField(),
        "page": serializers.IntegerField(),
    },
)

_CONTRACT_PAGE = inline_serializer(
    name="ContractPage",
    fields={
        "list": ContractSerializer(many=True),
        "totalCount": serializers.IntegerField(),
        "page": serializers.IntegerField(),
    },
)

# order anahtarı → (model alanı, VARSAYILAN yön).
# ⚠️ Yön alanın kendisinde ("-" öneki) taşınmaz: sayısal alanlar azalan, ad ise alfabetik
# (artan) varsayılan ister. Öneki alana gömüp `siralamaTipi=asc` gelince ters çevirmek,
# `ad` için asc/desc'i takas ediyordu (asc → Z'den A'ya).
_ORDER_MAP = {
    "sozlesme_sayisi": ("sozlesme_sayisi", "desc"),
    "toplam_bedel": ("toplam_sozlesme_bedeli", "desc"),
    "son_sozlesme": ("son_sozlesme_tarihi", "desc"),
    "ad": ("kanonik_ad", "asc"),
}


@extend_schema(
    tags=["ekap"],
    auth=[],
    operation_id="ekap_contractors_list",
    summary="Yüklenici (firma) ara / listele",
    description=(
        "Sözleşme imzalamış yüklenicileri **kendi veritabanımızdan** arar.\n\n"
        "Yanıt: `data.list`, `data.totalCount`, `data.page`.\n\n"
        "**Kimlik notu:** EKAP yüklenici için VKN/vergi no vermez; firma kimliği "
        "normalize edilmiş ünvandır. Aynı ünvanın farklı yazımları tek firmada "
        "birleştirilir ve `GET /ekap/contractors/{id}/` yanıtındaki `aliaslar` "
        "alanında hangi yazımların birleştiği görülebilir.\n\n"
        "**`sozlesme_sayisi` ≠ `ihale_sayisi`:** kısımlı ihalede bir firma birden çok "
        "kısım kazanabilir (3 sözleşme / 1 ihale).\n\n"
        "**`ortalama_indirim_orani` her zaman `indirim_orani_ornek_sayisi` ile birlikte "
        "okunmalıdır** — yaklaşık maliyet yalnızca Sonuç İlanı yayımlanmış "
        "sözleşmelerde bilinir, kapsam kısmidir."
    ),
    parameters=[
        OpenApiParameter("q", str, description="Ünvan araması (Türkçe-i güvenli).",
                         examples=[OpenApiExample("Firma adı", value="decoline")]),
        OpenApiParameter("kind", str,
                         description="Tür listesi (virgülle): `firma`, `sahis`, `ortak_girisim`.",
                         examples=[OpenApiExample("Yalnız firmalar", value="firma")]),
        OpenApiParameter("il_id", str, description="İl id listesi (virgülle)."),
        OpenApiParameter("min_sozlesme", int,
                         description="En az bu kadar sözleşmesi olan firmalar."),
        OpenApiParameter("order", str,
                         enum=list(_ORDER_MAP), default="sozlesme_sayisi",
                         description="Sıralama alanı."),
        OpenApiParameter("siralamaTipi", str, enum=["desc", "asc"], default="desc"),
        OpenApiParameter("page", int, default=1),
        OpenApiParameter("page_size", int, default=20, description="En fazla 100."),
    ],
    responses={200: _CONTRACTOR_PAGE},
)
class ContractorListView(APIView):
    """GET /ekap/contractors/ — firma arama/listeleme."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        qp = request.query_params
        qs = Contractor.objects.all()

        q = qp.get("q")
        if q and q.strip():
            # Türkçe-i güvenli: normalize sütunda `contains` (asla `icontains`)
            qs = qs.filter(arama_norm__contains=normalize_tr(q))

        kinds = _as_str_list(qp.get("kind"))
        if kinds:
            qs = qs.filter(kind__in=kinds)

        il_ids = _as_int_list(qp.get("il_id"))
        if il_ids:
            qs = qs.filter(il_id__in=il_ids)

        min_soz = qp.get("min_sozlesme")
        if min_soz and str(min_soz).isdigit():
            qs = qs.filter(sozlesme_sayisi__gte=int(min_soz))

        field, varsayilan_yon = _ORDER_MAP.get(
            qp.get("order", "sozlesme_sayisi"), _ORDER_MAP["sozlesme_sayisi"]
        )
        yon = qp.get("siralamaTipi") or varsayilan_yon
        # ⚠️ `nulls_last` şart: hiç sözleşmesi olmayan firmada `toplam_sozlesme_bedeli` ve
        # `son_sozlesme_tarihi` NULL'dur. Postgres varsayılanı DESC'te NULLS FIRST olduğu
        # için "en yüksek ciro" listesinin başında boş firmalar çıkardı (SQLite'ta tersi →
        # DB'ye göre değişen davranış). Her iki yönde de NULL'lar sona.
        order_expr = (
            F(field).asc(nulls_last=True) if yon == "asc"
            else F(field).desc(nulls_last=True)
        )
        qs = qs.order_by(order_expr, "kanonik_ad")

        return _paginate(request, qs, ContractorListSerializer)


@extend_schema(
    tags=["ekap"],
    auth=[],
    parameters=[_CONTRACTOR_PK_PARAM],
    operation_id="ekap_contractor_detail",
    summary="Yüklenici detayı + istatistikleri",
    description=(
        "Firmanın kimliği, toplam istatistikleri ve kırılımları.\n\n"
        "`dagilim` (ihale tipi / il / yıl / idare) **canlı hesaplanır** — yalnızca o "
        "firmanın sözleşmeleri üzerinden, `idare` en çok 10 satır.\n\n"
        "`aliaslar` bu firmada birleştirilen ham yazımlardır (kimlik ünvan tabanlı "
        "olduğu için şeffaflık gerekir).\n\n"
        "⚠️ **Kazanma oranı hesaplanamaz:** EKAP yalnızca imzalanmış sözleşmeleri "
        "yayımlar, kaybedilen teklifler veri kaynağında yoktur."
    ),
    responses={200: OpenApiTypes.OBJECT},
)
class ContractorDetailView(APIView):
    """GET /ekap/contractors/<pk>/ — firma detayı + kırılımlar."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, pk):
        try:
            c = Contractor.objects.get(pk=pk)
        except Contractor.DoesNotExist:
            return api_response(
                data=None, message="Yüklenici bulunamadı.", success=False, status=404
            )

        # Kırılımlar tek firmanın sözleşmeleriyle sınırlı (tipik <100 satır) →
        # denormalize edilmez, canlı hesaplanır. İngest-kopyası sütunlar sayesinde
        # Tender JOIN'i yapılmaz.
        base = Contract.objects.filter(yuklenici=c)
        data = {
            "id": c.pk,
            "ad": c.kanonik_ad,
            "kanonik_anahtar": c.kanonik_anahtar,
            "kind": c.kind,
            "kind_aciklama": c.get_kind_display(),
            "tuzel_tip": c.tuzel_tip or None,
            "uyruk": c.uyruk or None,
            "adres": c.adres or None,
            "il_adi": c.il_adi or None,
            "il_id": c.il_id,
            "istatistik": {
                "sozlesme_sayisi": c.sozlesme_sayisi,
                "ihale_sayisi": c.ihale_sayisi,
                "idare_sayisi": c.idare_sayisi,
                "toplam_sozlesme_bedeli": _dec(c.toplam_sozlesme_bedeli),
                "ilk_sozlesme_tarihi": (
                    c.ilk_sozlesme_tarihi.isoformat() if c.ilk_sozlesme_tarihi else None
                ),
                "son_sozlesme_tarihi": (
                    c.son_sozlesme_tarihi.isoformat() if c.son_sozlesme_tarihi else None
                ),
                "ortalama_indirim_orani": _dec(c.ortalama_indirim_orani),
                "indirim_orani_ornek_sayisi": c.indirim_orani_ornek_sayisi,
            },
            "dagilim": self._dagilim(base),
            "aliaslar": list(c.aliaslar.values_list("ham_ad", flat=True)[:50]),
            "ortak_girisimler": [
                {"id": m.ortak_girisim.pk, "ad": m.ortak_girisim.kanonik_ad,
                 "sozlesme_sayisi": m.ortak_girisim.sozlesme_sayisi, "pilot": m.pilot}
                for m in c.ortakliklar.select_related("ortak_girisim").all()
            ],
            "uyeler": [
                {"id": m.uye.pk, "ad": m.uye.kanonik_ad, "pilot": m.pilot,
                 "sozlesme_sayisi": m.uye.sozlesme_sayisi}
                for m in c.uyelikler.select_related("uye").all()
            ],
            "uyeleri_cozumlendi": c.uyeleri_cozumlendi,
        }
        return api_response(data=data)

    @staticmethod
    def _dagilim(base):
        from decimal import Decimal

        from django.db.models import Count, Sum
        from django.db.models.functions import ExtractYear

        def money(v):
            # Sum() Decimal hassasiyetini genişletir → 2 haneye sabitle
            return str(v.quantize(Decimal("0.01"))) if v is not None else None

        def rows(qs, key):
            return [
                {key: r[key], "adet": r["adet"], "toplam_bedel": money(r["toplam"])}
                for r in qs.values(key).annotate(
                    adet=Count("id"), toplam=Sum("sozlesme_bedeli_num")
                ).order_by("-adet")
            ]

        # İl adları statik seed'den okunur (DB'deki City tablosu boş olabilir)
        il_adlari = {ekap_il_id: ad for ekap_il_id, _plaka, ad, _big in CITIES}
        idare_adlari = dict(
            base.exclude(idare_id="").values_list("idare_id", "tender__idare_adi")[:200]
        )
        return {
            "ihale_tipi": [
                {**r, "ad": IHALE_TURU.get(r["ihale_tip"], "")}
                for r in rows(base.exclude(ihale_tip__isnull=True), "ihale_tip")
            ],
            "il": [
                {**r, "ad": il_adlari.get(r["il_id"], "")}
                for r in rows(base.exclude(il_id__isnull=True), "il_id")
            ],
            "yil": rows(
                base.exclude(sozlesme_tarihi__isnull=True)
                .annotate(yil=ExtractYear("sozlesme_tarihi")), "yil"
            ),
            "idare": [
                {**r, "ad": idare_adlari.get(r["idare_id"], "")}
                for r in rows(base.exclude(idare_id=""), "idare_id")[:10]
            ],
        }


def _seri_dict(s):
    """`RecurringTenderSeries` → API sözlüğü."""
    return {
        "id": s.pk,
        "idare_id": s.idare_id,
        "idare_adi": s.idare_adi,
        "en_ust_idare_kod": s.en_ust_idare_kod or None,
        "il_id": s.il_id,
        "okas_ana_kod": s.okas_ana_kod or None,
        "okas_ana_adi": s.okas_ana_adi or None,
        "ihale_tip": s.ihale_tip,
        "ornek_ihale_adi": s.ornek_ihale_adi,
        "ihale_sayisi": s.ihale_sayisi,
        "ilk_ilan": s.ilk_ilan.isoformat() if s.ilk_ilan else None,
        "son_ilan": s.son_ilan.isoformat() if s.son_ilan else None,
        "son_ekap_id": s.son_ekap_id or None,
        "periyot_tip": s.periyot_tip,
        "periyot_gun": s.periyot_gun,
        "sapma_gun": s.sapma_gun,
        "guven": s.guven,
        "beklenen_ilan_tarihi": (
            s.beklenen_ilan_tarihi.isoformat() if s.beklenen_ilan_tarihi else None
        ),
        "beklenen_ay": s.beklenen_ay or None,
        "aktif": s.aktif,
        "ortalama_bedel": _dec(s.ortalama_bedel),
        "ortalama_indirim": _dec(s.ortalama_indirim),
        # ⚠️ Ortalama indirim ASLA örneklem sayısı olmadan gösterilmemeli.
        "indirim_ornek_sayisi": s.indirim_ornek,
    }


@extend_schema(
    tags=["ekap"],
    parameters=[
        OpenApiParameter("idare_id", str, description="İdare id listesi (virgülle)."),
        OpenApiParameter("idare_detsis", str, description="DETSIS düğümü (alt birimler dahil)."),
        OpenApiParameter("en_ust_idare_kod", str, description="Bakanlık/üst kurum kodu."),
        OpenApiParameter("okas_ana_kod", str, description="Birincil OKAS kodu (virgülle)."),
        OpenApiParameter("il_id", str, description="İl id listesi (virgülle)."),
        OpenApiParameter("ihale_tip", str, description="İhale türü listesi (virgülle)."),
        OpenApiParameter("periyot_tip", str,
                         enum=["yillik", "6_aylik", "3_aylik", "aylik", "duzensiz"]),
        OpenApiParameter("guven", str, enum=["yuksek", "orta", "dusuk"],
                         description="Tahmin güveni (üye sayısı + aralık düzenliliği)."),
        OpenApiParameter("aktif", bool, default=True,
                         description="Yalnızca canlı seriler (son ilandan bu yana 2 periyottan az geçmiş)."),
        OpenApiParameter("beklenen_gun", int,
                         description="Önümüzdeki N gün içinde beklenen seriler."),
        OpenApiParameter("order", str,
                         enum=["beklenen", "ihale_sayisi", "son_ilan"], default="beklenen"),
        OpenApiParameter("page", int, default=1),
        OpenApiParameter("page_size", int, default=20, description="En fazla 100."),
    ],
    operation_id="ekap_recurring_list",
    summary="Tekrar eden ihaleler (Pro)",
    description=(
        "Aynı idarenin yıldan yıla tekrarladığı işler ve **sıradaki ilanın beklenen "
        "tarihi**. Kullanıcı ilan çıkmadan önce hazırlanabilir.\n\n"
        "Seriler ingest'te hesaplanan bir ad-iskeleti anahtarıyla gruplanır; aynı idare + "
        "aynı OKAS + en az 2 anlamlı ortak kelime şartı vardır (yanlış birleştirmeyi "
        "önlemek için kasıtlı olarak muhafazakâr).\n\n"
        "⚠️ **`guven` alanına bakın**: `yuksek` = en az 4 üye ve düzenli aralıklar; "
        "`dusuk` serilerde `beklenen_ilan_tarihi` bir tahminden ibarettir.\n\n"
        "⚠️ `ortalama_indirim` her zaman `indirim_ornek_sayisi` ile birlikte okunmalıdır."
    ),
    responses={200: OpenApiTypes.OBJECT},
)
class RecurringSeriesListView(APIView):
    """GET /ekap/recurring/ — tekrar eden ihale serileri."""

    permission_classes = [permissions.IsAuthenticated]

    _ORDER = {
        "beklenen": "beklenen_ilan_tarihi",
        "ihale_sayisi": "-ihale_sayisi",
        "son_ilan": "-son_ilan",
    }

    def get(self, request):
        require_premium(request.user, MSG_TEKRAR)
        qp = request.query_params
        qs = RecurringTenderSeries.objects.all()

        idare_ids = _as_str_list(qp.get("idare_id"))
        if idare_ids:
            qs = qs.filter(idare_id__in=idare_ids)
        detsis = (qp.get("idare_detsis") or "").strip()
        if detsis:
            genis = descendant_idare_ids([detsis]) & tender_idare_id_set()
            qs = qs.filter(idare_id__in=sorted(genis)) if genis else qs.none()
        bakanlik = _as_str_list(qp.get("en_ust_idare_kod"))
        if bakanlik:
            qs = qs.filter(en_ust_idare_kod__in=bakanlik)
        okas = _as_str_list(qp.get("okas_ana_kod"))
        if okas:
            qs = qs.filter(okas_ana_kod__in=okas)
        il_ids = _as_int_list(qp.get("il_id"))
        if il_ids:
            qs = qs.filter(il_id__in=il_ids)
        tipler = _as_int_list(qp.get("ihale_tip"))
        if tipler:
            qs = qs.filter(ihale_tip__in=tipler)
        periyot = _as_str_list(qp.get("periyot_tip"))
        if periyot:
            qs = qs.filter(periyot_tip__in=periyot)
        guven = _as_str_list(qp.get("guven"))
        if guven:
            qs = qs.filter(guven__in=guven)

        aktif = _as_bool(qp.get("aktif"))
        if aktif is not False:  # varsayılan: yalnızca canlı seriler
            qs = qs.filter(aktif=True)

        gun = qp.get("beklenen_gun")
        if gun and str(gun).isdigit():
            bugun = timezone.localdate()
            qs = qs.filter(
                beklenen_ilan_tarihi__gte=bugun,
                beklenen_ilan_tarihi__lte=bugun + timedelta(days=int(gun)),
            )

        alan = self._ORDER.get(qp.get("order", "beklenen"), "beklenen_ilan_tarihi")
        qs = qs.order_by(F(alan.lstrip("-")).desc(nulls_last=True)
                         if alan.startswith("-")
                         else F(alan).asc(nulls_last=True), "-ihale_sayisi")

        return _paginate(request, qs, _SeriSerializer)


class _SeriSerializer(serializers.Serializer):
    """`_paginate` serializer bekliyor; dönüşüm `_seri_dict`te."""

    def to_representation(self, s):
        return _seri_dict(s)


@extend_schema(
    tags=["ekap"],
    parameters=[_TENDER_KEY_PARAM],
    operation_id="ekap_tender_recurring",
    summary="Bu ihale tekrar eden bir serinin parçası mı? (Pro)",
    description=(
        "İhale bir seriye aitse seriyi ve **geçmiş örneklerini** döner; değilse `null`.\n\n"
        "Kullanıcı böylece \"bu iş her yıl açılıyor, geçen sene kaça verilmişti\" "
        "sorusuna tek istekte cevap alır."
    ),
    responses={200: OpenApiTypes.OBJECT},
)
class TenderRecurringView(APIView):
    """GET /ekap/tenders/<key>/recurring/ — ihalenin serisi + geçmiş örnekleri."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, key):
        require_premium(request.user, MSG_TEKRAR)
        tender = _tender_by_key(key, defer_raw=True)
        if tender is None:
            return api_response(
                data=None, message="İhale bulunamadı.", success=False, status=404
            )
        if not tender.seri_anahtar:
            return api_response(data={"seri": None, "gecmis": []})

        seri = RecurringTenderSeries.objects.filter(
            seri_anahtar=tender.seri_anahtar, idare_id=tender.idare_id
        ).first()
        gecmis = (
            Tender.objects.filter(
                seri_anahtar=tender.seri_anahtar, idare_id=tender.idare_id
            )
            .exclude(pk=tender.pk)
            .only(*_LIST_FIELDS)
            .order_by("-ilan_tarihi")[:20]
        )
        return api_response(data={
            "seri": _seri_dict(seri) if seri else None,
            "gecmis": EkapTenderListSerializer(gecmis, many=True).data,
        })


@extend_schema(
    tags=["ekap"],
    parameters=[
        OpenApiParameter("idare_id", str, description="İdare id listesi (virgülle) — yaprak seçim."),
        OpenApiParameter("idare_detsis", str,
                         description="DETSIS düğümü; alt birimlerin tamamı kapsanır."),
        OpenApiParameter("en_ust_idare_kod", str,
                         description="Bakanlık/üst kurum kodu — **en ucuz yol** (tek indeksli "
                                     "eşitlik, alt ağaç genişletmesi yapılmaz).",
                         examples=[OpenApiExample("Sağlık Bakanlığı", value="15")]),
        OpenApiParameter("detay", bool, default=True,
                         description="`false` → yalnızca özet + yıllık seri (kırılımlar atlanır)."),
    ],
    operation_id="ekap_authority_profile",
    summary="İdare (alıcı) profili (Pro)",
    description=(
        "Bir idarenin satın alma davranışı: yıllık harcama, işleri kimler alıyor "
        "(**yoğunlaşma/HHI**), ortalama indirim, iptal ve itiraz oranı, usul/tür dağılımı, "
        "il ve OKAS kırılımı, **ihale takvimi** (hangi ayda ihale açıyor).\n\n"
        "Kapsam üçünden biriyle verilir; öncelik `en_ust_idare_kod` > `idare_id` > "
        "`idare_detsis`.\n\n"
        "⚠️ Kapsam çok geniş olursa (`kapsam.cok_genis`) ayrıntılı kırılımlar hesaplanmaz — "
        "on binlerce idarelik bir `IN` listesi planlayıcıyı bozar. Bakanlık geneli için "
        "`en_ust_idare_kod` kullanın.\n\n"
        "⚠️ **Ortalamalar örneklem sayısıyla birlikte okunmalı**: `ortalama_indirim` yalnızca "
        "Sonuç İlanı yayımlanmış sözleşmelerden, `ortalama_istekli_sayisi` yalnızca "
        "değerlendirmesi bitmiş ihalelerden hesaplanır. `itiraz_orani`'nın paydası da tüm "
        "ihaleler değil, bayrağı **bilinen** ihalelerdir."
    ),
    responses={200: OpenApiTypes.OBJECT},
)
class AuthorityProfileView(APIView):
    """GET /ekap/authorities/profile/ — idarenin satın alma profili."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_premium(request.user, MSG_IDARE_PROFIL)
        detay = request.query_params.get("detay", "true").strip().lower() not in ("0", "false")
        veri, hata = authority_profile.profil(request.query_params, detay=detay)
        if hata:
            return api_response(data=None, message=hata, success=False, status=400)
        return api_response(data=veri)


@extend_schema(
    tags=["ekap"],
    parameters=[
        _TENDER_KEY_PARAM,
        OpenApiParameter(
            "yil_geri", int, default=5,
            description="Kaç yıl geriye bakılsın (1-10). ⚠️ Varsayılan **5**: 2024-2025 "
                        "arşivi hâlâ doldurulduğu için dar pencere veri deliğine düşebilir.",
        ),
        OpenApiParameter(
            "kapsam", str,
            enum=["auto", "idare", "il", "ulke", "grup", "idare_tur"], default="auto",
            description="Benzerlik kademesi. `auto` → yeterli örnek bulunana kadar genişler; "
                        "kullanılan kademe yanıtta `kapsam.seviye` ile döner.",
        ),
        OpenApiParameter("limit", int, default=20, description="Benzer iş listesi boyutu (en çok 50)."),
    ],
    operation_id="ekap_tender_benchmark",
    summary="Benzer işler ve kazanan fiyat analizi (Pro)",
    description=(
        "İhaleye **benzer, sonuçlanmış** işlerin kazanan bedel ve indirim oranı "
        "dağılımını, beklenen rekabeti ve karşılaştırma listesini döner.\n\n"
        "**Free kullanıcı**: `200` döner ama sayılar maskelenir ve `kilitli: true` gelir "
        "(örneklem sayıları görünür, değerler görünmez) → istemci teaser gösterip "
        "Paywall'a yönlendirebilir. **Pro**: tüm değerler açık.\n\n"
        "⚠️ **Örneklem dürüstlüğü**: `indirim_orani` yalnızca Sonuç İlanı yayımlanmış "
        "sözleşmelerde bilinir. Her dağılım `ornek.indirim_ornek_sayisi` ve `ornek.guven` "
        "ile birlikte gelir; `ornek.yeterli_veri=false` ise dağılım gösterilmemelidir.\n\n"
        "⚠️ **Tek para medyanı yıllar arası karşılaştırılamaz** (TL enflasyonu) — "
        "`sozlesme_bedeli.yillara_gore` her zaman döner, onu kullanın."
    ),
    responses={200: OpenApiTypes.OBJECT},
)
class TenderBenchmarkView(APIView):
    """GET /ekap/tenders/<key>/benchmark/ — benzer işler + kazanan fiyat dağılımı."""

    # Uç girişsiz DEĞİL: maskeleme kullanıcıya göre yapılıyor.
    permission_classes = [permissions.IsAuthenticated]

    # Free'ye kapalı sayısal alanlar (maskeleme istemcide biçim korunarak yapılır).
    # ⚠️ `benzer_ihaleler` de listede: satırlar bedel/indirim taşıyor, kilitli değerin
    # listeden sızmaması gerek.
    _KILITLI = ("indirim_orani", "ortalama_indirim_orani", "sozlesme_bedeli",
                "yillara_gore", "rekabet", "benzer_ihaleler")

    def get(self, request, key):
        tender = _tender_by_key(key)
        if tender is None:
            return api_response(
                data=None, message="İhale bulunamadı.", success=False, status=404
            )

        try:
            limit = min(50, max(1, int(request.query_params.get("limit", 20))))
        except (TypeError, ValueError):
            limit = 20

        veri, hata = benchmark_mod.benchmark(
            tender,
            yil_geri=request.query_params.get("yil_geri") or benchmark_mod.VARSAYILAN_YIL,
            kapsam=request.query_params.get("kapsam", "auto"),
            limit=limit,
        )
        if hata:
            return api_response(data=None, message=hata, success=False, status=422)

        # ── Free maskeleme ────────────────────────────────────────────────────
        # 403 DEĞİL: değerin *varlığını* göstermek dönüşümü artırır ("47 benzer iş
        # bulundu · kazanan indirim medyanı %••,•"). İstemci `kilitli` bayrağını 403'ten
        # ayrı ele almalı — 403 doğrudan Paywall'a atlar, `kilitli` maskeli gösterir.
        if not getattr(request.user, "is_premium", False):
            for alan in self._KILITLI:
                veri[alan] = None
            veri["kilitli"] = True
        else:
            veri["kilitli"] = False
        return api_response(data=veri)


@extend_schema(
    tags=["ekap"],
    auth=[],
    parameters=[
        _CONTRACTOR_PK_PARAM,
        OpenApiParameter("il_id", str, description="İl id listesi (virgülle)."),
        OpenApiParameter("idare_id", str, description="İdare id listesi (virgülle)."),
        OpenApiParameter("ihale_tip", str, description="İhale türü listesi (virgülle)."),
        OpenApiParameter("yil", int, description="Sözleşme yılı."),
        OpenApiParameter("order", str, enum=["sozlesme_tarihi", "sozlesme_bedeli"],
                         default="sozlesme_tarihi"),
        OpenApiParameter("siralamaTipi", str, enum=["desc", "asc"], default="desc"),
        OpenApiParameter("page", int, default=1),
        OpenApiParameter("page_size", int, default=20, description="En fazla 100."),
    ],
    operation_id="ekap_contractor_contracts",
    summary="Yüklenicinin sözleşme geçmişi",
    description=(
        "Firmanın kazandığı işler (imzaladığı sözleşmeler), en yenisi başta.\n\n"
        "⚠️ **Yalnızca KAZANDIĞI işler görünür.** EKAP kaybedilen teklifleri "
        "yayımlamadığı için firmanın teklif verip kazanamadığı ihaleler bu listede "
        "yer almaz.\n\n"
        "⚠️ **Kısım tutarları dönmez** — EKAP onları bozuk ölçekte gönderiyor. "
        "`kisimlar` yalnızca kısım adlarını taşır."
    ),
    responses={200: _CONTRACT_PAGE},
)
class ContractorContractsView(APIView):
    """GET /ekap/contractors/<pk>/contracts/ — firma sözleşme geçmişi."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, pk):
        if not Contractor.objects.filter(pk=pk).exists():
            return api_response(
                data=None, message="Yüklenici bulunamadı.", success=False, status=404
            )
        qp = request.query_params
        qs = (Contract.objects.filter(yuklenici_id=pk)
              .select_related("tender").defer(*_TENDER_BLOB_FIELDS)
              .prefetch_related("kisimlar"))

        il_ids = _as_int_list(qp.get("il_id"))
        if il_ids:
            qs = qs.filter(il_id__in=il_ids)
        idare_ids = _as_str_list(qp.get("idare_id"))
        if idare_ids:
            qs = qs.filter(idare_id__in=idare_ids)
        tipler = _as_int_list(qp.get("ihale_tip"))
        if tipler:
            qs = qs.filter(ihale_tip__in=tipler)
        yil = qp.get("yil")
        if yil and str(yil).isdigit():
            qs = qs.filter(sozlesme_tarihi__year=int(yil))

        field = ("sozlesme_bedeli_num" if qp.get("order") == "sozlesme_bedeli"
                 else "sozlesme_tarihi")
        prefix = "" if qp.get("siralamaTipi") == "asc" else "-"
        qs = qs.order_by(f"{prefix}{field}")

        return _paginate(request, qs, ContractSerializer)


@extend_schema(
    tags=["ekap"],
    auth=[],
    parameters=[
        OpenApiParameter(
            name="key", location=OpenApiParameter.PATH, type=str, required=True,
            description=(
                "İhalenin EKAP iç kimliği (`ekap_id`). **İKN kullanmayın** — İKN `/` "
                "içerir ve yol parametresi olarak eşleşmez."
            ),
            examples=[OpenApiExample("EKAP iç kimliği", value="e8c865a28be0b142")],
        ),
    ],
    operation_id="ekap_tender_contracts",
    summary="İhalenin sözleşmeleri ve yüklenicileri",
    description=(
        "İhalede imzalanmış sözleşmeler + bağlı yüklenici firmalar. Kısımlı ihalede "
        "birden çok sözleşme olur.\n\n"
        "`yuklenici` **null olabilir** (satır henüz firmaya çözülmemişse) — istemci "
        "bunu tolere etmelidir. Ortak girişimlerde `yuklenici.uyeler` üye firmaları verir."
    ),
    responses={200: OpenApiTypes.OBJECT},
)
class TenderContractsView(APIView):
    """GET /ekap/tenders/<key>/contracts/ — ihalenin sözleşmeleri."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, key):
        tender = _tender_by_key(key, defer_raw=True)  # yalnızca FK filtresi olarak lazım
        if tender is None:
            return api_response(data={"list": []})
        qs = (tender.sozlesmeler.select_related("tender", "yuklenici")
              .defer(*_TENDER_BLOB_FIELDS)
              .prefetch_related("kisimlar", "yuklenici__uyelikler__uye")
              .order_by("-sozlesme_bedeli_num"))
        return api_response(data={"list": TenderContractSerializer(qs, many=True).data})
