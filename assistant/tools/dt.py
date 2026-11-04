"""
Doğrudan temin (DT) aracı.

Neden var: asistan DT sorularına "doğrudan temin ilansızdır, EKAP verisinde yok"
diyordu — oysa uygulamanın Doğrudan sekmesi binlerce açık alımı listeliyor. Bir
kullanıcı bunun üzerine "sistem görmüyorsa uygulamayı kullanmama gerek kalmıyor"
yazdı (2026-10-23, sohbet analizi 2026-11-04).

⚠️ "Açık" tanımı ve filtreler `ekap.dt_sorgu`'dan gelir — liste ucu ve DT alarm görevi
de aynı modülü kullanıyor; üçüncü bir tanım yazmak sayıların ayrışması demektir.
⚠️ Yalnızca KAYITLI veri okunur: asistan EKAP'ı tetiklemez (detay/doküman mobilin
kullanıcı belge bütçesini harcar — `views_dt`'deki bot kuralının aynı gerekçesi).
"""
import logging

from django.utils import timezone

from .trim import butceye_sigdir, kes

logger = logging.getLogger("ihaletakip")

AZAMI_DT = 15


def dogrudan_temin_ara(ctx, q=None, il_id=None, tur=None, son_teklif_max=None,
                       sadece_acik=True, order=None, page_size=None, **_):
    from ekap import dt_sorgu
    from ekap.mobil.constants import DT_TUR
    from ekap.models import DogrudanTemin

    params = {"q": q, "il_id": il_id, "tur": tur, "son_teklif_max": son_teklif_max}
    try:
        qs = dt_sorgu.acik_qs() if sadece_acik is not False else DogrudanTemin.objects.all()
        qs = dt_sorgu.filtrele(qs, params)
        toplam = qs.count()
        qs = dt_sorgu.sirala(qs, "yeni" if order == "yeni" else "son_teklif")
        n = min(int(page_size or 10), AZAMI_DT)
        satirlar = list(qs.only("dtn", "ad", "idare_adi", "il_adi", "tur", "madde",
                                "son_teklif", "ilan_tarihi", "dokuman_var", "durum_metin")[:n])
    except Exception:
        logger.exception("dogrudan_temin_ara başarısız: %s", params)
        return {"ok": False, "hata": "Doğrudan temin araması sırasında bir sorun oldu."}

    liste = [
        {
            "dtn": d.dtn,
            "ad": kes(d.ad, 140),
            "idare_adi": kes(d.idare_adi, 90),
            "il": d.il_adi or None,
            "tur": DT_TUR.get(d.tur),
            "madde": d.madde or None,
            "son_teklif": (timezone.localtime(d.son_teklif).strftime("%d.%m.%Y %H:%M")
                           if d.son_teklif else None),
            "ilan_tarihi": d.ilan_tarihi.strftime("%d.%m.%Y") if d.ilan_tarihi else None,
            "dokuman_var": d.dokuman_var,
            "durum": d.durum_metin or None,
        }
        for d in satirlar
    ]
    sonuc = {
        "ok": True,
        "toplam": toplam,
        "liste": liste,
        "not": (
            "Bunlar uygulamanın Doğrudan sekmesindeki alımlardır; kullanıcı oradan açıp "
            "kaydedebilir, filtresini kaydedip alarm kurabilir (alarm Pro). DTN numarasını "
            "yaz. Arşiv tutulmaz: son teklif tarihi 30 günden eski alımlar listede yoktur."
        ),
    }
    if not liste:
        sonuc["not"] += " Bu filtreyle şu an kayıt yok; terimi genelleştir ya da il kısıtını kaldır."
    return butceye_sigdir(sonuc)
