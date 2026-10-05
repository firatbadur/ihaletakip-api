"""
Toplu durum taraması — mobil liste ucunun `ihaleDurumu` filtresiyle.

⚠️ Neden var (2026-10-05): durum yalnızca ihale başına detay isteğiyle tazeleniyordu;
teklif tarihi geçmiş 27.691 ihale "Katılıma Açık" kalmıştı ve tik kapasitesiyle
(~200 tazeleme/gün) eritilmesi aylar sürerdi. Ölçüm (`mobil_probe --is durum`,
`docs/ekap-mobil-api.md`): liste ucu `ihaleDurumu` ile **süzüyor** ve kodlar bizim
`IHALE_DURUM` ile **birebir aynı** (3/4/5/6/15 detay metniyle doğrulandı). Yani bir
(tarih aralığı × tür × durum) dilimi = **1 istek** ile o dilimdeki tüm ihalelerin
durumu öğrenilir. Satırda durum metni YOK — bilgi filtrenin kendisinden okunur.

Yığın `SyncCheckpoint("mobil_durum").extra["yigin"]`, eleman
`[bas_iso, bit_iso, tur, durum, il]`. 250'ye takılan dilim keşifteki gibi önce
tarihte, tek günde ise ilde bölünür.

İki bölge, iki tempo (değişim teklif tarihinden hemen sonra yoğun):
  - **yakın**: son `EKAP_MOBIL_DURUM_YAKIN_GUN` (30) gün → ~günlük
  - **uzak** : 30..`EKAP_MOBIL_DURUM_GERI_GUN` (120) gün → haftalık
"""
import logging
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

from ..constants import DURUM_SONUCLANMIS
from ..models import SyncCheckpoint, Tender
from . import constants as C

logger = logging.getLogger("ihaletakip")

CHECKPOINT = "mobil_durum"
# 15 önce: teklif tarihi geçmiş ihalelerin büyük çoğunluğu (ölçümde 108/135).
DURUMLAR = (15, 6, 5, 4, 3)
TURLER = (1, 2, 3, 4)
_YAKIN_TAZELIK_SAAT = 20
_UZAK_TAZELIK_GUN = 7


def _cp():
    cp, _ = SyncCheckpoint.objects.get_or_create(name=CHECKPOINT)
    if not isinstance(cp.extra, dict):
        cp.extra = {}
    return cp


def _eski_mi(iso, esik):
    if not iso:
        return True
    try:
        from datetime import datetime

        return timezone.now() - datetime.fromisoformat(iso) > esik
    except ValueError:
        return True


def yigin(olustur: bool):
    """Bekleyen dilimler; boşsa ve bir bölgenin vakti geldiyse yeniden kurar."""
    cp = _cp()
    mevcut = cp.extra.get("yigin") or []
    if mevcut or not olustur:
        return mevcut

    bugun = timezone.localdate()
    yakin_gun = getattr(settings, "EKAP_MOBIL_DURUM_YAKIN_GUN", 30)
    geri_gun = getattr(settings, "EKAP_MOBIL_DURUM_GERI_GUN", 120)
    simdi = timezone.now().isoformat()
    yeni = []
    bolgeler = []
    if _eski_mi(cp.extra.get("son_yakin"), timedelta(hours=_YAKIN_TAZELIK_SAAT)):
        bolgeler.append((bugun - timedelta(days=yakin_gun), bugun))
        cp.extra["son_yakin"] = simdi
    if geri_gun > yakin_gun and _eski_mi(cp.extra.get("son_uzak"),
                                         timedelta(days=_UZAK_TAZELIK_GUN)):
        bolgeler.append((bugun - timedelta(days=geri_gun),
                         bugun - timedelta(days=yakin_gun + 1)))
        cp.extra["son_uzak"] = simdi
    for bas, bit in bolgeler:
        for durum in DURUMLAR:
            for tur in TURLER:
                yeni.append([bas.isoformat(), bit.isoformat(), tur, durum, 0])
    if yeni:
        cp.extra["yigin"] = yeni
        cp.save(update_fields=["extra", "updated_at"])
    return yeni


def _yaz(kalan):
    cp = _cp()
    cp.extra["yigin"] = kalan
    cp.save(update_fields=["extra", "updated_at"])


def uygula(iknler, durum: int) -> dict:
    """
    Dilimden dönen İKN'lere durumu yazar. Değişen satır sayısını döner.

    ⚠️ Sonuçlanmış bir ihale toplu yolla sonuçlanmamış bir duruma GERİ ÇEKİLMEZ:
    iki istek arasında durum ilerlemişse eski dilimin cevabı bayattır.
    ⚠️ Teklif tarihi `EKAP_MOBIL_DURUM_ALARM_SESSIZ_GUN`'den eski ihalenin "sonuçlandı"
    geçişi alarm ÜRETMEZ: ilk toplu düzeltme haftalardır bayat kalmış binlerce ihaleyi
    birden çevirecekti ve aylar önce sonuçlanmış bir ihale için bugün "İhale Sonuçlandı"
    demek haber değil, gürültüdür. Yakın ihaleler normal yoldan bildirilir.
    """
    if not iknler:
        return {"degisen": 0}
    qs = Tender.objects.filter(ikn__in=list(iknler)).exclude(ihale_durum=durum)
    if durum not in DURUM_SONUCLANMIS:
        qs = qs.exclude(ihale_durum__in=DURUM_SONUCLANMIS)
    satirlar = list(qs.values_list("pk", "ikn", "ekap_id", "ihale_tarihi"))
    if not satirlar:
        return {"degisen": 0}
    Tender.objects.filter(pk__in=[s[0] for s in satirlar]).update(
        ihale_durum=durum,
        ihale_durum_aciklama=C.DURUM_ACIKLAMA.get(durum, ""),
        updated_at=timezone.now(),
    )

    sessiz = 0
    if durum in DURUM_SONUCLANMIS:
        from tenders.models import TenderAlarm

        esik = timezone.now() - timedelta(
            days=getattr(settings, "EKAP_MOBIL_DURUM_ALARM_SESSIZ_GUN", 14)
        )
        eski = [s for s in satirlar if s[3] and s[3] < esik]
        if eski:
            from django.db.models import Q

            sessiz = TenderAlarm.objects.filter(
                Q(tender_id__in=[s[2] for s in eski]) | Q(tender_ikn__in=[s[1] for s in eski]),
                completed_notified=False,
            ).update(completed_notified=True, last_ihale_durum=durum)
    return {"degisen": len(satirlar), "alarm_sessiz": sessiz}


def adim(cli):
    """Yığından tek dilim çeker (tek istek), durumu yazar, gerekirse böler."""
    kuyruk = yigin(olustur=True)
    if not kuyruk:
        return {"atlandi": "durum_yok"}
    bas_s, bit_s, tur, durum, il = kuyruk[0]
    kalan = kuyruk[1:]
    bas, bit = date.fromisoformat(bas_s), date.fromisoformat(bit_s)

    govde = cli.liste_govdesi(
        ihaleTarihiBaslangic=f"{bas:%Y-%m-%d} 00:00:00",
        ihaleTarihiBitis=f"{bit:%Y-%m-%d} 23:59:59",
        ihaleTuru=tur, ilKod=il or 0, ihaleDurumu=durum,
    )
    veri = cli.liste(govde)
    satirlar = veri if isinstance(veri, list) else []
    iknler = {str(r.get("ikn")) for r in satirlar if r.get("ikn")}
    # ⚠️ Kesilmiş (250) sayfanın İKN'leri de yazılır — o kayıtların durumu kesindir.
    sonuc_ = uygula(iknler, durum)

    bolundu = None
    if len(satirlar) >= C.LISTE_TAVAN:
        if bas < bit:
            orta = bas + (bit - bas) / 2
            kalan = [[bas.isoformat(), orta.isoformat(), tur, durum, il],
                     [(orta + timedelta(days=1)).isoformat(), bit.isoformat(), tur, durum, il]
                     ] + kalan
            bolundu = "tarih"
        elif not il:
            kalan = [[bas_s, bit_s, tur, durum, p] for p in range(1, 82)] + kalan
            bolundu = "il"
        else:
            logger.error("mobil durum tavanı aşılamıyor (%s tür=%s durum=%s il=%s)",
                         bas, tur, durum, il)
    _yaz(kalan)
    return {"is": "durum", "aralik": [bas_s, bit_s], "tur": tur, "durum": durum, "il": il,
            "kayit": len(satirlar), "bolundu": bolundu, "kalan_dilim": len(kalan), **sonuc_}
