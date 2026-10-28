"""
İhale tazelemesi — EKAP **v2** üzerinden, durum odaklı (2026-10-27).

⚠️ Neden v2: mobil tik hattı (2 dk'da 1 istek, ~720/gün) hafta içi yeni ihale detayı +
keşifle doluyor; tazeleme 6/gün'e, sonuç ilanı 0'a inmişti (kuyruk 11.245). Ürün
kararı: tazeleme YALNIZCA v2'de, mobile otomatik dönüş YOK (`EKAP_TAZELEME_KAYNAK`).

Kural (ürün kararı):
  • Tazelenen = teklif tarihi GEÇMİŞ ve SONUÇLANMAMIŞ ihale; sıra: takipli (kayıtlı +
    alarm) → hâlâ "Katılıma Açık" (2/NULL) → "Teklif Değerlendirme" (3) → "Değerlendirme
    Tamamlanmış" (4). Teklif tarihinden ≤30 gün → günlük, 30–120 gün → haftalık,
    takipli → günlük. Açık ihale, sonuçlanmış/iptal ve >120 gün HİÇ tazelenmez.
  • Sonuçlanmış (5/15/20) ihalenin sözleşme + sonuç ilanı verisi BİR KEZ çekilir
    (`Tender.sonuc_denendi_at`), sonra bir daha dokunulmaz.

⚠️⚠️ v2 YALNIZCA KRİTİK ALANLARI YAZAR (ürün kararı, 2026-10-27 denemesinden sonra):
durum (+ açıklama, iptal bilgisi) ve sonuçlanmışsa sözleşme + sonuç ilanı. Detayın geri
kalanı (il, idare, ad, OKAS, tarihler, `detail_raw`, `detail_synced_at`) mobilindir ve
ELLENMEZ. İlk deneme tam detayı koruyucu modda yazıyordu: 100 ihalenin 47'sinde il
yanlış yazıldı (v2 il numaralarını değiştirmiş: MUŞ 303→304), idare adı/OKAS ana kodu
kaydı. Kritik olmayan alanı yazmanın ürüne faydası yok, riski var.

⚠️⚠️ v2 yanıtı 2026-10-27'de ölçüldüğü hâliyle (fixture: `ekap/tests/fixtures/v2_detay_*`):
  • İhale `id`'si 64 hex ve HER ÇAĞRIDA DEĞİŞİYOR (eskisi bir süre çalışıyor) →
    `Tender.v2_id` = son görülen; detay olmazsa İKN aramasıyla tazelenip BİR KEZ tekrar.
  • `idareId` 64 hex KARMA → yazılmaz (sayısal `idare_id` = DETSIS/favori idare anahtarı).
  • `idare.enUstIdareKod`, `islemlerKuralSeti`, `tebligatAlanIstekliList`,
    `ihaleIlansizMi` YOK; il numaraları bizim `City.ekap_il_id` ile UYUŞMUYOR.
  • İlanlarda `id` / `sozlesmeId`, sözleşmelerde `id` YOK → kimliksiz ilanlar tek
    anahtara düşüp birbirini eziyor ve sonuç ilanı sözleşmeye bağlanamıyordu (yaklaşık
    maliyet/indirim boş kalıyordu) → içerikten kararlı anahtar + bağlama (`uyarla`).
  • Sözleşme `*Degeri` bedelleri "0.0"; doğru bedel metinde ("818,440.00 TRY") —
    `sync` zaten `parse_money_value(0) → None` → metin sırasıyla doğru okuyor.

⚠️ Çekmeli tek tüketici (fan-out YOK — 2026-08-11 arızası, 218 bin görev): görev turu
sırayla işler, istekler arasında `EKAP_V2_TAZELEME_ARALIK_SN` bekler.
"""
import copy
import hashlib
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Q, When
from django.utils import timezone

from .client import EkapDogrulamaError, EkapError, EkapV2Client
from .constants import DURUM_IPTAL, DURUM_SONUCLANMIS
from .mobil.constants import DURUM_ACIKLAMA
from .mobil.adapt import ILAN_ONEK as MOBIL_ILAN_ONEK
from .models import Contract, Tender
from .sonuc_ilani import parse_sonuc_ilani
from .sync import _as_int, detay_govdesi, extract_list, sync_contracts_from_raw
from .utils import normalize_tr, parse_money, parse_money_value, pick_money

logger = logging.getLogger("ihaletakip")

ILAN_ONEK = "v2:i:"
SOZLESME_ONEK = "v2:s:"
SONUC_DURUMLARI = (5, 15, 20)          # sonuçlanmış ama iptal DEĞİL (6/10'da sonuç verisi yok)

_ISARET = "ekap:v2:taz:"               # geçici hata / bulunamadı işareti (İKN başına)
_ISARET_HATA_TTL = 6 * 3600
_ISARET_YOK_TTL = 7 * 86400
_KILIT = "ekap:v2:taz:kilit"
_SAYAC = "ekap:v2:sayac:"
SAYAC_ADLARI = ("tazeleme", "sonuc", "kimlik", "bulunamadi", "hata")
_ART_ARDA_HATA_TAVANI = 5              # EKAP düştüyse turu boşuna yakma


# ── Sayaçlar (günlük, Redis) ────────────────────────────
_SON_IS = "ekap:v2:son_is"
_SON_HATA = "ekap:v2:son_hata"           # pano: son hatanın kısa metni


def _say(ad, adet=1):
    anahtar = f"{_SAYAC}{ad}:{timezone.localdate():%Y%m%d}"
    try:
        if not cache.add(anahtar, adet, timeout=2 * 86400):
            cache.incr(anahtar, adet)
        if ad in ("tazeleme", "sonuc"):
            cache.set(_SON_IS, timezone.now().isoformat(), 2 * 86400)
    except Exception:                                   # noqa: BLE001
        pass


def son_hata() -> str:
    return cache.get(_SON_HATA) or ""


def son_is():
    """Son başarılı işin zamanı (pano). `v2_tazelendi_at` indekssiz → MAX taraması yapılmaz."""
    from datetime import datetime
    ham = cache.get(_SON_IS)
    return datetime.fromisoformat(ham) if ham else None


def sayaclar() -> dict:
    gun = f"{timezone.localdate():%Y%m%d}"
    return {ad: cache.get(f"{_SAYAC}{ad}:{gun}") or 0 for ad in SAYAC_ADLARI}


# ── Uyarlayıcı ─────────────────────────────────────────
def _karma_mi(deger) -> bool:
    return deger not in (None, "") and not str(deger).isdigit()


def _sozlesme_anahtari(s, idx):
    ham = "|".join([normalize_tr(s.get("yukleniciAdi") or ""), str(s.get("sozlesmeTarih") or ""),
                    str(s.get("sozlesmeBedeli") or "")]).strip("|") or f"konum{idx}"
    return f"{SOZLESME_ONEK}{hashlib.sha1(ham.encode('utf-8')).hexdigest()[:16]}"


def _ilan_anahtari(i, idx):
    ham = "|".join([str(i.get("ilanTip") or ""), str(i.get("ilanTarihi") or ""),
                    i.get("baslik") or "", (i.get("veriHtml") or "")[:500]])
    return f"{ILAN_ONEK}{_as_int(i.get('ilanTip')) or 0}:{hashlib.sha1(ham.encode('utf-8')).hexdigest()[:12]}"


def uyarla(detail) -> dict:
    """2026-10 v2 detay yanıtını `sync`'in beklediği şekle getirir (yeni sözlük döner)."""
    item = copy.deepcopy(detay_govdesi(detail))
    # 1) Karma kimlikler (64 hex, çağrıdan çağrıya değişiyor) atılır.
    item.pop("id", None)
    for kap in (item, item.get("ihaleBilgi") or {}):
        if _karma_mi(kap.get("idareId")):
            kap.pop("idareId", None)
    # 2) Sözleşmeler: içerikten kararlı anahtar. Boş liste → anahtar hiç verilmez
    #    (`sync_contracts_from_raw`'un boş liste dalı satır siler, para özetini sıfırlar).
    sozlesmeler = item.get("sozlesmeBilgiList") or []
    for idx, s in enumerate(sozlesmeler):
        s["id"] = _sozlesme_anahtari(s, idx)
        # İndirim kararı için (`sync._kismi_ihale`); `Tender.kismi_ihale` kolonuna YAZILMAZ.
        s.setdefault("kismiIhale", item.get("kismiIhale"))
    if not sozlesmeler:
        item.pop("sozlesmeBilgiList", None)
    # 3) İlanlar: içerikten kararlı anahtar (kimliksiz ilanlar tek anahtara düşüp
    #    birbirini eziyordu). Sonuç ilanı (tip 4) → sözleşmeye bağ (`sozlesmeId`).
    ilanlar = item.get("ilanList") or []
    for idx, i in enumerate(ilanlar):
        i["id"] = _ilan_anahtari(i, idx)
    _sonuc_ilanlarini_bagla(sozlesmeler, [i for i in ilanlar if _as_int(i.get("ilanTip")) == 4])
    return {"item": item, "_kaynak": "v2"}


def _sonuc_ilanlarini_bagla(sozlesmeler, sonuc_ilanlari):
    """Her sonuç ilanını (kısım başına bir tane) TEK sözleşmeye bağlar (`sozlesmeId`).

    ⚠️ Önce BEDEL: aynı firma iki kısım alınca ünvan iki sözleşmede de geçer ve ünvana
    bakan eşleşme ilanı yanlış kısma bağlıyordu (2. deneme, 2026/1747276: 1.892.160 TL'lik
    ilan 1.728.640 TL'lik sözleşmeye). Sonra tek aday kalan ünvan; tek sözleşmede en yeni
    ilan. Bir ilan iki sözleşmeye verilmez."""
    if not sonuc_ilanlari:
        return
    bilgi = [(i, parse_sonuc_ilani(i.get("veriHtml") or ""),
              normalize_tr(i.get("veriHtml") or "")) for i in sonuc_ilanlari]
    bos = list(range(len(bilgi)))

    def ver(s, aday):
        if len(aday) == 1:
            bos.remove(aday[0])
            bilgi[aday[0]][0]["sozlesmeId"] = s["id"]
            return True
        return False

    kalan = []
    for s in sozlesmeler:
        bedel = pick_money(parse_money_value(s.get("sozlesmeBedeliDegeri")),
                           parse_money(s.get("sozlesmeBedeli")))
        if not (bedel and ver(s, [k for k in bos if bilgi[k][1].get("sozlesme_bedeli") == bedel])):
            kalan.append(s)
    for s in kalan:
        ad = normalize_tr(s.get("yukleniciAdi") or "")[:30]
        ver(s, [k for k in bos if ad and ad in bilgi[k][2]])
    if len(sozlesmeler) == 1 and not any(i.get("sozlesmeId") for i in sonuc_ilanlari):
        # Tek sözleşme: ilan HTML'i ünvanı kısaltmış olabilir → en yeni sonuç ilanı.
        sorted(sonuc_ilanlari, key=lambda i: str(i.get("ilanTarihi") or ""))[-1]["sozlesmeId"] = \
            sozlesmeler[0]["id"]


# ── Aday seçimi ────────────────────────────────────────
def _takip_edilen_iknler():
    from .mobil.tasks import _takip_edilen_iknler as takip
    return takip()


def _isaretli(ikn) -> bool:
    return bool(cache.get(f"{_ISARET}{ikn}"))


def tazeleme_adaylari(simdi=None, takip=None):
    """Vadesi gelmiş tazeleme adayları, öncelik sırasıyla (QuerySet, dilimlenmemiş)."""
    simdi = simdi or timezone.now()
    takip = _takip_edilen_iknler() if takip is None else takip
    gun30, gun120 = simdi - timedelta(days=30), simdi - timedelta(days=120)
    taze = simdi - timedelta(hours=20)
    vade = (Q(v2_tazelendi_at__isnull=True)
            | Q(ihale_tarihi__gte=gun30, v2_tazelendi_at__lt=taze)
            | Q(v2_tazelendi_at__lt=simdi - timedelta(days=7))
            | Q(ikn__in=takip, v2_tazelendi_at__lt=taze))
    return (
        Tender.objects.filter(ihale_tarihi__lte=simdi, ihale_tarihi__gte=gun120)
        # ⚠️ `exclude(__in)` NULL'u KORUR (durumu bilinmeyen ihale tazelenir) — istenen.
        .exclude(ihale_durum__in=DURUM_SONUCLANMIS)
        # Sözleşmesi olan ihale tazelenmez: sözleşme tablosuna yalnızca sonuç toplama dokunur.
        .exclude(Exists(Contract.objects.filter(tender=OuterRef("pk"))))
        .filter(vade)
        .annotate(oncelik=Case(
            When(ikn__in=takip, then=0),
            When(Q(ihale_durum__isnull=True) | Q(ihale_durum__in=(1, 2)), then=1),
            When(ihale_durum=3, then=2),
            default=3, output_field=IntegerField()))
        .order_by("oncelik", F("v2_tazelendi_at").asc(nulls_first=True), "pk")
    )


def sonuc_adaylari(simdi=None):
    """Sözleşme verisi bir kez çekilecek sonuçlanmış ihaleler (yeniden eskiye)."""
    simdi = simdi or timezone.now()
    return (
        Tender.objects.filter(ihale_durum__in=SONUC_DURUMLARI,
                              ihale_tarihi__gte=simdi - timedelta(days=180),
                              sonuc_denendi_at__isnull=True)
        .exclude(Exists(Contract.objects.filter(tender=OuterRef("pk"))))
        .order_by("-ihale_tarihi", "-pk")
    )


_ALANLAR = ("pk", "ikn", "ekap_id", "v2_id", "ihale_durum", "ihale_tarihi", "v2_tazelendi_at")


def _sonraki(simdi):
    """`(tender, sonuc_mu)` — önce tazeleme, sonra sonuç toplama; işaretliler atlanır."""
    for qs, sonuc_mu in ((tazeleme_adaylari(simdi), False), (sonuc_adaylari(simdi), True)):
        for t in qs.only(*_ALANLAR)[:200]:
            if not _isaretli(t.ikn):
                return t, sonuc_mu
    return None, None


# ── Tek ihale ──────────────────────────────────────────
class _Tempo:
    """İstekler arası asgari süre (global v2 throttle'ından AYRI: web istekleri onu paylaşıyor)."""

    def __init__(self, aralik):
        self.aralik, self.son = aralik, None

    def bekle(self):
        if self.son is not None:
            kalan = self.aralik - (time.monotonic() - self.son)
            if kalan > 0:
                time.sleep(kalan)
        self.son = time.monotonic()


def _kimlik_bul(cli, tempo, ikn):
    yil, sayi = ikn.split("/")
    tempo.bekle()
    satirlar, _ = extract_list(cli.search(cli.build_search_body(iknYili=int(yil), iknSayi=int(sayi))))
    _say("kimlik")
    for s in satirlar:
        if str(s.get("ikn")) == ikn and s.get("id"):
            return str(s["id"])
    return None


def _detay_al(cli, tempo, tender):
    """`(detail, v2_id)`; v2'de yoksa `(None, None)`. Saklı kimlik bayatsa BİR KEZ tazelenir."""
    kimlik = tender.v2_id or None
    for deneme in range(2):
        if not kimlik:
            kimlik = _kimlik_bul(cli, tempo, tender.ikn)
            if not kimlik:
                return None, None
        tempo.bekle()
        try:
            detail = cli.get_detail(kimlik)
        except EkapDogrulamaError:
            raise
        except EkapError:
            if deneme == 0 and tender.v2_id:
                kimlik = None
                continue
            raise
        # ⚠️ Kimlik başka ihaleye ait çıkarsa ASLA yazma (başkasının verisi bu satıra düşerdi).
        if str(detay_govdesi(detail).get("ikn") or "") == tender.ikn:
            return detail, kimlik
        kimlik = None
    raise EkapError(f"v2 kimliği İKN ile eşleşmedi: {tender.ikn}")


def _durum_alanlari(tender, item, simdi) -> dict:
    """v2'nin durumundan yazılacak alanlar (yalnızca durum ailesi)."""
    bilgi = item.get("ihaleBilgi") or {}
    yeni = _as_int(item.get("ihaleDurum") or bilgi.get("ihaleDurum"))
    if not yeni:
        return {}
    # v2 detay bazen teklif tarihi geçmiş ihaleyi hâlâ "Katılıma Açık" (2) veriyor, mobil
    # durum taraması aynı ihaleyi 3/4 görmüştü (deneme: 1/100) → durum GERİYE yazılmaz.
    if (yeni == 2 and tender.ihale_durum in (3, 4) and tender.ihale_tarihi
            and tender.ihale_tarihi <= simdi):
        return {}
    # Açıklama mobil adaptörüyle AYNI kaynaktan (kod → metin): iki kaynak aynı kolona
    # farklı metin yazmasın.
    alanlar = {"ihale_durum": yeni,
               "ihale_durum_aciklama": DURUM_ACIKLAMA.get(yeni) or bilgi.get("ihaleDurumAciklama") or ""}
    if yeni in DURUM_IPTAL:
        for alan, anahtar in (("iptal_tarihi", "iptalTarihi"), ("iptal_nedeni", "iptalNedeni"),
                              ("iptal_madde", "iptalMadde")):
            if bilgi.get(anahtar):
                alanlar[alan] = str(bilgi[anahtar])[:Tender._meta.get_field(alan).max_length]
    return alanlar


def _sonuc_yaz(tender, item) -> int:
    """Sözleşme + sonuç ilanı (yalnızca bunlar). Döner: yazılan sözleşme sayısı."""
    sozlesmeler = item.get("sozlesmeBilgiList") or []
    if not sozlesmeler:
        return 0
    sonuc_ilanlari = [i for i in item.get("ilanList") or [] if _as_int(i.get("ilanTip")) == 4]
    if sonuc_ilanlari:
        # Mobilin sonuç ilanı v2'ninkine yer bırakır (iki kez görünmesin).
        tender.ilanlar.filter(ekap_ilan_id__startswith=MOBIL_ILAN_ONEK, ilan_tip=4).delete()
    # ⚠️ Mobil sonuç adımıyla aynı yol (`buda=False`): yalnızca verilen ilan/sözleşme
    # yazılır, mobilin İhale İlanı ve diğer satırlar silinmez.
    ozet = sync_contracts_from_raw(
        tender, detail={"item": {"sozlesmeBilgiList": sozlesmeler, "ilanList": sonuc_ilanlari}},
        recompute=True, buda=False)
    return ozet.get("contracts", 0)


def isle(tender, sonuc_mu, cli=None, tempo=None, simdi=None):
    """Tek ihaleyi v2'den tazeler. Döner: "tazeleme" | "sonuc" | "bulunamadi".

    ⚠️ YALNIZCA durum + (sonuçlanmışsa) sözleşme/sonuç ilanı yazılır — bkz. modül notu."""
    cli = cli or EkapV2Client()
    tempo = tempo or _Tempo(0)
    simdi = simdi or timezone.now()
    detail, kimlik = _detay_al(cli, tempo, tender)
    if detail is None:
        cache.set(f"{_ISARET}{tender.ikn}", "yok", _ISARET_YOK_TTL)
        _say("bulunamadi")
        guncel = {"v2_tazelendi_at": simdi}
        if sonuc_mu:
            guncel["sonuc_denendi_at"] = simdi
        Tender.objects.filter(pk=tender.pk).update(**guncel)
        return "bulunamadi"

    item = uyarla(detail)["item"]
    guncel = {"v2_id": kimlik, "v2_tazelendi_at": simdi, **_durum_alanlari(tender, item, simdi)}
    # Tazeleme sırasında sonuçlandıysa sonuç verisi de şimdi alınır (aynı yanıt, ek istek yok).
    sonuc_al = sonuc_mu or guncel.get("ihale_durum") in SONUC_DURUMLARI
    if sonuc_al:
        guncel["sonuc_denendi_at"] = simdi
    with transaction.atomic():
        Tender.objects.filter(pk=tender.pk).update(**guncel)
        if sonuc_al:
            _sonuc_yaz(Tender.objects.defer("detail_raw", "list_raw").get(pk=tender.pk), item)
    sonuc = "sonuc" if sonuc_mu else "tazeleme"
    _say(sonuc)
    return sonuc


# ── Tur ────────────────────────────────────────────────
def tur(max_sure=None, max_ihale=None):
    """Bir çekmeli tur: süre bütçesi dolana kadar sıradaki ihaleyi işler."""
    max_sure = max_sure or getattr(settings, "EKAP_V2_TAZELEME_SURE", 240)
    tempo = _Tempo(getattr(settings, "EKAP_V2_TAZELEME_ARALIK_SN", 3))
    cli = EkapV2Client()
    bas, sonuclar, art_arda_hata = time.monotonic(), {}, 0
    while time.monotonic() - bas < max_sure:
        if max_ihale is not None and sum(sonuclar.values()) >= max_ihale:
            break
        simdi = timezone.now()
        tender, sonuc_mu = _sonraki(simdi)
        if tender is None:
            sonuclar["is_yok"] = 1
            break
        try:
            ne = isle(tender, sonuc_mu, cli=cli, tempo=tempo, simdi=simdi)
            art_arda_hata = 0
        except EkapDogrulamaError:
            # Doğrulama geri açıldı / düştü → kapı bir sonraki turda durumu yeniden sorar.
            sonuclar["dogrulama"] = 1
            break
        except Exception as e:                          # noqa: BLE001
            logger.warning("v2 tazeleme hata %s: %s", tender.ikn, e)
            cache.set(_SON_HATA, f"{timezone.localtime():%d.%m %H:%M} {tender.ikn}: {e}"[:200],
                      2 * 86400)
            cache.set(f"{_ISARET}{tender.ikn}", "hata", _ISARET_HATA_TTL)
            _say("hata")
            ne = "hata"
            art_arda_hata += 1
            if art_arda_hata >= _ART_ARDA_HATA_TAVANI:
                sonuclar["art_arda_hata"] = 1
                break
        sonuclar[ne] = sonuclar.get(ne, 0) + 1
    return sonuclar


def kilitli_tur(**kw):
    if not cache.add(_KILIT, "1", timeout=getattr(settings, "EKAP_V2_TAZELEME_SURE", 240) + 60):
        return {"atlandi": "kilit"}
    try:
        return tur(**kw)
    finally:
        cache.delete(_KILIT)


# ── Durum (run_ingest --task tazeleme_durum, pano) ─────
def durum_ozeti(simdi=None) -> dict:
    simdi = simdi or timezone.now()
    adaylar = tazeleme_adaylari(simdi)
    # ⚠️ `.order_by()` ŞART: sıralama alanları GROUP BY'a sızıp kademe başına tek satırı bölerdi.
    kademe = dict(adaylar.order_by().values("oncelik").annotate(n=Count("pk"))
                  .values_list("oncelik", "n"))
    gun30 = simdi - timedelta(days=30)
    yakin = (Tender.objects.filter(ihale_tarihi__lte=simdi, ihale_tarihi__gte=gun30)
             .exclude(ihale_durum__in=DURUM_SONUCLANMIS))
    return {
        "bekleyen": {"takipli": kademe.get(0, 0), "katilima_acik": kademe.get(1, 0),
                     "degerlendirme": kademe.get(2, 0), "tamamlanmis": kademe.get(3, 0)},
        "sonuc_kuyrugu": sonuc_adaylari(simdi).count(),
        "yakin_toplam": yakin.count(),
        "yakin_24sa_taze": yakin.filter(v2_tazelendi_at__gte=simdi - timedelta(hours=24)).count(),
        "bugun": sayaclar(),
    }
