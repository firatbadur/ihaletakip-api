"""
EKAP → DB eşleme ve toplama (ingest) mantığı.

Celery görevleri (`tasks.py`) ve yönetim komutları bu fonksiyonları çağırır.
Response şekilleri mobil ekran tüketiminden çıkarıldı; alanlar savunmacı `.get`
ile okunur ve tam ham JSON `detail_raw`/`list_raw`'da saklanır.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from .constants import DURUM_SONUCLANMIS
from .models import (
    Announcement,
    Authority,
    City,
    Contract,
    ContractSection,
    OkasCode,
    OkasItem,
    Tender,
    TenderDate,
)
from .utils import parse_ekap_datetime, parse_money

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


def upsert_tender_detail(ekap_id, detail, announcements=None) -> Tender:
    """Detay response'unu Tender + çocuk tablolara yazar."""
    data = detail.get("item", detail) if isinstance(detail, dict) else {}
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
        OkasItem(tender=tender, kodu=str(o.get("kodu", "")), adi=o.get("adi", "") or "")
        for o in (data.get("ihtiyacKalemiOkasList") or [])
    ])

    # İlanlar (detay içi ilanList + varsa ayrı announcements response)
    ilan_list = list(data.get("ilanList") or [])
    if announcements:
        extra, _ = extract_list(announcements)
        ilan_list += extra
    tender.ilanlar.all().delete()
    Announcement.objects.bulk_create([
        Announcement(
            tender=tender,
            ekap_ilan_id=str(i.get("id", "")),
            ilan_tip=_as_int(i.get("ilanTip")),
            ilan_tarihi=parse_ekap_datetime(i.get("ilanTarihi")),
            baslik=i.get("baslik", "") or "",
            veri_html=i.get("veriHtml", "") or "",
            istekli_adi=i.get("istekliAdi", "") or "",
        )
        for i in ilan_list
    ])

    # Sözleşmeler + kısımlar
    tender.sozlesmeler.all().delete()
    for s in (data.get("sozlesmeBilgiList") or []):
        contract = Contract.objects.create(
            tender=tender,
            yuklenici_adi=s.get("yukleniciAdi", "") or "",
            sozlesme_tarih=str(s.get("sozlesmeTarih") or ""),
            sozlesme_bedeli=str(s.get("sozlesmeBedeli") or ""),
            sozlesme_bedeli_num=parse_money(s.get("sozlesmeBedeli")),
            en_dusuk_teklif=str(s.get("enDusukTeklif") or ""),
            en_dusuk_teklif_num=parse_money(s.get("enDusukTeklif")),
            en_yuksek_teklif=str(s.get("enYuksekTeklif") or ""),
            en_yuksek_teklif_num=parse_money(s.get("enYuksekTeklif")),
            yaklasik_maliyet=str(s.get("yaklasikMaliyet") or ""),
            yaklasik_maliyet_num=parse_money(s.get("yaklasikMaliyet")),
            fesih_string=str(s.get("fesihString") or ""),
            tasfiye_transfer_string=str(s.get("tasfiyeTransferString") or ""),
        )
        kisim_list = ((s.get("kisimItemDto") or {}).get("kisimList")) or []
        ContractSection.objects.bulk_create([
            ContractSection(
                contract=contract,
                kisim_adi=k.get("kisimAdi", "") or "",
                en_dusuk_teklif=str(k.get("enDusukTeklif") or ""),
                en_yuksek_teklif=str(k.get("enYuksekTeklif") or ""),
                yaklasik_maliyet=str(k.get("yaklasikMaliyet") or ""),
            )
            for k in kisim_list
        ])


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

    # Sonuçlanmış (iptal/sonuç/sözleşme) ve son 7 günde bakılmış → hayır
    if tender.ihale_durum in DURUM_SONUCLANMIS:
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
        OkasCode.objects.update_or_create(
            kod=str(kod),
            defaults={
                "adi": o.get("kalemAdi") or o.get("adi") or "",
                "adi_eng": o.get("kalemAdiEng") or "",
            },
        )
        count += 1
    return count


def sync_authorities(client, take=5000):
    """DETSIS kurum ağacını çekip upsert eder."""
    resp = client.detsis_agaci(take=take)
    items, _ = extract_list(resp)
    count = 0
    for a in items:
        detsis_id = a.get("id") or a.get("detsisNo") or a.get("detsisId")
        ad = a.get("ad") or a.get("adi")
        if detsis_id is None or not ad:
            continue
        Authority.objects.update_or_create(
            detsis_id=str(detsis_id),
            defaults={
                "ad": ad.strip(),
                "ust_idare": a.get("ustIdare") or a.get("ustIdareAdi") or "",
                # Filtre `idareKodList` bu id ile eşleşir
                "idare_kod": str(a.get("idareId") or a.get("id") or ""),
            },
        )
        count += 1
    return count
