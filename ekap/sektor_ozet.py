"""
Sektör özeti — admin ekranının veri katmanı (HTTP'den bağımsız).

`sektor` **kapalı bir taksonomidir** (`constants.SEKTORLER`, 37 etiket) ve ayrı bir
tablosu YOKTUR: `Tender`, `Contract` ve `TenderNamePattern` üzerinde düz
`CharField(32)` olarak durur. Bu bilinçliydi — serbest etiket (a) etiket patlamasına,
(b) indekslenemez kolona, (c) her AI batch'inde farklı isimlendirmeye yol açardı.

⚠️ **Bu yüzden özet MATERYALİZE EDİLMEDİ, canlı hesaplanır.** Ölçüldü (2026-09-22,
tinyfect): `Tender` 212 ms · `TenderNamePattern` 86 ms · `Contract` + bedel 146 ms —
üçü birlikte yarım saniyenin altında. 37 satırlık bir tablo kurup onu tazeleyen bir
görev yazmak, bayatlık ve tutarlılık sorunu eklemekten başka bir şey getirmezdi.
Üç kolon da indekslidir (`ekap_tender_sektor_tarih_idx`,
`ekap_contract_sektor_tarih_idx`).

⚠️ **`ortalama_indirim` `market.MIN_INDIRIM_ORNEK` altında GÖSTERİLMEZ** — projedeki
"örneklemsiz ortalama gösterme" kuralı (bkz. `market._indirim`, `benchmark`).

⚠️ **Sektörsüz satırlar GİZLENMEZ** (`_BOS_KOD`): ihalelerin ~%4,6'sında `sektor` boş
(kalıbı ayırt edici değil ya da henüz işlenmedi). Sessizce düşürmek toplamları yanlış
gösterirdi — pazar panosundaki `okas_bucket=""` kuralının aynısı.
"""
import time

from django.core.cache import cache
from django.db.models import Avg, Count, Sum

from .constants import SEKTORLER
from .market import MIN_INDIRIM_ORNEK

CACHE_ANAHTARI = "ekap:sektor_ozet:v1"
CACHE_TTL = 600

# Boş `sektor` için sentetik kod — SEKTORLER'de böyle bir anahtar yok, çakışmaz.
_BOS_KOD = "__bos__"
_BOS_AD = "(sektörsüz)"


def _sayim(model, alan="sektor"):
    """`{sektor_kodu: adet}` — boş olanlar `_BOS_KOD` altında toplanır."""
    out = {}
    for satir in model.objects.values(alan).annotate(n=Count("*")):
        out[satir[alan] or _BOS_KOD] = satir["n"]
    return out


def ozet(yenile=False):
    """
    Sektör başına kalıp / ihale / sözleşme sayıları ve para özeti.

    Döner: `{"satirlar": [...], "toplam": {...}, "sure_ms": int, "onbellek": bool}`
    """
    if not yenile:
        onbellekli = cache.get(CACHE_ANAHTARI)
        if onbellekli:
            return {**onbellekli, "onbellek": True}

    from .models import Contract, Tender, TenderNamePattern

    basla = time.monotonic()
    kaliplar = _sayim(TenderNamePattern)
    ihaleler = _sayim(Tender)

    # Sözleşme tarafı: sayı + bedel + indirim tek sorguda.
    sozlesmeler = {}
    for satir in (Contract.objects.values("sektor").annotate(
            n=Count("*"),
            bedel=Sum("sozlesme_bedeli_num"),
            indirim=Avg("indirim_orani"),
            indirim_n=Count("indirim_orani"))):
        sozlesmeler[satir["sektor"] or _BOS_KOD] = satir

    adlar = dict(SEKTORLER)
    adlar[_BOS_KOD] = _BOS_AD
    # Üç kaynakta geçen tüm kodlar — SEKTORLER'de olmayan bir kod çıkarsa (eski bir
    # etiket, elle yazılmış değer) gizlenmez, kodu adı olarak gösterilir.
    kodlar = set(adlar) | set(kaliplar) | set(ihaleler) | set(sozlesmeler)

    satirlar = []
    for kod in kodlar:
        soz = sozlesmeler.get(kod) or {}
        n_ind = soz.get("indirim_n") or 0
        satirlar.append({
            "kod": kod,
            "ad": adlar.get(kod, kod),
            "bos_mu": kod == _BOS_KOD,
            "kalip": kaliplar.get(kod, 0),
            "ihale": ihaleler.get(kod, 0),
            "sozlesme": soz.get("n") or 0,
            "bedel": soz.get("bedel"),
            # ⚠️ Örneklem eşiğinin altında değer `None` — "veri yok" demek, uydurma
            # bir ortalama göstermekten doğrudur.
            # ⚠️ `indirim_orani` 0-1 ARALIĞINDA saklanır (0,2165 = %21,65) → yüzdeye
            # çevirme burada yapılır; şablonda çarpma yapılamaz ve ham değer basılsaydı
            # ekranda "%0,2" görünürdü.
            "indirim": (float(soz["indirim"]) * 100
                        if n_ind >= MIN_INDIRIM_ORNEK and soz.get("indirim") is not None
                        else None),
            "indirim_ornek": n_ind,
        })

    toplam_ihale = sum(s["ihale"] for s in satirlar) or 1
    for s in satirlar:
        s["pay"] = round(100 * s["ihale"] / toplam_ihale, 1)

    # Sektörsüz satır en sona; kalanlar ihale sayısına göre.
    satirlar.sort(key=lambda s: (s["bos_mu"], -s["ihale"]))

    veri = {
        "satirlar": satirlar,
        "toplam": {
            "sektor": sum(1 for s in satirlar if not s["bos_mu"] and s["ihale"]),
            "kalip": sum(s["kalip"] for s in satirlar),
            "ihale": sum(s["ihale"] for s in satirlar),
            "sozlesme": sum(s["sozlesme"] for s in satirlar),
            "bedel": sum((s["bedel"] or 0) for s in satirlar),
        },
        "sure_ms": int((time.monotonic() - basla) * 1000),
        "onbellek": False,
    }
    cache.set(CACHE_ANAHTARI, veri, timeout=CACHE_TTL)
    return veri
