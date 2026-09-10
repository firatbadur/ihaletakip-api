"""
Mobil payload → **v2 detay şekli** çevirici.

⚠️ **Neden adapter, neden ikinci bir türetme yolu değil:** bu kod tabanının en sert
kurallarından biri "tek çıkarım kaynağı"dır (`sync.apply_pro_fields` docstring'i).
Mobil yanıtları için ayrı bir `upsert` yazılsaydı arşiv (v2 kaynaklı) ile yeni
kayıtlar sessizce farklı semantiğe kayardı. Bu yüzden mobil payload, `sync.py`'nin
beklediği şekle çevrilir; ondan sonrası (`upsert_tender_detail` → `apply_pro_fields`
→ `sync_contracts_from_raw` → `keywords.uygula`) **değişmeden** çalışır.

⚠️ Bedeli: `Tender.detail_raw` artık ham EKAP yanıtı olmayabilir. Bu yüzden sentetik
gövde `{"item": {...}, "_kaynak": "mobil", "_ham": <ham mobil yanıt>}` biçiminde
yazılır — `sync.detay_govdesi(raw)` `raw["item"]` döndürdüğü için tüm mevcut
tüketiciler çalışmaya devam eder ve ham veri de kaybolmaz.
"""
import hashlib
import logging

from . import constants as C
from ..utils import normalize_tr, parse_ekap_datetime

logger = logging.getLogger("ihaletakip")

# ⚠️ Mobil API EKAP'ın iç `id`'sini HİÇBİR uçta vermiyor (ölçüldü: ihale, doküman
# listesi, idari şartname, sonuç ilanı XML'i). `Tender.ekap_id` NOT NULL + unique
# olduğu için sentetik bir kimlik üretilir. Upsert zaten **İKN'ye göre** yapıldığından
# (bkz. `sync.upsert_tender_from_list`) çakışma doğmaz.
SENTETIK_ONEK = "mobil:"
# Mobil kaynaklı sözleşme/ilan satırlarının anahtar öneki. Kaynağı ayırt etmek için
# ayrı bir kolon eklemek yerine anahtarın kendisi işaretlenir.
SOZLESME_ONEK = "mobil:s:"
ILAN_ONEK = "mobil:i:"


def sentetik_mi(ekap_id) -> bool:
    return str(ekap_id or "").startswith(SENTETIK_ONEK)


def ekap_id_coz(ikn: str) -> str:
    """
    İhalenin `ekap_id`'si — v2'den gelen **gerçek** id varsa o korunur.

    ⚠️ Sentetik id'yi koşulsuz yazmak, v2'nin bulduğu gerçek id'yi ezer ve o ihale
    bir daha v2 detay ucundan çekilemez (yedek yol kapanır).
    """
    from ..models import Tender

    mevcut = Tender.objects.filter(ikn=str(ikn)).values_list(
        "ekap_id", flat=True
    ).first()
    if mevcut and not sentetik_mi(mevcut):
        return mevcut
    return f"{SENTETIK_ONEK}{ikn}"


# ── Metin → kod ────────────────────────────────────────
def durum_kodu(metin):
    """
    `ihaleDurumu` metnini `IHALE_DURUM` koduna çevirir; **çeviremezse `None`**.

    ⚠️ Uydurma kod yazmak yasak: `DURUM_SONUCLANMIS` üzerine kurulu her şey
    ("İhale Sonuçlandı" alarmı, sonuçlanmış filtresi, `should_refresh_detail`)
    sessizce bozulurdu. Bilinmeyen metin loglanır ki yeni bir durum eklendiğinde
    fark edelim.
    """
    n = normalize_tr(metin)
    if not n:
        return None
    if n in C.DURUM_METIN:
        return C.DURUM_METIN[n]
    for parca, kod in C.DURUM_PARCA:
        if parca in n:
            return kod
    logger.warning("Bilinmeyen ihale durumu metni: %r", metin)
    return None


def kapsam_tur_usul(metin):
    """
    "4734 Kapsamında - Mal - Açık" → `(yasa_kapsami, ihale_tip, ihale_usul)`.

    Parçalar **sırasız** eşleştirilir: "İstisna - Hizmet - 4734 / 3-g" gibi
    varyantlarda üçüncü parça usul değil madde numarasıdır → o parça hiçbir haritaya
    uymaz ve `None` kalır (yanlış usul yazmaktansa boş bırakılır).
    """
    kapsam = tip = usul = None
    for parca in str(metin or "").split("-"):
        n = normalize_tr(parca)
        if not n:
            continue
        if kapsam is None and n in C.KAPSAM_METIN:
            kapsam = C.KAPSAM_METIN[n]
            continue
        if tip is None and n in C.TUR_METIN:
            tip = C.TUR_METIN[n]
            continue
        if usul is None and n in C.USUL_METIN:
            usul = C.USUL_METIN[n]
    return kapsam, tip, usul


def _as_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ── Liste ──────────────────────────────────────────────
def liste_satirindan(item: dict, ekap_id: str) -> dict:
    """
    Mobil liste satırı → `sync.upsert_tender_from_list`'in beklediği v2 şekli.

    ⚠️ Listede **yalnızca altı alan doludur** (ikn, ihaleAdi, idareAdi, idareIlAdi,
    ihaleTarihi, ihaleTipi); kalanı `null` gelir. `ilanTarihi` YOKTUR → `_LISTE_EZMEZ`
    sayesinde detaydan gelen değer korunur.
    """
    return {
        "id": ekap_id,
        "ikn": item.get("ikn"),
        "ihaleAdi": item.get("ihaleAdi") or "",
        "idareAdi": item.get("idareAdi") or "",
        "ihaleIlAdi": item.get("idareIlAdi") or "",
        "ihaleTarihSaat": item.get("ihaleTarihi") or "",
        "ihaleTip": _as_int(item.get("ihaleTipi")),
    }


# ── Detay ──────────────────────────────────────────────
def detaydan(ikn: str, detay: dict, *, okas_list=None) -> dict:
    """
    Mobil `IhaleArama/Ihale` yanıtı → sentetik v2 detay gövdesi.

    ⚠️ **Anahtar yoksa KONMAZ.** `sync.upsert_tender_detail(koruyucu=True)` bu
    sözleşmeye dayanıyor: bulunmayan anahtar "veri yok" sayılır ve ilgili alan/çocuk
    tablo hiç ellenmez. Boş liste/boş string koymak, v2'nin doldurduğu değeri
    silmek demek olurdu (2026-08-27 `_LISTE_EZMEZ` arızasının aynısı).

    ⚠️ `idareId` **hiç konmaz**: mobil API bu değeri hiçbir uçta vermiyor (ölçüldü) ve
    ad üzerinden eşleştirme reddedildi (2000 ihalede %11,7 YANLIŞ eşleşme). Yanlış
    `idare_id` ihaleyi yanlış kuruma bağlar ve kullanıcı bunu fark edemez.
    """
    kapsam, tip, usul = kapsam_tur_usul(detay.get("ihaleKapsamTurUsul"))
    durum = durum_kodu(detay.get("ihaleDurumu"))

    bilgi = {
        "ikn": ikn,
        "ihaleAdi": detay.get("ihaleAdi") or "",
        "idareAdi": detay.get("idareAdi") or "",
        "ihaleTarihSaat": detay.get("ihaleTarihi") or "",
        "isinYapilacagiYer": detay.get("isinYeri") or "",
        "ihaleYeri": detay.get("ihaleYer") or "",
    }
    if tip is not None:
        bilgi["ihaleTip"] = tip
    if kapsam is not None:
        bilgi["yasaKapsami4734"] = kapsam
    if durum is not None:
        bilgi["ihaleDurum"] = durum
        bilgi["ihaleDurumAciklama"] = (detay.get("ihaleDurumu") or "")[:200]
    if detay.get("ihaleKapsamTurUsul"):
        bilgi["ihaleKapsamAciklama"] = str(detay["ihaleKapsamTurUsul"])[:200]

    item = {
        "ikn": ikn,
        "ihaleAdi": detay.get("ihaleAdi") or "",
        "idareAdi": detay.get("idareAdi") or "",
        "ihaleBilgi": bilgi,
        "idare": _idare(detay),
    }
    if durum is not None:
        item["ihaleDurum"] = durum
    if usul is not None:
        item["ihaleUsul"] = usul

    ilan = _ihale_ilani(detay)
    if ilan:
        item["ilanList"] = [ilan]
    if okas_list:
        item["ihtiyacKalemiOkasList"] = list(okas_list)

    # ⚠️ Ham yanıt korunur: sentetik gövde bir çeviridir, kanıt değil. Bir alan
    # yanlış eşlendiğinde `_ham`'a bakmadan teşhis edilemez.
    return {"item": item, "_kaynak": "mobil", "_ham": detay}


def _idare(detay):
    """
    İdare bloğu — ⚠️ yalnızca **adlar**; `idare_id`/`enUstIdareKod` mobilde YOK.

    `il.id` konmaz: `sync.upsert_tender_detail` `il` sözlüğündeki `id`'yi doğrudan
    `il_id`'ye yazıyor ve mobil il **adı** veriyor. Ad → `City` çözümü liste yolunda
    zaten yapılıyor (`sync._resolve_il_id`), orada bulunamazsa alan boş kalır.
    """
    idare = {}
    il_adi = (detay.get("idareIlAdi") or "").strip()
    if il_adi:
        idare["il"] = {"adi": il_adi.upper()}
        from ..models import City
        sehir = City.objects.filter(ad=il_adi.upper()).values_list(
            "ekap_il_id", flat=True
        ).first()
        if sehir:
            idare["il"]["id"] = sehir
    if detay.get("bagliOlduguEnUstIdare"):
        idare["enUstIdareAdi"] = str(detay["bagliOlduguEnUstIdare"])[:500]
    if detay.get("bagliOlduguIdare"):
        idare["ustIdare"] = str(detay["bagliOlduguIdare"])[:500]
    return idare


def _ihale_ilani(detay):
    """
    `ihaleIlani` → v2 `ilanList` girdisi (`ilanTip=1`).

    ⚠️ **Bu, `Tender.ilan_tarihi`nin tek kaynağıdır** ve filtre / favori idare / OKAS
    bildirimlerinin tamamı o alan üzerinden çalışır (`sync._publish_date_from_ilanlar`
    `ilanTip == 1`i tercih eder).
    """
    ilan = detay.get("ihaleIlani") or {}
    tarih = ilan.get("ilanTarihi") or ""
    html = ilan.get("ilanHtml") or detay.get("ilanHtml") or ""
    if not tarih and not html:
        return None
    if tarih and parse_ekap_datetime(tarih) is None:
        logger.warning("İlan tarihi çözümlenemedi: %r", tarih)
    return {
        # Kararlı anahtar: aynı ilan her turda aynı satıra düşsün.
        "id": f"{ILAN_ONEK}1",
        "ilanTip": 1,
        "ilanTarihi": tarih,
        "veriHtml": html,
        "baslik": (detay.get("ilanSekli") or "İhale İlanı")[:500],
    }


# ── Sonuç ilanları / sözleşmeler ───────────────────────
def sonuc_govdesi(ikn: str, kayitlar: list) -> dict:
    """
    Mobil `SonucIlanlari/Ilan` listesi → sentetik `{item: {ilanList, sozlesmeBilgiList}}`.

    Mobil **kısım/kazanan başına bir kayıt** döndürür; sözleşme tablosunun doğru
    granülerliği budur.

    ⚠️ Para alanları burada ayrıştırılmaz: `sync.sync_contracts_from_raw` sonuç
    ilanının HTML'ini mevcut `parse_sonuc_ilani` ile okuyor ve ölçümde (2024/1362677,
    13 ilan) XML ile **birebir aynı** değerleri üretti. İki ayrı para çıkarımı
    yazmak, tek çıkarım kaynağı kuralının ihlali olurdu.

    XML'den yalnızca HTML'de olmayan iki şey alınır:
      • `IhaleKazanan` → yüklenicinin TAM ünvanı (HTML ayrıştırıcısı adı kesiyor ve
        kesilmiş ad `contractors.canonical_key` üzerinden **mükerrer firma** doğurur),
      • `IhaleKisimYMGosterilsinMi` → kısım maliyeti beyanı (bkz.
        `sync._kisim_maliyeti_belirsiz`).
    """
    from .sonuc_xml import parse as xml_parse

    ilan_list, sozlesme_list = [], []
    for idx, kayit in enumerate(kayitlar or []):
        if not isinstance(kayit, dict):
            continue
        x = xml_parse(kayit.get("ilanXml") or "")
        anahtar = _sozlesme_anahtari(ikn, idx, x, kayit)
        ilan_list.append({
            "id": f"{ILAN_ONEK}s{idx}",
            "ilanTip": 4,
            "ilanTarihi": kayit.get("ilanTarihi") or "",
            "veriHtml": kayit.get("ilanHtml") or "",
            "sozlesmeId": anahtar,
            "istekliAdi": (x.get("yuklenici_adi") or "")[:500],
            "baslik": "Sonuç İlanı",
        })
        sozlesme = {
            "id": anahtar,
            "yukleniciAdi": x.get("yuklenici_adi") or "",
            "sozlesmeTarih": x.get("sozlesme_tarihi") or "",
        }
        bedel = x.get("sozlesme_bedeli")
        if bedel is not None:
            # ⚠️ `*Degeri` alanı **sayısal** olmalı: `utils.parse_money_value` string
            # gördüğünde `None` döner (EKAP'ın bozuk string'lerine karşı bilinçli kural).
            sozlesme["sozlesmeBedeliDegeri"] = float(bedel)
        if "kisim_ym_gosterilsin" in x:
            sozlesme["kisimYMGosterilsinMi"] = x["kisim_ym_gosterilsin"]
        sozlesme_list.append(sozlesme)

    return {
        "item": {"ikn": ikn, "ilanList": ilan_list,
                 "sozlesmeBilgiList": sozlesme_list},
        "_kaynak": "mobil",
    }


def _sozlesme_anahtari(ikn, idx, x, kayit) -> str:
    """
    Kararlı sözleşme anahtarı.

    ⚠️ Mobilde `sozlesmeBilgiList[].id` karşılığı YOK. Anahtar **konumdan** üretilseydi
    (idx) EKAP sıralamayı değiştirdiğinde aynı sözleşme yeni satıra düşer, eskisi
    yetim kalırdı. Bu yüzden içerikten türetilir: kazanan + sözleşme tarihi + bedel.
    Üçü de boşsa son çare konumdur (o zaman da satır en azından sabit kalır).
    """
    parcalar = [
        normalize_tr(x.get("yuklenici_adi") or ""),
        str(x.get("sozlesme_tarihi") or ""),
        str(x.get("sozlesme_bedeli") or ""),
    ]
    ham = "|".join(parcalar).strip("|")
    if not ham:
        ham = f"{ikn}|{idx}|{(kayit.get('ilanTarihi') or '')}"
    return f"{SOZLESME_ONEK}{hashlib.sha1(ham.encode('utf-8')).hexdigest()[:16]}"
