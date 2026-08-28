"""
EKAP → DB eşleme ve toplama (ingest) mantığı.

Celery görevleri (`tasks.py`) ve yönetim komutları bu fonksiyonları çağırır.
Response şekilleri mobil ekran tüketiminden çıkarıldı; alanlar savunmacı `.get`
ile okunur ve tam ham JSON `detail_raw`/`list_raw`'da saklanır.
"""
import logging
from datetime import timedelta

from django.db import models as dj_models
from django.utils import timezone

from . import contractors as contractors_mod
from . import keywords as keywords_mod
from .constants import DURUM_SONUCLANMIS
from .models import (
    Announcement,
    Authority,
    City,
    Contract,
    ContractorAlias,
    ContractSection,
    OkasCode,
    OkasItem,
    Tender,
    TenderDate,
)
from .sonuc_ilani import parse_sonuc_ilani
from .utils import (
    normalize_tr,
    parse_ekap_datetime,
    parse_money,
    parse_money_value,
    pick_money,
)

logger = logging.getLogger("ihaletakip")


# ── Response yardımcıları ──────────────────────────────
def extract_list(resp):
    """Arama response'undan (list, totalCount) çıkarır. DevExtreme `loadResult`
    ve düz `list/data/items` sarmallarını destekler."""
    if not resp:
        return [], 0
    if isinstance(resp, list):
        return resp, len(resp)
    if not isinstance(resp, dict):
        return [], 0
    # DevExtreme: {"loadResult": {"data": [...], "totalCount": N}}
    if isinstance(resp.get("loadResult"), dict):
        inner = resp["loadResult"]
        data = inner.get("data") or []
        total = inner.get("totalCount") or len(data)
        return data, int(total)
    for key in ("list", "data", "items"):
        val = resp.get(key)
        if isinstance(val, list):
            total = resp.get("totalCount") or resp.get("total") or len(val)
            return val, int(total)
    return [], 0


# Liste yanıtında boş gelebilen ama DETAY senkronunda dolan alanlar. Değer `None`
# ise liste upsert'i bu alanlara **dokunmaz** (bkz. upsert_tender_from_list).
_LISTE_EZMEZ = ("ilan_tarihi", "il_id", "ihale_tarihi")


def _resolve_il_id(item):
    il_id = item.get("ihaleIlId") or item.get("ilId")
    if il_id:
        try:
            return int(il_id)
        except (TypeError, ValueError):
            pass
    ad = (item.get("ihaleIlAdi") or "").strip().upper()
    if ad:
        city = City.objects.filter(ad=ad).first()
        if city:
            return city.ekap_il_id
    return None


# ── Liste satırı upsert ────────────────────────────────
def upsert_tender_from_list(item) -> Tender | None:
    """Arama listesi item'ından Tender liste-seviyesi alanlarını upsert eder."""
    ekap_id = item.get("id")
    ikn = item.get("ikn")
    if ekap_id is None or not ikn:
        return None

    defaults = {
        "ekap_id": str(ekap_id),
        "ihale_adi": item.get("ihaleAdi", "") or "",
        "idare_adi": item.get("idareAdi", "") or "",
        "ihale_adi_norm": normalize_tr(item.get("ihaleAdi", "")),
        "idare_adi_norm": normalize_tr(item.get("idareAdi", "")),
        "ihale_il_adi": (item.get("ihaleIlAdi") or "").upper(),
        "il_id": _resolve_il_id(item),
        "ihale_tarih_saat": item.get("ihaleTarihSaat", "") or "",
        "ihale_tarihi": parse_ekap_datetime(item.get("ihaleTarihSaat")),
        "ilan_tarihi": parse_ekap_datetime(item.get("ilanTarihi") or item.get("ilanTarihSaat")),
        "ihale_tip": _as_int(item.get("ihaleTip")),
        "ihale_tipi_aciklama": item.get("ihaleTipAciklama", "") or "",
        "ihale_usul_aciklama": item.get("ihaleUsulAciklama", "") or "",
        "ihale_durum": _as_int(item.get("ihaleDurum")),
        "ihale_durum_aciklama": item.get("ihaleDurumAciklama", "") or "",
        "dokuman_sayisi": _as_int(item.get("dokumanSayisi")) or 0,
        "ilan_var_mi": bool(item.get("ilanVarMi")),
        "list_raw": item,
        "list_synced_at": timezone.now(),
    }
    # ⚠️⚠️ **Liste upsert'i, DETAYDAN dolan alanları NULL ile EZMEMELİ.**
    # `ilanTarihi` liste yanıtında **%100 boş** gelir (belgeli); `ihaleIlId` de sık
    # sık boştur. Bu alanlar `update_or_create(defaults=...)` ile koşulsuz yazılınca
    # her liste turu, detay senkronunun doldurduğu değeri **siliyordu**.
    # Üretimde ölçüldü (2026-08-27): `sync_recent` turundan sonra 25 ve 26 Ağustos'un
    # `ilan_tarihi` dolu kayıt sayısı 157/213 → **0**. `sync_recent` günde 12 kez ve
    # `backfill` tüm gün aynı fonksiyonu çağırdığı için arşiv genelinde siliniyordu.
    # Etkisi bildirimlere kadar uzanıyor: filtre/idare/OKAS görevlerinin tamamı
    # `ilan_tarihi` üzerinden çalışır, alan NULL'lanınca **hiçbiri eşleşme bulamaz**.
    # ⚠️ Genel kural: EKAP'ın bir alanı **boş döndürmesi** "değer yok" demektir,
    # "değeri sil" demek DEĞİL. Buraya yeni alan eklerken, alan detay senkronunda
    # doluyorsa `_LISTE_EZMEZ`'e de ekleyin.
    for alan in _LISTE_EZMEZ:
        if defaults.get(alan) is None:
            defaults.pop(alan, None)

    # İKN kanonik ihale kimliğidir (bir ihale = bir İKN). EKAP'ın iç `id`'si aynı
    # İKN için değişebildiğinden (yeniden yayım vb.) upsert İKN'ye göre yapılır;
    # ekap_id son gelen değere güncellenir. (ekap_id ile upsert edilirse aynı İKN
    # farklı id'yle geldiğinde ikn unique kısıtını ihlal edip insert patlardı.)
    tender, _ = Tender.objects.update_or_create(
        ikn=str(ikn), defaults=defaults
    )
    return tender


def _as_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── Detay upsert ───────────────────────────────────────
def detay_govdesi(raw):
    """Ham detay yanıtından gövdeyi çıkarır.

    ⚠️ `Tender.detail_raw` **ham EKAP yanıtını** saklar; EKAP gövdeyi çoğu zaman
    `{"item": {...}}` içine sarar (bazen sarmadan döner). `detail_raw`'ı doğrudan
    `data` gibi kullanan kod `ilanList`/`ihaleBilgi` gibi anahtarları **bulamaz ve
    sessizce boş döner** — üretimde yaşandı (2026-08-27): `fix_ilan_tarihi` 465
    kaydın hepsini işledi, hiçbirini onaramadı (`onarılan=0`), çünkü sarmalı açmıyordu.
    `detail_raw`'dan türetme yapan HER yer bu yardımcıdan geçmeli.
    """
    if not isinstance(raw, dict):
        return {}
    govde = raw.get("item", raw)
    return govde if isinstance(govde, dict) else {}


def _publish_date_from_ilanlar(data, announcements):
    """
    Detay `ilanList`'inden ihalenin **yayın (ilan) tarihini** çıkarır.
    İhale İlanı (`ilanTip=1`) tercih edilir; yoksa en erken ilan tarihi kullanılır.
    (EKAP `ilanTarihi`'yi liste yanıtında boş döndürür → tender.ilan_tarihi ancak
    burada, detaydan dolar.)
    """
    ilan_list = list(data.get("ilanList") or [])
    if announcements:
        try:
            extra, _ = extract_list(announcements)
            ilan_list += extra
        except Exception:
            pass
    ihale_ilani = None
    dates = []
    for i in ilan_list:
        d = parse_ekap_datetime(i.get("ilanTarihi"))
        if not d:
            continue
        dates.append(d)
        if _as_int(i.get("ilanTip")) == 1 and ihale_ilani is None:
            ihale_ilani = d
    return ihale_ilani or (min(dates) if dates else None)


# `apply_pro_fields`'in yazdığı alanlar. Backfill görevi `bulk_update`'e bu listeyi verir.
# ⚠️ Buraya alan eklerken `apply_pro_fields`'e de ekleyin (ve tersi) — eksik kalan alan
# backfill'de sessizce hiç yazılmaz.
PRO_TENDER_FIELDS = [
    "okas_ana_kod", "okas_ana_adi", "okas_bucket", "okas_kalem_sayisi",
    "en_ust_idare_kod", "en_ust_idare_adi",
    "istekli_sayisi",
    # ⚠️ Şikâyet bayrakları BİLEREK YOK — `islemlerKuralSeti` kullanıcıya özeldir,
    # üçü de üretimde 1.043.450 ihalenin sıfırında true. Bkz. apply_pro_fields.
    "fiyat_disi_unsur_var", "e_eksiltme_yapilacak", "duzeltme_ilani_var",
    "kismi_ihale", "ilansiz_mi",
    "seri_anahtar",
]


def _ucdeger(kaynak: dict, anahtar: str):
    """
    Üç değerli bayrak: anahtar yoksa `None` ("bilinmiyor"), varsa `bool`.

    ⚠️ `kaynak.get(anahtar, False)` KULLANMAYIN — bilinmeyeni "hayır"a çevirir ve
    "itirazı olmayan ihaleler" filtresi, detayı ayrıştırılmamış ihaleleri de toplar.
    """
    if anahtar not in kaynak:
        return None
    return bool(kaynak[anahtar])


def apply_pro_fields(tender: Tender, data: dict) -> None:
    """
    `detail_raw["item"]` → Pro sinyal kolonları. EKAP'a istek ATMAZ.

    **Tek kaynak**: hem canlı detay senkronu (`upsert_tender_detail`) hem geriye dönük
    doldurma görevi (`ekap.tasks.backfill_tender_fields`) bunu çağırır. İki ayrı çıkarım
    yazılsaydı arşiv ile yeni kayıtlar sessizce farklı semantiğe kayardı.

    `tender` üzerinde alanları set eder, kaydetmez (çağıran `save`/`bulk_update` yapar).
    """
    from .series import series_key

    bilgi = data.get("ihaleBilgi") or {}
    idare = data.get("idare") or {}

    # ── Birincil OKAS ─────────────────────────────────────────────────────────
    # Kaynak `ihtiyacKalemiOkasList[0]`; `ihaleBilgi.okas` aynı listenin birleştirilmiş
    # string'i olduğu için yalnızca liste boşsa yedek olarak ayrıştırılır.
    okas_list = data.get("ihtiyacKalemiOkasList") or []
    if okas_list:
        ilk = okas_list[0] or {}
        tender.okas_ana_kod = str(ilk.get("kodu") or "")[:16]
        tender.okas_ana_adi = str(ilk.get("adi") or "")[:500]
    else:
        ham = str(bilgi.get("okas") or "").strip()
        if ham:
            # "45350000 - Mekanik tesisatlar, 45000000 - İnşaat işleri" → ilk çift
            ilk_parca = ham.split(",")[0]
            kod, _, ad = ilk_parca.partition(" - ")
            tender.okas_ana_kod = kod.strip()[:16]
            tender.okas_ana_adi = ad.strip()[:500]
    tender.okas_bucket = tender.okas_ana_kod[:4]
    tender.okas_kalem_sayisi = len(okas_list)

    # ── Bakanlık (üst kurum) ──────────────────────────────────────────────────
    tender.en_ust_idare_kod = str(idare.get("enUstIdareKod") or "")[:16]
    tender.en_ust_idare_adi = str(idare.get("enUstIdareAdi") or "")[:500]

    # ── Katılım ───────────────────────────────────────────────────────────────
    # `or None`: boş liste "sıfır istekli" değil "değerlendirme henüz bitmedi" demek.
    tender.istekli_sayisi = len(data.get("tebligatAlanIstekliList") or []) or None

    # ── islemlerKuralSeti bayrakları ──────────────────────────────────────────
    # ⚠️⚠️ **`islemlerKuralSeti` bir İHALE ÖZELLİĞİ DEĞİLDİR** — o an giriş yapmış EKAP
    # kullanıcısına özel bir arayüz durum kümesidir (hangi buton aktif olacak).
    # Ham örnek: `dokumanIndirmisMi` (BEN indirdim mi), `teklifteBulunmusMu` (BEN teklif
    # verdim mi), `sozlesmeImzaliMi` (BENİM sözleşmem var mı), `teklifVerilebilirMi`
    # (BEN teklif verebilir miyim). Biz EKAP'a üçüncü taraf olarak gittiğimiz için
    # kullanıcıya bağlı bayraklar **kalıcı olarak `false`** döner.
    #
    # Üretim ölçümü (2026-08-13, 1.043.450 detaylı ihale):
    #   itirazenESikayetMi  → true olan: **0**
    #   idareyeSikayetMi    → true olan: **0**
    #   sikayetDilekceVarMi → true olan: **0**
    # Bunları `False` yazmak "bu ihaleye itiraz edilmedi" demektir — oysa BİLMİYORUZ.
    # Bilinmeyeni "hayır"a çevirmek bu kod tabanının en sert kuralının ihlalidir
    # (aynı ilke `_ucdeger`'in var oluş sebebi). Bu yüzden üçü de **yazılmıyor**:
    # kolonlar `None` (bilinmiyor) kalır ve API'de hiç sunulmaz.
    kural = data.get("islemlerKuralSeti") or {}
    # itirazen_sikayet_var / idareye_sikayet_var / sikayet_dilekce_var: BİLEREK YAZILMIYOR
    tender.fiyat_disi_unsur_var = _ucdeger(kural, "fiyatDisiUnsurVarMi")
    tender.e_eksiltme_yapilacak = _ucdeger(kural, "eEksiltmeYapilacakMi")
    tender.duzeltme_ilani_var = _ucdeger(kural, "ilanDuzeltmeIlani")

    tender.kismi_ihale = _ucdeger(data, "kismiIhale")
    tender.ilansiz_mi = _ucdeger(data, "ihaleIlansizMi")

    # ── Seri anahtarı (Adım 7) ────────────────────────────────────────────────
    # Burada üretilir ki arşivin `detail_raw`'ı bir kez okunsun.
    tender.seri_anahtar = series_key(
        tender.idare_id, tender.okas_ana_kod, tender.ihale_adi
    )


def upsert_tender_detail(ekap_id, detail, announcements=None) -> Tender:
    """Detay response'unu Tender + çocuk tablolara yazar."""
    data = detay_govdesi(detail)
    bilgi = data.get("ihaleBilgi", {}) or {}
    idare = data.get("idare", {}) or {}

    tender, _ = Tender.objects.get_or_create(
        ekap_id=str(ekap_id),
        defaults={"ikn": str(data.get("ikn") or bilgi.get("ikn") or ekap_id)},
    )

    # Top-level + ihaleBilgi alanları (liste verisini eziyorsa dikkatli birleştir)
    tender.ikn = str(data.get("ikn") or bilgi.get("ikn") or tender.ikn)
    tender.ihale_adi = data.get("ihaleAdi") or bilgi.get("ihaleAdi") or tender.ihale_adi
    tender.idare_adi = data.get("idareAdi") or bilgi.get("idareAdi") or tender.idare_adi
    tender.ihale_adi_norm = normalize_tr(tender.ihale_adi)
    tender.idare_adi_norm = normalize_tr(tender.idare_adi)
    tender.idare_id = str(data.get("idareId") or bilgi.get("idareId") or tender.idare_id or "")
    tender.e_ihale = bool(data.get("eIhale", tender.e_ihale))
    tender.ihale_usul = _as_int(data.get("ihaleUsul")) or tender.ihale_usul
    tender.ihale_usul_aciklama = bilgi.get("ihaleUsulAciklama") or tender.ihale_usul_aciklama
    tender.ihale_tipi_aciklama = bilgi.get("ihaleTipiAciklama") or tender.ihale_tipi_aciklama
    tender.ihale_tip = _as_int(bilgi.get("ihaleTip")) or tender.ihale_tip
    tender.ihale_durum = _as_int(data.get("ihaleDurum") or bilgi.get("ihaleDurum")) or tender.ihale_durum
    tender.ihale_durum_aciklama = bilgi.get("ihaleDurumAciklama") or tender.ihale_durum_aciklama
    tender.ihale_kapsam_aciklama = (
        data.get("ihaleKapsamAciklama") or bilgi.get("ihaleKapsamAciklama") or tender.ihale_kapsam_aciklama
    )
    tender.yasa_kapsami = _as_int(bilgi.get("yasaKapsami4734")) or tender.yasa_kapsami

    # İhale özellik etiketleri (gelişmiş boolean filtreleri için)
    ozellik_list = data.get("ihaleOzellikList") or []
    tender.ozellikler = [
        (o.get("ihaleOzellik") or "").replace("TENDER_DETAIL.", "")
        for o in ozellik_list
        if o.get("ihaleOzellik")
    ]
    tender.ihale_tarih_saat = bilgi.get("ihaleTarihSaat") or tender.ihale_tarih_saat
    tender.ihale_tarihi = parse_ekap_datetime(bilgi.get("ihaleTarihSaat")) or tender.ihale_tarihi
    tender.isin_yapilacagi_yer = bilgi.get("isinYapilacagiYer") or tender.isin_yapilacagi_yer
    tender.ihale_yeri = bilgi.get("ihaleYeri") or tender.ihale_yeri
    tender.itirazen_sikayet_basvuru_bedeli = (
        str(bilgi.get("itirazenSikayetBasvuruBedeli") or "") or tender.itirazen_sikayet_basvuru_bedeli
    )
    tender.iptal_tarihi = str(bilgi.get("iptalTarihi") or "") or tender.iptal_tarihi
    tender.iptal_nedeni = bilgi.get("iptalNedeni") or tender.iptal_nedeni
    tender.iptal_madde = str(bilgi.get("iptalMadde") or "") or tender.iptal_madde

    # İdare bilgisi
    il = idare.get("il") or {}
    ilce = idare.get("ilce") or {}
    if il.get("adi"):
        tender.ihale_il_adi = str(il["adi"]).upper()
    if il.get("id"):
        tender.il_id = _as_int(il["id"])
    tender.ilce_adi = ilce.get("ilceAdi") or tender.ilce_adi
    # ⚠️ `ust_idare` BİLEREK olduğu gibi bırakıldı (mobil okuyor olabilir). Ama bu alan
    # bakanlık DEĞİLDİR: `ustIdare` doluysa (ör. "BAKAN YARDIMCILIKLARI") kazanır ve
    # gerçek bakanlık adı düşer. Bakanlık kırılımı için `en_ust_idare_*` kullanın —
    # onları `apply_pro_fields` yazar (tek kaynak, backfill ile ortak).
    tender.ust_idare = idare.get("ustIdare") or idare.get("enUstIdareAdi") or tender.ust_idare
    tender.idare_telefon = str(idare.get("telefon") or "") or tender.idare_telefon
    tender.idare_fax = str(idare.get("fax") or "") or tender.idare_fax

    # İlan (yayın) tarihi — detayın ilanList'inden (liste yanıtında boş gelir)
    pub = _publish_date_from_ilanlar(data, announcements)
    if pub:
        tender.ilan_tarihi = pub
    # Doküman sayısı / ilan var mı — detaydan da tazele
    if data.get("dokumanSayisi") is not None:
        tender.dokuman_sayisi = _as_int(data.get("dokumanSayisi")) or 0
    if data.get("ilanVarMi") is not None:
        tender.ilan_var_mi = bool(data.get("ilanVarMi"))

    tender.detail_raw = detail
    tender.detail_synced_at = timezone.now()
    tender.sync_status = Tender.SyncStatus.OK
    tender.sync_error = ""
    # Pro sinyal kolonları — `idare_id`/`ihale_adi` set edildikten SONRA (seri anahtarı
    # ikisini de kullanıyor), `save()`'den önce.
    apply_pro_fields(tender, data)
    # Anahtar kelime katmanı — **ingest hızlı yolu**. Kalıp sözlüğünde `durum="ok"`
    # bir kayıt varsa keyword'ler AI'ya HİÇ gidilmeden kopyalanır (tek indeksli
    # SELECT + birkaç INSERT); yoksa kalıp `pending` açılır ve bir sonraki
    # `dispatch` turunda modele sorulur. Günde ~700 yeni ihalenin AI maliyeti bu
    # sayede sıfıra yakındır.
    # ⚠️ Hata YUTULUR: keyword bir zenginleştirmedir, detay senkronunu düşürmemeli.
    try:
        keywords_mod.uygula(tender)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("keyword uygulanamadı (%s): %s", tender.ekap_id, exc)
    tender.save()

    # ── Çocuk tablolar (tam yenile) ────────────────────
    _sync_children(tender, bilgi, data, announcements)
    return tender


def _sync_children(tender, bilgi, data, announcements):
    # Tarihler
    tender.tarihler.all().delete()
    TenderDate.objects.bulk_create([
        TenderDate(tender=tender, etiket=t.get("ihaleTarihiEtiket", ""),
                   deger=t.get("ihaleTarihiEtiketDegeri", ""))
        for t in (bilgi.get("ihaleTarihSaatList") or [])
    ])

    # OKAS kalemleri
    tender.okas_kalemleri.all().delete()
    OkasItem.objects.bulk_create([
        OkasItem(
            tender=tender,
            kodu=str(o.get("kodu", "")),
            adi=o.get("adi", "") or "",
            adi_norm=normalize_tr(o.get("adi", "")),
        )
        for o in (data.get("ihtiyacKalemiOkasList") or [])
    ])

    # İlanlar + sözleşmeler + kısımlar → kararlı anahtarla upsert (bkz. aşağısı)
    sync_contracts_from_raw(tender, detail={"item": data}, announcements=announcements)


# ── Sözleşme / yüklenici çözümlemesi ───────────────────
def _dedupe_by_key(rows, key_of):
    """
    Aynı anahtar iki kez gelirse SONUNCUSU kalır.

    ⚠️ Zorunlu: `ilanList` ile ayrı announcements yanıtı birleştirildiğinde aynı `id`
    tekrar edebiliyor; Postgres `ON CONFLICT DO UPDATE`'in aynı satıra iki kez
    dokunmasını reddeder ("cannot affect row a second time").
    """
    out = {}
    for row in rows:
        out[key_of(row)] = row
    return out


def _kirp_alanlar(model, objs):
    """Model CharField sınırlarını aşan string değerleri kırpar (uyarı loglar)."""
    if not objs:
        return
    limitler = {
        f.attname: f.max_length
        for f in model._meta.concrete_fields
        if isinstance(f, dj_models.CharField) and f.max_length
    }
    for obj in objs:
        for attname, limit in limitler.items():
            deger = getattr(obj, attname, None)
            if isinstance(deger, str) and len(deger) > limit:
                logger.warning(
                    "%s.%s %s karakter, %s'e kırpıldı: %r",
                    model.__name__, attname, len(deger), limit, deger[:120],
                )
                setattr(obj, attname, deger[:limit])


def _bulk_upsert_children(model, child_qs, wanted, key_attr, fields, build):
    """
    Kararlı anahtarla toplu upsert + budama — **satır sayısından bağımsız ~4 sorgu**.

    `update_or_create` döngüsü satır başına 2-3 sorgu yapıyordu; 69 kısımlı bir ihale
    300+ sorguya çıkıyordu ki milyonlarca ihalelik backfill için kabul edilemez.

    child_qs: mevcut çocuk satırların queryset'i · wanted: {key: alan sözlüğü}
    build(key, vals) → yeni örnek. Döner: {key: örnek} (yeni + güncel hepsi)

    ⚠️ Mevcut satırlar **anahtar başına tekilleştirilir**: eski sil-yeniden-yaz
    döneminden kalan satırların hepsi `ekap_*_id=""` taşıyor, yani aynı ihalede N tane
    aynı-anahtarlı satır olabiliyor. Sözlüğe doğrudan çevirmek fazlalıkları sessizce
    gizler ve budama listesi onları hiç görmezdi → her tenderda N-1 yetim satır kalırdı.
    """
    existing, stale = {}, []
    for obj in child_qs:
        key = getattr(obj, key_attr)
        if key in existing:
            stale.append(obj.pk)  # aynı anahtardan fazlalık → buda, birini tut
        else:
            existing[key] = obj

    to_create, to_update, out = [], [], {}
    for key, vals in wanted.items():
        obj = existing.get(key)
        if obj is None:
            out[key] = build(key, vals)
            to_create.append(out[key])
            continue
        changed = False
        for f, v in vals.items():
            if getattr(obj, f) != v:
                setattr(obj, f, v)
                changed = True
        if changed:
            to_update.append(obj)
        out[key] = obj

    # ⚠️ **EKAP string alanları kolon sınırını aşabilir → yazmadan önce kırp.**
    # Üretimde yaşandı (2026-08-27): İKN 2019/430389'da bir ham tutar alanı
    # `varchar(100)`'e sığmadı, `bulk_create` `DataError: value too long` attı ve
    # `sync_contractors` **her turda** aynı ihalede `errors=1` verdi (artımlı mod
    # hatada `contractors_synced_at`'i ilerletmediği için kayıt sonsuza dek geri
    # geliyordu). Tek kolonu genişletmek yerine sınıfın tamamına koruma konuldu:
    # ham string alanları izlenebilirlik içindir, sayısal karşılıkları ayrı
    # kolonlarda durur (`sozlesme_bedeli_num` vb.) → kırpmak veri kaybı değildir.
    # ⚠️ Kırpma **sessiz olmamalı**: ne kırpıldığı log'a yazılır, yoksa EKAP'ın
    # beklenmedik bir alan formatına geçtiğini fark edemeyiz.
    _kirp_alanlar(model, to_create)
    _kirp_alanlar(model, to_update)

    if to_create:
        model.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        model.objects.bulk_update(to_update, fields, batch_size=500)

    stale += [o.pk for key, o in existing.items() if key not in wanted]
    if stale:
        model.objects.filter(pk__in=stale).delete()
    return out


_ANNOUNCEMENT_FIELDS = [
    "ilan_tip", "ilan_tarihi", "baslik", "veri_html", "istekli_adi", "ekap_sozlesme_id",
]


def _upsert_announcements(tender, data, announcements) -> dict:
    """İlanları `ekap_ilan_id` ile upsert eder, artıkları budar. Döner: {sozlesme_id: ilan}."""
    ilan_list = list(data.get("ilanList") or [])
    if announcements:
        extra, _ = extract_list(announcements)
        ilan_list += extra

    deduped = _dedupe_by_key(
        [i for i in ilan_list if i], lambda i: str(i.get("id", "") or "")
    )
    wanted = {
        (key or f"noid:{idx}"): {
            "ilan_tip": _as_int(i.get("ilanTip")),
            "ilan_tarihi": parse_ekap_datetime(i.get("ilanTarihi")),
            "baslik": (i.get("baslik") or "")[:500],
            "veri_html": i.get("veriHtml") or "",
            "istekli_adi": (i.get("istekliAdi") or "")[:500],
            "ekap_sozlesme_id": str(i.get("sozlesmeId") or "")[:64],
        }
        for idx, (key, i) in enumerate(deduped.items())
    }

    objs = _bulk_upsert_children(
        Announcement,
        tender.ilanlar.all(),
        wanted,
        "ekap_ilan_id",
        _ANNOUNCEMENT_FIELDS,
        lambda key, vals: Announcement(tender=tender, ekap_ilan_id=key, **vals),
    )
    # Sonuç ilanları (ilanTip=4) sözleşmeye `sozlesmeId` ile bağlanır
    return {
        o.ekap_sozlesme_id: o
        for o in objs.values()
        if o.ilan_tip == 4 and o.ekap_sozlesme_id
    }


_CONTRACT_FIELDS = [
    "yuklenici_adi", "yuklenici_adi_norm", "yuklenici",
    "sozlesme_tarih", "sozlesme_tarihi",
    "sozlesme_bedeli", "sozlesme_bedeli_num",
    "en_dusuk_teklif", "en_dusuk_teklif_num",
    "en_yuksek_teklif", "en_yuksek_teklif_num",
    "yaklasik_maliyet", "yaklasik_maliyet_num", "yaklasik_maliyet_kaynak",
    "tender_yaklasik_maliyet_num", "teklif_sayisi", "gecerli_teklif_sayisi",
    "dokuman_indiren_sayisi", "indirim_orani", "ekap_ilan_id",
    "fesih_string", "tasfiye_transfer_string",
    "idare_id", "il_id", "ihale_tip",
    "okas_ana_kod", "okas_bucket", "en_ust_idare_kod", "sektor",
    # ⚠️ `ilk_gorulme` BİLEREK YOK: yalnızca yaratma yolunda yazılır. Buraya eklenirse
    # her detay tazelemesinde güncellenir ve rakip alarmının dayandığı "yeni keşfedilen
    # sözleşme" sinyali anlamını yitirir.
]


def sync_contracts_from_raw(
    tender, *, detail=None, announcements=None, recompute=True
) -> dict:
    """
    İhalenin sözleşmelerini + yüklenici bağlantılarını ham detaydan kurar.

    `detail=None` ise `tender.detail_raw` kullanılır → **EKAP'a hiç gidilmez**; offline
    backfill/onarım bu sayede mümkündür.

    `recompute=True` (varsayılan) dokunulan firmaların agregalarını hemen yeniden
    hesaplar — **canlı detay senkronu için şart**: EKAP sözleşme bedelini/tarihini
    güncellediğinde `Contract` satırı düzelir ama firma sayaçları (`sozlesme_sayisi`,
    `toplam_sozlesme_bedeli`, `son_sozlesme_tarihi` = firma listesinin sıralama
    anahtarları) bayat kalırdı. Artımlı görev de yakalayamaz, çünkü bu fonksiyon
    `contractors_synced_at`'i güncelliyor → "bayat" görünmüyor.
    Toplu çağıranlar (backfill görevi/komutu) `recompute=False` verip birleşik kümeyi
    sonda tek seferde hesaplar.

    Döner: `{"contracts": n, "contractors": {id, ...}}`
    """
    raw = detail if detail is not None else tender.detail_raw
    if not raw:
        return {"contracts": 0, "contractors": set(), "alias_cakismasi": 0}
    data = detay_govdesi(raw)
    if not data:
        return {"contracts": 0, "contractors": set(), "alias_cakismasi": 0}

    # 1) İlanlar önce — sözleşmeler sonuç ilanına bağlanacak
    sonuc_ilanlari = _upsert_announcements(tender, data, announcements)

    sozlesme_list = [s for s in (data.get("sozlesmeBilgiList") or []) if s]
    if not sozlesme_list:
        # EKAP tüm sözleşmeleri kaldırmış olabilir (iptal/yeniden yayım). Satırları
        # silmeden ÖNCE firmaları topla, yoksa sayaçları bayat kalırdı.
        onceki = set(
            tender.sozlesmeler.exclude(yuklenici__isnull=True)
            .values_list("yuklenici_id", flat=True)
        )
        tender.sozlesmeler.all().delete()
        Tender.objects.filter(pk=tender.pk).update(
            contractors_synced_at=timezone.now(),
            sonuc_ilani_eksik=False,
            yaklasik_maliyet_num=None,
            # Sözleşmeler kaldırıldı → özet de sıfırlanmalı, yoksa "sonuçlanmış"
            # filtresi bu ihaleyi hâlâ eşleştirirdi.
            sozlesme_sayisi=0,
            toplam_sozlesme_bedeli=None,
        )
        if recompute and onceki:
            contractors_mod.recompute_aggregates(onceki)
        return {"contracts": 0, "contractors": onceki, "alias_cakismasi": 0}

    # 2) Tüm yüklenici adları TEK çağrıda çözülür (13 sözleşme de olsa ~5 sorgu)
    resolved = contractors_mod.resolve_contractors(
        [s.get("yukleniciAdi") for s in sozlesme_list]
    )

    now = timezone.now()
    touched_contractors, seen_keys = set(), []
    sonuc_eksik = False
    wanted, kisimlar_by_key, alias_jobs = {}, {}, []
    # İhalede birden çok sözleşme varsa (kısımlı ihale), Sonuç İlanı'ndan gelen "kısım
    # maliyeti" aslında ihale toplamı olabilir → bkz. `_kisim_maliyeti_belirsiz`.
    cok_sozlesmeli = len(sozlesme_list) > 1

    for idx, s in enumerate(sozlesme_list):
        # Anahtar asla boş kalmamalı — koşulsuz unique constraint (R2) buna dayanır
        key = str(s.get("id") or "").strip() or f"noid:{idx}"
        seen_keys.append(key)

        ham_ad = (s.get("yukleniciAdi") or "").strip()
        contractor = resolved.get(ham_ad)

        # ⚠️ Para: EKAP'ın hazır `*Degeri` float'ları güvenilir; string'ler formatı
        # karıştırır. `yaklasikMaliyet` string'i BOZUK üretildiği için hiç ayrıştırılmaz
        # (yalnızca Sonuç İlanı'ndan doldurulur).
        defaults = {
            "yuklenici_adi": ham_ad[:500],
            "yuklenici_adi_norm": normalize_tr(ham_ad)[:500],
            "yuklenici": contractor,
            "sozlesme_tarih": str(s.get("sozlesmeTarih") or ""),
            "sozlesme_tarihi": parse_ekap_datetime(s.get("sozlesmeTarih")),
            "sozlesme_bedeli": str(s.get("sozlesmeBedeli") or ""),
            "sozlesme_bedeli_num": pick_money(
                parse_money_value(s.get("sozlesmeBedeliDegeri")),
                parse_money(s.get("sozlesmeBedeli")),
            ),
            "en_dusuk_teklif": str(s.get("enDusukTeklif") or ""),
            "en_dusuk_teklif_num": pick_money(
                parse_money_value(s.get("enDusukTeklifDegeri")),
                parse_money(s.get("enDusukTeklif")),
            ),
            "en_yuksek_teklif": str(s.get("enYuksekTeklif") or ""),
            "en_yuksek_teklif_num": pick_money(
                parse_money_value(s.get("enYuksekTeklifDegeri")),
                parse_money(s.get("enYuksekTeklif")),
            ),
            "yaklasik_maliyet": str(s.get("yaklasikMaliyet") or ""),
            "yaklasik_maliyet_num": None,
            "yaklasik_maliyet_kaynak": "",
            "tender_yaklasik_maliyet_num": None,
            "teklif_sayisi": None,
            "gecerli_teklif_sayisi": None,
            "dokuman_indiren_sayisi": None,
            "indirim_orani": None,
            "ekap_ilan_id": "",
            "fesih_string": str(s.get("fesihString") or "")[:255],
            "tasfiye_transfer_string": str(s.get("tasfiyeTransferString") or "")[:255],
            # İngest-kopyası → firma sorguları Tender JOIN'i yapmasın
            "idare_id": tender.idare_id or "",
            "il_id": tender.il_id,
            "ihale_tip": tender.ihale_tip,
            # Benchmark benzerlik merdiveni bunlarla `ekap_tender`'a hiç dokunmaz.
            "okas_ana_kod": tender.okas_ana_kod or "",
            "okas_bucket": tender.okas_bucket or "",
            "en_ust_idare_kod": tender.en_ust_idare_kod or "",
            "sektor": getattr(tender, "sektor", "") or "",
        }

        # 3) Sonuç İlanı — yaklaşık maliyetin TEK doğru kaynağı
        ilan = sonuc_ilanlari.get(key)
        if ilan is not None:
            parsed = parse_sonuc_ilani(ilan.veri_html)
            defaults["ekap_ilan_id"] = ilan.ekap_ilan_id[:64]
            ym = parsed.get("kisim_yaklasik_maliyet")
            if ym is not None:
                defaults["yaklasik_maliyet_num"] = ym
                defaults["yaklasik_maliyet_kaynak"] = "sonuc_ilani"
            for src, dst in (
                ("yaklasik_maliyet", "tender_yaklasik_maliyet_num"),
                ("teklif_sayisi", "teklif_sayisi"),
                ("gecerli_teklif_sayisi", "gecerli_teklif_sayisi"),
                ("dokuman_indiren_sayisi", "dokuman_indiren_sayisi"),
            ):
                if parsed.get(src) is not None:
                    defaults[dst] = parsed[src]
            # ⚠️ Kısımlı ihalede ayrıştırıcı, kısmın maliyeti yerine ihalenin TAMAMININ
            # maliyetini yakalayabiliyor (Sonuç İlanı bazen yalnızca toplam maliyeti
            # yayımlıyor). O zaman `ym` aslında ihale toplamıdır ve tek kısım alan firma
            # için indirim oranı yapay olarak 1'e yaklaşır.
            #
            # Üretimde ölçüldü (220k sözleşme, 2026-08):
            #   çok sözleşmeli + kısım YM == ihale YM → 19.509 satır, ortalama indirim
            #   **0,88**, %86,6'sı >%70. Aynı eşitlik TEK sözleşmeli ihalede meşrudur
            #   (kısım zaten ihalenin kendisi) ve orada aşırı oran yalnızca %2,7.
            #
            # Bu yüzden kısım maliyeti "bilinmiyor" sayılır: `yaklasik_maliyet_num` ve
            # `indirim_orani` NULL bırakılır, `kaynak` boşaltılır. İhalenin toplam
            # maliyeti (`tender_yaklasik_maliyet_num`) DOĞRUdur, o korunur.
            # Yanlış bir sayı göstermektense "veri yok" demek doğrudur — kullanıcı bu
            # orana bakıp teklif fiyatı belirliyor.
            if _kisim_maliyeti_belirsiz(ym, parsed.get("yaklasik_maliyet"), cok_sozlesmeli):
                defaults["yaklasik_maliyet_num"] = None
                defaults["yaklasik_maliyet_kaynak"] = ""
                defaults["indirim_orani"] = None
            else:
                defaults["indirim_orani"] = _indirim_orani(
                    ym, defaults["sozlesme_bedeli_num"]
                )
            if contractor is not None:
                _enrich_contractor(contractor, parsed, ilan)
        elif defaults["sozlesme_tarihi"] is not None:
            # Sonuç İlanı imzadan SONRA yayımlanır → yakın tarihli sözleşmede eksikse
            # detayı daha sık tazelemek gerekir (bkz. should_refresh_detail)
            if (now - defaults["sozlesme_tarihi"]) <= timedelta(days=180):
                sonuc_eksik = True

        wanted[key] = defaults
        kisimlar_by_key[key] = (s.get("kisimItemDto") or {}).get("kisimList") or []

        if contractor is not None:
            touched_contractors.add(contractor.pk)
            # Kısım/ilan yazımları AYNI sözleşmeye ait → firması kesin; bağımsız
            # çözülselerdi yazım farkları mükerrer firma üretirdi.
            kisim_yuklenici = (s.get("kisimItemDto") or {}).get("yuklenici")
            if kisim_yuklenici and kisim_yuklenici.strip() != ham_ad:
                alias_jobs.append(
                    (contractor, kisim_yuklenici, ContractorAlias.Kaynak.KISIM)
                )
            if ilan is not None and ilan.istekli_adi and ilan.istekli_adi != ham_ad:
                alias_jobs.append(
                    (contractor, ilan.istekli_adi, ContractorAlias.Kaynak.ILAN)
                )

    # ⚠️ Budanacak satırların firmaları da "dokunulan" sayılmalı: EKAP bir sözleşmeyi
    # kaldırdığında (ya da başka firmaya geçtiğinde) o firmanın sayaçları düşmeli.
    # Yalnızca yeni listedeki firmaları toplamak, kaybeden firmayı bayat bırakırdı.
    touched_contractors |= set(
        tender.sozlesmeler.exclude(yuklenici__isnull=True)
        .values_list("yuklenici_id", flat=True)
    )

    contracts = _bulk_upsert_children(
        Contract,
        tender.sozlesmeler.all(),
        wanted,
        "ekap_sozlesme_id",
        _CONTRACT_FIELDS,
        # ⚠️ `ilk_gorulme` YALNIZCA burada (yaratma yolunda) yazılır: `build` sadece yeni
        # satırlar için çağrılır. `_CONTRACT_FIELDS`'a EKLENMEMELİ — orada olsaydı her
        # `refresh_stale` turunda güncellenir ve "yeni keşfedilen sözleşme" sinyali
        # anlamını yitirirdi (her sözleşme her gün "yeni" görünürdü).
        lambda key, vals: Contract(
            tender=tender, ekap_sozlesme_id=key, ilk_gorulme=now, **vals
        ),
    )
    for key, contract in contracts.items():
        _upsert_sections(contract, kisimlar_by_key.get(key) or [])
    alias_cakismasi = 0
    for contractor, raw_ad, kaynak in alias_jobs:
        if not contractors_mod.attach_alias(contractor, raw_ad, kaynak):
            alias_cakismasi += 1

    tender_ym = next(
        (c.tender_yaklasik_maliyet_num
         for c in tender.sozlesmeler.all() if c.tender_yaklasik_maliyet_num is not None),
        None,
    )
    # Sözleşme özeti — `contracts` upsert'ten döndüğü için değerler zaten elde;
    # ek sorgu ya da ek `detail_raw` okuması YOK (bkz. Tender.sozlesme_sayisi yorumu).
    bedeller = [
        c.sozlesme_bedeli_num for c in contracts.values()
        if c.sozlesme_bedeli_num is not None
    ]
    Tender.objects.filter(pk=tender.pk).update(
        contractors_synced_at=now,
        sonuc_ilani_eksik=sonuc_eksik,
        yaklasik_maliyet_num=tender_ym,
        sozlesme_sayisi=len(seen_keys),
        # None (bedeli bilinmeyen sözleşmeler) ile 0 (bedeli gerçekten sıfır) farklı →
        # hiç bedel yoksa toplam da NULL kalır, 0 yazılmaz.
        toplam_sozlesme_bedeli=sum(bedeller) if bedeller else None,
    )
    if recompute and touched_contractors:
        contractors_mod.recompute_aggregates(touched_contractors)
    return {
        "contracts": len(seen_keys),
        "contractors": touched_contractors,
        "alias_cakismasi": alias_cakismasi,
    }


_SECTION_FIELDS = ["kisim_adi", "en_dusuk_teklif", "en_yuksek_teklif", "yaklasik_maliyet"]


def _upsert_sections(contract, kisim_list) -> None:
    """Kısımları `ekap_kisim_id` ile upsert eder, artıkları budar."""
    deduped = _dedupe_by_key(
        [k for k in kisim_list if k], lambda k: str(k.get("id", "") or "")
    )
    wanted = {
        (key or f"noid:{idx}"): {
            "kisim_adi": (k.get("kisimAdi") or "")[:500],
            # ⚠️ Bu tutarlar bozuk ölçekte — sayısala çevrilmez, API'de gösterilmez.
            "en_dusuk_teklif": str(k.get("enDusukTeklif") or "")[:100],
            "en_yuksek_teklif": str(k.get("enYuksekTeklif") or "")[:100],
            "yaklasik_maliyet": str(k.get("yaklasikMaliyet") or "")[:100],
        }
        for idx, (key, k) in enumerate(deduped.items())
    }
    _bulk_upsert_children(
        ContractSection,
        contract.kisimlar.all(),
        wanted,
        "ekap_kisim_id",
        _SECTION_FIELDS,
        lambda key, vals: ContractSection(contract=contract, ekap_kisim_id=key, **vals),
    )


def _kisim_maliyeti_belirsiz(kisim_ym, ihale_ym, cok_sozlesmeli: bool) -> bool:
    """
    Ayrıştırılan "kısım maliyeti" aslında ihalenin tamamının maliyeti mi?

    Çok sözleşmeli (kısımlı) bir ihalede kısım maliyeti ile ihale toplamı **birebir
    eşitse**, Sonuç İlanı büyük olasılıkla yalnızca toplam maliyeti yayımlamıştır; o
    değeri tek bir kısma atfetmek indirim oranını 1'e yaklaştırır. Tek sözleşmeli
    ihalede aynı eşitlik meşrudur (kısım = ihalenin kendisi) → orada `False` döner.

    Ayrı fonksiyon: koşul hem canlı senkronda hem `fix_indirim_orani` onarım komutunda
    kullanılıyor, iki yerde ayrı yazılırsa sessizce ayrışırlardı.
    """
    if kisim_ym is None or ihale_ym is None:
        return False
    return cok_sozlesmeli and kisim_ym == ihale_ym


def _indirim_orani(yaklasik_maliyet, sozlesme_bedeli):
    """(YM − bedel) / YM. Yalnızca YM>0 ve oran [-1, 1] aralığındaysa döner."""
    if yaklasik_maliyet is None or sozlesme_bedeli is None or yaklasik_maliyet <= 0:
        return None
    oran = (yaklasik_maliyet - sozlesme_bedeli) / yaklasik_maliyet
    if oran < -1 or oran > 1:
        return None
    return round(oran, 4)


def _enrich_contractor(contractor, parsed, ilan) -> None:
    """Sonuç İlanı'ndan gelen uyruk/adres/il bilgisini firmaya yazar (boşsa doldurur)."""
    from .models import Contractor

    updates = {}
    if parsed.get("uyruk") and not contractor.uyruk:
        updates["uyruk"] = parsed["uyruk"]
    if parsed.get("adres") and not contractor.adres:
        updates["adres"] = parsed["adres"]
    if parsed.get("il_adi") and not contractor.il_adi:
        updates["il_adi"] = parsed["il_adi"]
        updates["il_id"] = parsed.get("il_id")
    if updates:
        Contractor.objects.filter(pk=contractor.pk).update(**updates)
        for k, v in updates.items():
            setattr(contractor, k, v)


# ── Yüksek seviye toplama ──────────────────────────────
def sync_detail(ekap_id, client):
    """
    Bir ihalenin detayını çekip kaydeder.

    Detay response'u zaten `ilanList`'i içerdiği için ayrı `Ilan/GetList` çağrısı
    yapılmaz — bu, ihale başına EKAP çağrısını yarıya indirir (rate limit dostu).
    """
    try:
        detail = client.get_detail(ekap_id)
        return upsert_tender_detail(ekap_id, detail)
    except Exception as e:
        Tender.objects.filter(ekap_id=str(ekap_id)).update(
            sync_status=Tender.SyncStatus.ERROR, sync_error=str(e)[:500]
        )
        raise


def should_refresh_detail(tender, now=None) -> bool:
    """Detayın yeniden çekilmesi gerekip gerekmediğine karar verir (akıllı kural)."""
    now = now or timezone.now()

    # Hiç detay çekilmediyse → evet
    if tender.detail_synced_at is None:
        return True

    # Sonuçlanmış (sözleşme imzalanmış / iptal)
    if tender.ihale_durum in DURUM_SONUCLANMIS:
        # ⚠️ Sonuç İlanları imza tarihinden SONRA yayımlanır → imza anında senkronlanan
        # ihalede ilan yoktur. Bu carve-out olmadan, durum kümesinin düzeltilmesi
        # (bkz. constants.DURUM_SONUCLANMIS) tazelemeyi 1 günden 7 güne yavaşlatıp
        # yaklaşık maliyet verisinin gelmesini fiilen engellerdi.
        if tender.sonuc_ilani_eksik:
            return (now - tender.detail_synced_at) > timedelta(days=2)
        return (now - tender.detail_synced_at) > timedelta(days=7)

    # İhale tarihi gelecekte → detay güncelliği düşük öncelik (liste yeter)
    if tender.ihale_tarihi and tender.ihale_tarihi > now:
        return (now - tender.detail_synced_at) > timedelta(days=3)

    # İhale tarihi son 120 günde geçmiş ve sonuçlanmamış → sık yenile (sonuç yakala)
    if tender.ihale_tarihi and (now - tender.ihale_tarihi) <= timedelta(days=120):
        return (now - tender.detail_synced_at) > timedelta(days=1)

    # Diğerleri → seyrek
    return (now - tender.detail_synced_at) > timedelta(days=14)


def sync_okas(client, take=5000):
    """Tüm OKAS kodlarını çekip upsert eder."""
    resp = client.okas_get_all(take=take)
    items, _ = extract_list(resp)
    count = 0
    for o in items:
        kod = o.get("kod") or o.get("kodu") or o.get("kalemKodu")
        if not kod:
            continue
        adi = o.get("kalemAdi") or o.get("adi") or ""
        OkasCode.objects.update_or_create(
            kod=str(kod),
            defaults={
                "adi": adi,
                "adi_eng": o.get("kalemAdiEng") or "",
                "adi_norm": normalize_tr(adi)[:500],
            },
        )
        count += 1
    return count


# "Bağlantısız Kurumlar" (ağaca bağlı olmayan idareler) EKAP'ta parent sentinel'i
UNCONNECTED_PARENT = -999999


def sync_authorities(client, take=300000):
    """
    DETSIS kurum **ağacını** çekip `Authority` tablosunu tazeler (3 EKAP isteği:
    kökler + tüm bağlı alt düğümler + Bağlantısız Kurumlar). `detsis_no` üzerinden
    toplu upsert eder.

    Alan eşlemesi (bkz. `Authority` docstring): detsisNo→detsis_no,
    parentIdareKimlikKodu→parent_detsis (kök `0`→""), idareId→idare_id (dal
    düğümünde boş), hasItems→has_items, seviye→seviye. `detsisNo` benzersiz olduğu
    için tek anahtardır; aynı düğüm iki istekte de gelirse (kök) upsert son değeri yazar.

    **Bağlantısız Kurumlar** (parent=-999999): belediyelerin Bilgi İşlem
    Müdürlükleri gibi ağaca bağlı OLMAYAN ama **ihale açan** (idareId dolu) idareler.
    EKAP aramasında görünürler; `parent>0` sorgusu bunları kaçırdığı için ayrıca
    çekilir ve sentetik bir "Bağlantısız Kurumlar" kökü altına yerleştirilir.
    """
    roots, _ = extract_list(client.detsis_roots())
    descendants, _ = extract_list(client.detsis_all_descendants(take=take))
    unconnected, _ = extract_list(client.detsis_children(UNCONNECTED_PARENT, take=take))

    seen = set()
    objs = []

    # Sentetik "Bağlantısız Kurumlar" kökü (browse'da görünsün; alt düğümleri aramayla bulunur)
    if unconnected:
        seen.add(str(UNCONNECTED_PARENT))
        objs.append(
            Authority(
                detsis_no=str(UNCONNECTED_PARENT),
                ad="Bağlantısız Kurumlar",
                ad_norm=normalize_tr("Bağlantısız Kurumlar")[:500],
                parent_detsis="",
                idare_id="",
                has_items=True,
                seviye=0,
            )
        )

    for a in [*roots, *descendants, *unconnected]:
        detsis_no = a.get("detsisNo")
        ad = a.get("ad")
        if detsis_no is None or not ad:
            continue
        key = str(detsis_no)
        if key in seen:
            continue
        seen.add(key)
        parent = a.get("parentIdareKimlikKodu")
        idare_id = a.get("idareId")
        objs.append(
            Authority(
                detsis_no=key,
                ad=ad.strip()[:500],
                ad_norm=normalize_tr(ad)[:500],
                parent_detsis="" if parent in (0, None) else str(parent),
                idare_id="" if idare_id is None else str(idare_id),
                has_items=bool(a.get("hasItems")),
                seviye=a.get("seviye"),
            )
        )

    # PostgreSQL upsert (Django 5.1) — tabloyu boşaltmadan yerinde tazele
    Authority.objects.bulk_create(
        objs,
        update_conflicts=True,
        unique_fields=["detsis_no"],
        update_fields=["ad", "ad_norm", "parent_detsis", "idare_id", "has_items", "seviye"],
        batch_size=2000,
    )
    return len(objs)
