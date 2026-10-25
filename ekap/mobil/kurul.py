"""
KİK kurul karar tutanakları — yalnızca **uyuşmazlık** (`kararNitelikKod=2`).

Günde bir kez (`ekap.mobil.tasks.kurul_kontrol`): `Tarihler` (1 istek) → son
`EKAP_MOBIL_KURUL_GERI` toplantı → her biri için `Liste` (1'er istek) → `karar_no` ile
upsert. Geç eklenen kararlar son iki toplantının yeniden okunmasıyla yakalanır; ≈3
istek/gün.

⚠️ `Detay` ucu KULLANILMAZ: listeyle aynı bilgiyi veriyor, `kararAdres`/`kararId` null
(PDF yok — ölçüldü 2026-10-25). Karar başına bir istek boşa bütçe olurdu.
"""
import logging
from datetime import datetime

from django.utils import timezone

from ..models import KurulKarari
from ..utils import normalize_tr
from . import constants as C

logger = logging.getLogger("ihaletakip")


def _an(metin):
    """`"2026-10-21T10:25:09.857"` (saat dilimsiz, TR) → aware datetime."""
    if not metin:
        return None
    try:
        dt = datetime.fromisoformat(str(metin))
    except ValueError:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def _int(deger):
    try:
        return int(deger)
    except (TypeError, ValueError):
        return None


def satirdan(item: dict) -> dict:
    out = _alanlar(item)
    out["arama_norm"] = normalize_tr(" ".join(
        str(item.get(k) or "") for k in ("kararNo", "ihaleAdi", "idareadi", "basvuruSahibi")
    ))
    return out


def _alanlar(item: dict) -> dict:
    return {
        "gundem_maddesi_id": str(item.get("gundemMaddesiId") or "")[:128],
        "nitelik_kod": _int(item.get("kararNitelikKod")) or C.KURUL_NITELIK_UYUSMAZLIK,
        "kurul_yil": _int(item.get("kurulYil")),
        "kurul_sayi": _int(item.get("kurulSayi")),
        "sira_no": _int(item.get("gundemMaddesiSiraNo")),
        "rapor_turu": _int(item.get("raporTuru")),
        "karar_tur_kod": _int(item.get("kararTurKod")),
        # EKAP açıklamanın sonuna boşluk ve virgül bırakıyor ("…reddine,    ").
        "karar_aciklama": (item.get("kararAciklama") or "").strip().rstrip(",").strip(),
        "karsi_oy": (item.get("karsiOyDetay") or "")[:64],
        "karar_tarihi": _an(item.get("kararTarih")),
        "yayin_tarihi": _an(item.get("yayinTarihi")),
        "idare_adi": (item.get("idareadi") or "").strip()[:500],
        "basvuru_sahibi": (item.get("basvuruSahibi") or "").strip()[:500],
        "ihale_adi": (item.get("ihaleAdi") or "").strip(),
        "ikn": (item.get("ikn") or "").strip()[:100],
        "raw": item,
    }


def upsert(satirlar, *, toplanti=None) -> dict:
    """`{"yeni": [karar_no…], "guncel": n}` — `yeni` alarm görevinin girdisidir."""
    tekil = {}
    for item in satirlar or []:
        no = (item.get("kararNo") or "").strip()
        if no:
            tekil[no] = item
    if not tekil:
        return {"yeni": [], "guncel": 0}
    mevcut = {k.karar_no: k for k in KurulKarari.objects.filter(karar_no__in=list(tekil))}
    yeniler, guncel = [], []
    for no, item in tekil.items():
        alanlar = satirdan(item)
        alanlar["toplanti_tarihi"] = toplanti
        obj = mevcut.get(no)
        if obj is None:
            yeniler.append(KurulKarari(karar_no=no, **alanlar))
        else:
            for k, v in alanlar.items():
                setattr(obj, k, v)
            obj.updated_at = timezone.now()
            guncel.append(obj)
    if yeniler:
        KurulKarari.objects.bulk_create(yeniler, ignore_conflicts=True)
    if guncel:
        KurulKarari.objects.bulk_update(
            guncel, list(satirdan({}).keys()) + ["toplanti_tarihi", "updated_at"])
    return {"yeni": [k.karar_no for k in yeniler], "guncel": len(guncel)}


def kontrol(cli, *, geri: int = 2) -> dict:
    """Son `geri` toplantının uyuşmazlık kararlarını çeker. İstek: 1 + geri."""
    tarihler = cli.kurul_tarihler()
    istek, yeni, toplantilar = 1, [], []
    for t in tarihler[:max(0, geri)]:
        satirlar = cli.kurul_liste(t)
        istek += 1
        sonuc = upsert(satirlar, toplanti=_an(t))
        yeni += sonuc["yeni"]
        toplantilar.append({"tarih": t, "karar": len(satirlar), "yeni": len(sonuc["yeni"])})
    return {"istek": istek, "toplanti": toplantilar, "yeni": yeni}
