"""
EKAP Mobil toplama görevleri — **çekmeli (pull) tek tüketici** modeli.

⚠️ **Neden itmeli (fan-out) değil:** mobil bütçe `ekap` kuyruğunun iki mertebe
altında (~1 istek/2-3 dk vs ~1 istek/sn). Celery'ye binlerce `detay` görevi atmak,
2026-08-11'de ölçülen arızayı tekrar üretirdi: `ekap` kuyruğunda 218.443 görev
birikmişti ama detayı gerçekten eksik ihale 159.801'di — aradaki fark mükerrer,
bayat girdiydi ve günün ihaleleri FIFO'nun sonuna düşüyordu.

Bu modelde kuyruk **daima boştur**: beat her `EKAP_MOBIL_TIK_DK` dakikada bir `tik`
görevini tetikler, `tik` **tek** bir iş seçer (öncelik sırasına göre) ve **tek** istek
harcar. Öncelik her turda yeniden değerlendirilir → bugünün ihalesi asla sıranın
sonunda kalmaz.

Sıra:
  1. **Detay** — `detail_synced_at IS NULL`. `ilan_tarihi` buradan gelir ve
     filtre/favori idare/OKAS bildirimlerinin **tamamı** o alana bağlıdır.
  2. **Keşif** — liste taraması (her 3 turda bir öncelikli, aksi hâlde detay borcu
     keşfi sonsuza dek aç bırakırdı).
  3. **Sonuç ilanı** — para zinciri; günler gecikebilir, bildirim bağlı değil.
  4. **Tazeleme** — `sync.should_refresh_detail` diyen kayıtlar.

⚠️ `SyncRun` satırı yalnızca **keşif turlarında** ve hata durumunda yazılır. Tik 2
dakikada bir koşuyor; her tur için satır yazmak admin'i günde ~720 anlamsız kayıtla
doldururdu (`backfill_tender_fields`'te belgelenen aynı gerekçe). Gerçek teşhis
`Tender.detail_synced_at` / `ilan_tarihi` sayımlarıyla yapılır (`ingest_saglik`).
"""
import logging
from datetime import date, datetime, timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .. import sync as sync_mod
from ..constants import DURUM_SONUCLANMIS
from ..models import SyncCheckpoint, Tender
from ..tasks import _run
from . import adapt, captcha as captcha_mod, constants as C, okas as okas_mod, throttle
from .client import (
    EkapMobilClient,
    MobilButceError,
    MobilCaptchaError,
    MobilError,
    MobilSlotError,
)

logger = logging.getLogger("ihaletakip")

CHECKPOINT = "mobil_kesif"
# İşlenmekte olan ihale işareti — aynı kaydı arka arkaya seçip aynı hataya
# takılmamak için (v2'deki `ekap:detq:` deseninin karşılığı).
_ISARET = "ekap:mobil:is:"
_ISARET_TTL = 3600
_TUR_SAYACI = "ekap:mobil:tur"
# Keşif her N turda bir önceliklidir; aksi hâlde büyük bir detay borcu keşfi
# tümüyle aç bırakır ve yeni ihaleler hiç görünmezdi.
KESIF_PAYI = 3


# ── Sayaçlar (SyncRun yerine) ──────────────────────────
def _say(ad: str, adet: int = 1):
    anahtar = f"ekap:mobil:sayac:{timezone.localdate()}:{ad}"
    try:
        cache.add(anahtar, 0, timeout=36 * 3600)
        cache.incr(anahtar, adet)
    except Exception:                                   # noqa: BLE001
        pass


def sayaclar() -> dict:
    """Pano/sağlık raporu için bugünkü iş sayıları."""
    out = {}
    for ad in ("kesif", "detay", "sonuc", "tazeleme", "hata"):
        try:
            out[ad] = int(cache.get(f"ekap:mobil:sayac:{timezone.localdate()}:{ad}") or 0)
        except Exception:                               # noqa: BLE001
            out[ad] = 0
    return out


def _isaretle(ikn: str) -> bool:
    """İhaleyi 'işleniyor' olarak atomik işaretler; zaten işaretliyse `False`."""
    try:
        return bool(cache.add(f"{_ISARET}{ikn}", "1", timeout=_ISARET_TTL))
    except Exception:                                   # noqa: BLE001
        return True


def _isaret_sil(ikn: str):
    try:
        cache.delete(f"{_ISARET}{ikn}")
    except Exception:                                   # noqa: BLE001
        pass


def _ikn_parcala(ikn: str):
    yil, _, sayi = str(ikn or "").partition("/")
    if not yil.isdigit() or not sayi.isdigit():
        return None, None
    return yil, sayi


# ── Kalp atışı ─────────────────────────────────────────
@shared_task(name="ekap.mobil.tasks.tik")
def tik():
    """Tek tur: en öncelikli işi seçer ve **tek** istek harcar."""
    if not getattr(settings, "EKAP_MOBIL_ENABLED", False):
        return {"atlandi": "kapali"}
    if captcha_mod.bekliyor_mu():
        # ⚠️ Geri çekilme süresi doluyor. Israrla istek atmak soğumayı uzatır.
        return {"atlandi": "captcha_bekliyor"}
    if throttle.butce_kalan() <= 0:
        return {"atlandi": "butce_doldu"}

    try:
        tur = cache.incr(_TUR_SAYACI) if cache.get(_TUR_SAYACI) is not None else (
            cache.set(_TUR_SAYACI, 1, 86400) or 1
        )
    except Exception:                                   # noqa: BLE001
        tur = 1

    cli = EkapMobilClient()
    try:
        return _tur_yap(cli, tur)
    except MobilSlotError:
        return {"atlandi": "hiz_penceresi"}
    except MobilButceError:
        return {"atlandi": "butce_doldu"}
    except MobilCaptchaError as e:
        # ⚠️ Retry YOK: captcha modülü zaten geri çekilme bayrağını koydu ve
        # operatör yolunu açtı.
        logger.warning("mobil tik captcha: %s", e)
        _say("hata")
        return {"atlandi": "captcha"}
    except MobilError as e:
        logger.warning("mobil tik hata: %s", e)
        _say("hata")
        return {"hata": str(e)[:200]}


def _tur_yap(cli, tur: int):
    kesif_hazir = _kesif_yigini(olustur=False)
    detay_ikn = _sirada_detay()

    if kesif_hazir and (detay_ikn is None or tur % KESIF_PAYI == 0):
        return kesif_adimi(cli)
    if detay_ikn:
        return detay(detay_ikn, cli=cli)
    if _kesif_yigini(olustur=True):
        return kesif_adimi(cli)
    sonuc_ikn = _sirada_sonuc()
    if sonuc_ikn:
        return sonuc(sonuc_ikn, cli=cli)
    tazele_ikn = _sirada_tazeleme()
    if tazele_ikn:
        return detay(tazele_ikn, cli=cli, tazeleme=True)
    return {"atlandi": "is_yok"}


# ── Sıra seçimi ────────────────────────────────────────
def _sirada_detay():
    """
    Detayı hiç gelmemiş ihale — **en yeni ihale tarihi önce**.

    ⚠️ Pencere: çok eski kayıtlar mobil bütçeyle kapatılamaz (arşiv v2'den toplandı);
    burada amaç **ileri akış**, arşiv değil.
    """
    taban = timezone.now() - timedelta(days=getattr(settings, "EKAP_MOBIL_DETAY_GERI_GUN", 30))
    qs = Tender.objects.filter(
        detail_synced_at__isnull=True, ihale_tarihi__gte=taban
    ).order_by("-ihale_tarihi").values_list("ikn", flat=True)[:50]
    for ikn in qs:
        if _isaretle(ikn):
            return ikn
    return None


def _sirada_sonuc():
    """Sonuçlanmış ama sözleşmesi henüz yazılmamış ihale."""
    qs = Tender.objects.filter(
        ihale_durum__in=DURUM_SONUCLANMIS, sozlesme_sayisi=0
    ).exclude(ihale_durum__in=(6, 10)).order_by("-ihale_tarihi").values_list(
        "ikn", flat=True
    )[:50]
    for ikn in qs:
        if _isaretle(f"sonuc:{ikn}"):
            return ikn
    return None


def _sirada_tazeleme():
    """`sync.should_refresh_detail` diyen ilk kayıt (SQL'de kabaca, Python'da kesin)."""
    simdi = timezone.now()
    qs = Tender.objects.filter(
        detail_synced_at__lt=simdi - timedelta(days=1),
        ihale_tarihi__gte=simdi - timedelta(days=180),
    ).order_by("detail_synced_at")[:50]
    for t in qs:
        if sync_mod.should_refresh_detail(t, simdi) and _isaretle(t.ikn):
            return t.ikn
    return None


# ── Keşif ──────────────────────────────────────────────
def _kesif_yigini(olustur: bool):
    """
    Keşif iş yığınını okur; boşsa ve süresi geldiyse yeniden doldurur.

    Yığın `SyncCheckpoint("mobil_kesif").extra["yigin"]` içinde durur ve her eleman
    `[baslangic_iso, bitis_iso, ihale_turu]` bir **istek**tir. Sonuç 250'ye takılırsa
    (tavan) aralık ikiye bölünüp yığına geri konur — böylece istek sayısı gerçek
    hacimle orantılı olur, sabit gün×tür ızgarasıyla değil.
    """
    cp, _ = SyncCheckpoint.objects.get_or_create(name=CHECKPOINT)
    extra = cp.extra if isinstance(cp.extra, dict) else {}
    yigin = extra.get("yigin") or []
    if yigin or not olustur:
        return yigin

    son = extra.get("son_tur")
    if son:
        try:
            gecen = (timezone.now() - datetime.fromisoformat(son)).total_seconds()
        except (TypeError, ValueError):
            gecen = 10 ** 9
        if gecen < settings.EKAP_MOBIL_KESIF_ARALIK_DK * 60:
            return []

    bugun = timezone.localdate()
    bas = bugun - timedelta(days=settings.EKAP_MOBIL_KESIF_GERI_GUN)
    bit = bugun + timedelta(days=settings.EKAP_MOBIL_KESIF_ILERI_GUN)
    yigin = [[bas.isoformat(), bit.isoformat(), tur] for tur in C.IHALE_TURU_DILIMLERI]
    extra["yigin"] = yigin
    extra["son_tur"] = timezone.now().isoformat()
    cp.extra = extra
    cp.save(update_fields=["extra", "updated_at"])
    logger.info("mobil keşif turu başladı: %s → %s", bas, bit)
    return yigin


def _yigin_yaz(yigin):
    cp, _ = SyncCheckpoint.objects.get_or_create(name=CHECKPOINT)
    extra = cp.extra if isinstance(cp.extra, dict) else {}
    extra["yigin"] = yigin
    cp.extra = extra
    cp.save(update_fields=["extra", "updated_at"])


@shared_task(name="ekap.mobil.tasks.kesif_adimi")
def kesif_adimi(cli=None):
    """Yığından **tek** dilim çeker, tarar, gerekiyorsa ikiye böler."""
    yigin = _kesif_yigini(olustur=True)
    if not yigin:
        return {"atlandi": "kesif_yok"}
    cli = cli or EkapMobilClient()
    bas_s, bit_s, tur = yigin[0]
    kalan = yigin[1:]
    bas, bit = date.fromisoformat(bas_s), date.fromisoformat(bit_s)

    govde = cli.liste_govdesi(
        ihaleTarihiBaslangic=f"{bas:%Y-%m-%d} 00:00:00",
        ihaleTarihiBitis=f"{bit:%Y-%m-%d} 23:59:59",
        ihaleTuru=tur,
    )
    veri = cli.liste(govde)
    satirlar = veri if isinstance(veri, list) else []

    # ⚠️ Tavan sessizce keser: 250 dönen sorgu **eksiktir**. Aralık bölünebiliyorsa
    # ikiye bölünüp yığına geri konur; tek güne inmişse il kırılımı gerekir.
    if len(satirlar) >= C.LISTE_TAVAN and bas < bit:
        orta = bas + (bit - bas) / 2
        kalan = [[bas.isoformat(), orta.isoformat(), tur],
                 [(orta + timedelta(days=1)).isoformat(), bit.isoformat(), tur]] + kalan
        _yigin_yaz(kalan)
        _say("kesif")
        return {"is": "kesif", "bolundu": [str(bas), str(bit)], "tur": tur}

    yeni, hata = _satirlari_yaz(satirlar)
    _yigin_yaz(kalan)
    _say("kesif")
    if len(satirlar) >= C.LISTE_TAVAN:
        logger.warning(
            "mobil keşif tek günde tavana takıldı (%s tür=%s) — il kırılımı gerekiyor",
            bas, tur,
        )
    return {"is": "kesif", "aralik": [str(bas), str(bit)], "tur": tur,
            "kayit": len(satirlar), "yazilan": yeni, "hata": hata,
            "kalan_dilim": len(kalan)}


def _satirlari_yaz(satirlar):
    yazilan = hata = 0
    for item in satirlar:
        ikn = str(item.get("ikn") or "")
        if not ikn:
            continue
        try:
            v2 = adapt.liste_satirindan(item, adapt.ekap_id_coz(ikn))
            if sync_mod.upsert_tender_from_list(v2):
                yazilan += 1
        except Exception as e:                          # noqa: BLE001
            # Tek bozuk satır turu düşürmesin (v2'deki `_upsert_item_safe` gerekçesi).
            logger.warning("mobil ihale atlandı ikn=%s: %s", ikn, e)
            hata += 1
    return yazilan, hata


# ── Detay ──────────────────────────────────────────────
@shared_task(name="ekap.mobil.tasks.detay")
def detay(ikn, cli=None, tazeleme=False):
    """
    Tek ihalenin detayını çeker: `ilan_tarihi`, durum, kapsam/tür/usul ve OKAS.

    ⚠️ `koruyucu=True` ile yazılır — mobil detayın vermediği alanlar (özellik
    etiketleri, sözleşmeler, tarih listesi) **silinmez**.
    """
    yil, sayi = _ikn_parcala(ikn)
    if not yil:
        return {"hata": f"geçersiz İKN: {ikn}"}
    cli = cli or EkapMobilClient()
    try:
        ham = cli.ihale(yil, sayi)
        if not isinstance(ham, dict):
            return {"hata": "detay JSON değil"}
        # ⚠️ OKAS yalnızca idari şartnamenin içinde var; katalogla kesiştirilir.
        # Bulunamazsa liste HİÇ konmaz → mevcut OKAS kalemleri korunur.
        kalemler = okas_mod.okas_cikar(ham.get("idariSartnameHtml") or "")
        govde = adapt.detaydan(ikn, ham, okas_list=kalemler)
        tender = sync_mod.upsert_tender_detail(
            adapt.ekap_id_coz(ikn), govde, koruyucu=True
        )
        Tender.objects.filter(pk=tender.pk).update(detay_kaynak="mobil")
        _say("tazeleme" if tazeleme else "detay")
        return {"is": "detay", "ikn": ikn, "okas": len(kalemler),
                "ilan_tarihi": str(tender.ilan_tarihi or "")}
    finally:
        _isaret_sil(ikn)


# ── Sonuç ilanları ─────────────────────────────────────
@shared_task(name="ekap.mobil.tasks.sonuc")
def sonuc(ikn, cli=None):
    """
    Sonuç ilanlarını çeker → `Announcement` + `Contract` + yüklenici bağlantısı.

    ⚠️ v2 zaten sözleşme yazmışsa **dokunulmaz**: v2 kaydı daha zengindir (kısım
    listesi, `*Degeri` float'ları) ve mobil satırları eklemek mükerrer sözleşme
    doğururdu. Ayırt etme anahtar önekiyle yapılır (`adapt.SOZLESME_ONEK`), ayrı
    bir kolona gerek yok.
    """
    yil, sayi = _ikn_parcala(ikn)
    if not yil:
        return {"hata": f"geçersiz İKN: {ikn}"}
    tender = Tender.objects.filter(ikn=ikn).first()
    if tender is None:
        _isaret_sil(f"sonuc:{ikn}")
        return {"hata": "ihale yok"}
    if tender.sozlesmeler.exclude(
        ekap_sozlesme_id__startswith=adapt.SOZLESME_ONEK
    ).exists():
        _isaret_sil(f"sonuc:{ikn}")
        return {"atlandi": "v2_sozlesmesi_var"}

    cli = cli or EkapMobilClient()
    try:
        ham = cli.sonuc_ilanlari(yil, sayi)
        kayitlar = ham if isinstance(ham, list) else []
        if not kayitlar:
            # Sonuç ilanı henüz yayımlanmamış olabilir (imzadan aylar sonra çıkıyor).
            _say("sonuc")
            return {"is": "sonuc", "ikn": ikn, "kayit": 0}
        govde = adapt.sonuc_govdesi(ikn, kayitlar)
        # ⚠️ `buda=False`: mobil yalnızca sonuç ilanlarını görüyor; budamak detay
        # turunun yazdığı İhale İlanı satırını silerdi.
        ozet = sync_mod.sync_contracts_from_raw(
            tender, detail=govde, recompute=True, buda=False
        )
        _say("sonuc")
        return {"is": "sonuc", "ikn": ikn, "kayit": len(kayitlar),
                "sozlesme": ozet.get("contracts")}
    finally:
        _isaret_sil(f"sonuc:{ikn}")


# ── Elle tam tur (komut için) ──────────────────────────
@shared_task(name="ekap.mobil.tasks.kesif_turu")
def kesif_turu(max_istek=200):
    """Keşif yığınını bitene kadar işler — `run_mobil --is kesif` bunu çağırır."""
    with _run("mobil_kesif", lock_ttl=2 * 3600) as run:
        if run is None:
            return {"atlandi": "kilit"}
        cli = EkapMobilClient()
        toplam = yazilan = 0
        for _ in range(max_istek):
            sonuc_ = kesif_adimi(cli)
            if sonuc_.get("atlandi"):
                break
            toplam += sonuc_.get("kayit", 0)
            yazilan += sonuc_.get("yazilan", 0)
            if not sonuc_.get("kalan_dilim"):
                break
        run.items = yazilan
        run.note = f"taranan={toplam}"
        return {"taranan": toplam, "yazilan": yazilan}
