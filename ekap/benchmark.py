"""
"Kaça verilir?" — benzer ihalelerin kazanan fiyat/indirim dağılımı.

Açık bir ihaleye teklif verecek firmanın gerçekten para ödediği soru budur. Geçmişte
**benzer** işlerin kaça verildiğini, kazanan indirim oranının dağılımını ve beklenen
rekabeti gösterir.

## Benzerlik: tek tanım değil, genişleyen merdiven

Tek sabit tanım ya aşırı daraltır (aynı idare → n=2, istatistik anlamsız) ya aşırı
genişletir (hastane tekstili ile köy yolu aynı torbada). Bu yüzden **örneklem yeterli
olana kadar genişleyen kademeler** kullanılır ve **hangi kademenin cevapladığı yanıtta
döner** — kullanıcı "bu idarede" mi "Türkiye genelinde" mi baktığını bilmeli.

⚠️ Kademe **4b** (`idare_id` + `ihale_tip`, OKAS'sız) şart: üretimde ölçüldü,
**ihalelerin ~%19'unda OKAS kalemi yok**. O ihalelerde kademe 1-4 hiç eşleşmez ve
merdiven doğrudan 4b'den başlar.

## Dürüstlük kuralları (ürün açısından kritik)

- `indirim_orani` yalnızca Sonuç İlanı ayrıştırılabilmiş sözleşmelerde dolu (üretimde
  ölçülen kapsam **%24,5**; yüklenici süpürmesi bitince ~%55'e çıkacak). Bu yüzden her
  ortalama/dağılım **`ornek_sayisi` ile birlikte** döner ve eşik altında `guven` düşürülür.
  Aynı sözleşme `docs/mobil-yukleniciler.md`'de firma ortalamaları için de geçerli.
- **Yıllar arası tek para medyanı verilmez.** TL enflasyonunda 2021 ile 2026 bedelini
  aynı torbaya atmak yanıltıcıdır → `yillara_gore` kırılımı **her zaman** döner ve
  varsayılan pencere 5 yıldır.
- Örneklem yetersizse sayı **uydurulmaz**: `yeterli_veri=False` döner.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Count, Q
from django.db.models.functions import ExtractYear
from django.utils import timezone

from .aggregates import (
    MEDIAN,
    QUARTILES,
    PercentileCont,
    PercentileDisc,
    percentile_destekleniyor,
    yuzdelik_sozluk,
)
from .models import Contract

logger = logging.getLogger("ihaletakip")

# Dağılım göstermek için gereken en az örnek. Altındaysa sayılar döner ama `guven="dusuk"`.
MIN_INDIRIM_ORNEK = 8
MIN_SOZLESME_ORNEK = 10
# Bunun altında dağılım hiç gösterilmez (istatistik değil, anekdot olur).
MUTLAK_MIN_ORNEK = 3

VARSAYILAN_YIL = 5   # ⚠️ 3 değil: 2024-2025 backfill sürerken seyrek, dar pencere deliğe düşer
AZAMI_YIL = 10


class Kademe:
    """Bir benzerlik kademesi: filtre + kullanıcıya gösterilecek etiket."""

    __slots__ = ("ad", "aciklama", "kosul")

    def __init__(self, ad, aciklama, kosul):
        self.ad, self.aciklama, self.kosul = ad, aciklama, kosul


def _keyword_kademesi(tender):
    """
    Anahtar kelime örtüşmesine dayalı kademe — merdivenin EN ALAKALI basamağı.

    OKAS kademeleri "aynı iş kalemi" der ama OKAS ana kodu kabadır ve ihalelerin
    ~%19'unda hiç yoktur. Keyword katmanı bu boşluğu doldurur ve OKAS'ın kuramadığı
    köprüleri kurar — üretim pilotunda ölçüldü (2026-08-28): "mm ebatlı kcal/kg
    tolerans taş kömürü" ile "ısıl değeri kcal/kg yıkanmış elenmiş" ihalelerinin
    ORTAK HİÇBİR KELİMESİ yok, ama ikisi de `tas komur` + `yakit` keyword'lerini
    aldığı için birbirini buluyor.

    İki fazlı, bilinçli olarak:
      1. `ekap_tenderkeyword` üzerinde index-only scan → en benzer ihale id'leri.
      2. `Contract.tender_id IN (...)` → mevcut FK indeksi.
    ⚠️ `ekap_tender`'a HİÇ dokunulmaz; 0014'te kazanılan "tüm kademeler tek tablo"
    özelliği korunur (o JOIN uç başına 4,5 sn maliyet çıkarmıştı).

    ⚠️ Aday sayısı 2000 (300 değil): `_temel_qs` ayrıca tarih ve "bedeli dolu"
    süzgecinden geçiriyor; en benzer 300 ihalenin çoğu sonuçlanmamış olabilir ve
    örneklem `MIN_SOZLESME_ORNEK`in altına düşer.

    Kapalıysa ya da kanıt zayıfsa `None` döner → merdiven eskisi gibi çalışır.
    """
    if not getattr(settings, "KEYWORD_BENCHMARK_ENABLED", False):
        return None
    from . import keywords as kw_mod

    agirliklar = kw_mod.probe_keywordleri(tender.pk)
    # ⚠️ Tek keyword benzerlik değil tesadüftür ("malzeme" ortak olabilir); en az iki
    # bağımsız kanıt aranır.
    if len(agirliklar) < 2:
        return None
    ids = list(kw_mod.benzer_ihale_idleri(
        tender.pk, agirliklar, settings.KEYWORD_BENCHMARK_ADAY))
    if not ids:
        return None
    return Kademe("anahtar", "Benzer işler (ihale adı benzerliği)",
                  Q(tender_id__in=ids))


def _merdiven(tender):
    """
    İhaleye göre benzerlik kademeleri (en alakalıdan en genele).

    ⚠️ `okas_ana_kod` boşsa (ihalelerin ~%19'u) OKAS'lı kademeler hiç üretilmez —
    boş string'le eşleşme "OKAS'ı olmayan tüm ihaleler" demek olurdu, tam bir gürültü.
    """
    okas = tender.okas_ana_kod or ""
    bucket = tender.okas_bucket or ""
    kademeler = []

    # ⚠️ EN BAŞTA: merdiven "dardan geniçe" değil "en alakalıdan en genele" ilerler.
    # Keyword kademesi kapsam olarak `il`'den geniş ama alaka olarak `idare`'den
    # yüksektir. Yeterli örnek bulursa döngü orada durur; bulamazsa OKAS merdiveni
    # HİÇ DEĞİŞMEDEN devreye girer (geri alma: KEYWORD_BENCHMARK_ENABLED=False).
    anahtar = _keyword_kademesi(tender)
    if anahtar is not None:
        kademeler.append(anahtar)

    if okas:
        if tender.idare_id:
            kademeler.append(Kademe(
                "idare", "Aynı idare, aynı iş kalemi",
                Q(okas_ana_kod=okas, idare_id=tender.idare_id),
            ))
        if tender.il_id:
            kademeler.append(Kademe(
                "il", "Aynı il, aynı iş kalemi",
                Q(okas_ana_kod=okas, il_id=tender.il_id),
            ))
        kademeler.append(Kademe(
            "ulke", "Türkiye geneli, aynı iş kalemi",
            Q(okas_ana_kod=okas),
        ))
    # Sektör — OKAS'ı olmayan %19 için `idare_tur`'den çok daha iyi bir geniş kademe.
    # ⚠️ `Contract.sektor` ingest-kopyasıdır (tek değerli, ~36 kardinalite) → JOIN yok.
    if getattr(tender, "sektor", "") and tender.sektor != "diger":
        kademeler.append(Kademe(
            "sektor", "Aynı sektör",
            Q(sektor=tender.sektor),
        ))
    if bucket:
        kademeler.append(Kademe(
            "grup", "Aynı iş grubu",
            Q(okas_bucket=bucket)
            & (Q(ihale_tip=tender.ihale_tip) if tender.ihale_tip else Q()),
        ))
    # 4b — OKAS'sız ihaleler için tek yol (üretimde ~%19).
    if tender.idare_id and tender.ihale_tip:
        kademeler.append(Kademe(
            "idare_tur", "Aynı idare, aynı ihale türü",
            Q(idare_id=tender.idare_id, ihale_tip=tender.ihale_tip),
        ))
    return kademeler


def _temel_qs(tender, kosul, taban_tarih):
    """
    Kademe koşulunu uygulanmış sözleşme queryset'i.

    `idare_id`/`il_id`/`ihale_tip` **`Contract`'ın ingest-kopyası** kolonlarıdır
    (`ekap/models.py`) → o kademelerde `ekap_tender` JOIN'i yapılmaz. `okas_ana_kod`
    ise şimdilik `tender__` üzerinden gider.
    ⚠️ TODO(doldurma sonrası): `Tender.okas_ana_kod` dolduğunda o da `Contract`'a
    ingest-kopyası olarak taşınmalı → tüm kademeler tek tablo olur. Şimdi taşınamaz,
    çünkü kaynak kolon henüz boş (kopyalanacak veri yok).
    """
    return (
        Contract.objects.filter(kosul)
        .filter(sozlesme_tarihi__gte=taban_tarih)
        .exclude(tender_id=tender.pk)            # ihalenin kendisi örnekleme girmesin
        .exclude(sozlesme_bedeli_num__isnull=True)
    )


def _istatistik(qs):
    """
    Tek turda sayımlar + yüzdelikler.

    ⚠️ `.order_by()` ŞART: `Contract.Meta.ordering` aksi hâlde GROUP BY'a sızar
    (`detsis_tree.py`'de yaşanmış hata).
    Yüzdelikler yalnızca PostgreSQL'de eklenir — SQLite'a düşen yerel geliştirmede
    sayım/ortalama yine çalışsın (bkz. `aggregates.percentile_destekleniyor`).
    """
    agregalar = {
        "n": Count("id"),
        "n_indirim": Count("indirim_orani"),       # NOT NULL sayar → gerçek örneklem
        "n_teklif": Count("teklif_sayisi"),
        "ort_indirim": Avg("indirim_orani"),
        "ort_teklif": Avg("teklif_sayisi"),
        "ort_istekli": Avg("tender__istekli_sayisi"),
    }
    if percentile_destekleniyor():
        agregalar["bedel_p"] = PercentileDisc("sozlesme_bedeli_num", QUARTILES)  # para → DISC
        agregalar["indirim_p"] = PercentileCont("indirim_orani", QUARTILES)      # oran → CONT
    sonuc = qs.order_by().aggregate(**agregalar)
    sonuc.setdefault("bedel_p", None)
    sonuc.setdefault("indirim_p", None)
    return sonuc


def _yillara_gore(qs):
    """
    Yıl bazlı medyan + adet. **Her zaman döner** — TL enflasyonunda tek medyan yanıltıcı.
    """
    ek = (
        {"medyan": PercentileDisc("sozlesme_bedeli_num", MEDIAN)}
        if percentile_destekleniyor() else {}
    )
    satirlar = (
        qs.exclude(sozlesme_tarihi__isnull=True)
        .annotate(yil=ExtractYear("sozlesme_tarihi"))
        .values("yil")
        .annotate(adet=Count("id"), n_indirim=Count("indirim_orani"), **ek)
        .order_by("-yil")
    )
    return [
        {
            "yil": r["yil"],
            "adet": r["adet"],
            "medyan": (str(r["medyan"][0]) if r.get("medyan") else None),
            "indirim_ornek_sayisi": r["n_indirim"],
        }
        for r in satirlar
    ]


def _benzerler(qs, limit):
    """
    Karşılaştırma listesi — en yeni sözleşmeler önce.

    ⚠️ `.defer(tender__detail_raw/list_raw)`: `select_related("tender")` olmadan satır
    başına ek sorgu olurdu, `defer` olmadan ise satır başına ~80 KB JSONB TOAST'tan
    açılırdı (bkz. `views._TENDER_BLOB_FIELDS` — aynı hata sözleşme uçlarında yaşandı).
    """
    satirlar = (
        qs.select_related("tender", "yuklenici")
        .defer("tender__detail_raw", "tender__list_raw")
        .order_by("-sozlesme_tarihi")[:limit]
    )
    return [
        {
            "ekap_id": c.tender.ekap_id,
            "ikn": c.tender.ikn,
            "ihale_adi": c.tender.ihale_adi,
            "idare_adi": c.tender.idare_adi,
            "sozlesme_tarihi": c.sozlesme_tarihi.isoformat() if c.sozlesme_tarihi else None,
            "sozlesme_bedeli": str(c.sozlesme_bedeli_num) if c.sozlesme_bedeli_num is not None else None,
            "yaklasik_maliyet": str(c.yaklasik_maliyet_num) if c.yaklasik_maliyet_num is not None else None,
            "indirim_orani": str(c.indirim_orani) if c.indirim_orani is not None else None,
            "teklif_sayisi": c.teklif_sayisi,
            "yuklenici": (
                {"id": c.yuklenici.pk, "ad": c.yuklenici.kanonik_ad} if c.yuklenici else None
            ),
        }
        for c in satirlar
    ]


def _guven(n_indirim, n_sozlesme):
    if n_indirim >= MIN_INDIRIM_ORNEK and n_sozlesme >= MIN_SOZLESME_ORNEK:
        return "yuksek"
    if n_indirim >= MUTLAK_MIN_ORNEK:
        return "orta"
    return "dusuk"


def benchmark(tender, yil_geri=VARSAYILAN_YIL, kapsam="auto", limit=20):
    """
    İhale için benzer işlerin fiyat/indirim/rekabet dağılımı.

    `kapsam="auto"` → merdiven yeterli örnek bulana kadar genişler.
    Belirli bir kademe adı verilirse yalnızca o denenir.

    Döner: `(veri, hata_mesaji)`. `hata_mesaji` doluysa `veri` `None`'dır.
    """
    if tender.detail_synced_at is None:
        return None, "İhalenin detayı henüz alınmadı; benzer iş analizi için detay gerekiyor."

    yil_geri = max(1, min(AZAMI_YIL, int(yil_geri or VARSAYILAN_YIL)))
    taban = timezone.now() - timedelta(days=365 * yil_geri)

    kademeler = _merdiven(tender)
    if kapsam and kapsam != "auto":
        kademeler = [k for k in kademeler if k.ad == kapsam] or kademeler
    if not kademeler:
        return None, (
            "Bu ihale için karşılaştırılabilir bir küme tanımlanamadı "
            "(iş kalemi ve idare bilgisi eksik)."
        )

    secilen = istat = qs = None
    for kademe in kademeler:
        qs = _temel_qs(tender, kademe.kosul, taban)
        istat = _istatistik(qs)
        secilen = kademe
        # Yeterince örnek varsa genişletme; yoksa bir sonraki (daha geniş) kademeye geç.
        if (istat["n_indirim"] >= MIN_INDIRIM_ORNEK
                and istat["n"] >= MIN_SOZLESME_ORNEK):
            break

    n, n_ind = istat["n"], istat["n_indirim"]
    yeterli = n >= MUTLAK_MIN_ORNEK

    veri = {
        "ihale": {
            "ekap_id": tender.ekap_id,
            "ikn": tender.ikn,
            "ihale_adi": tender.ihale_adi,
            "idare_adi": tender.idare_adi,
            "okas_ana_kod": tender.okas_ana_kod or None,
            "okas_ana_adi": tender.okas_ana_adi or None,
            # Dürüstlük: 2+ ise "birincil OKAS" bir yaklaşıklıktır
            "okas_kalem_sayisi": tender.okas_kalem_sayisi,
        },
        "kapsam": {
            "seviye": secilen.ad,
            "aciklama": secilen.aciklama,
            "yil_geri": yil_geri,
            "baslangic": taban.date().isoformat(),
        },
        "ornek": {
            "sozlesme_sayisi": n,
            "indirim_ornek_sayisi": n_ind,
            "teklif_ornek_sayisi": istat["n_teklif"],
            "yeterli_veri": yeterli,
            "guven": _guven(n_ind, n),
        },
        "indirim_orani": yuzdelik_sozluk(istat["indirim_p"], QUARTILES, basamak=4) if n_ind else None,
        "ortalama_indirim_orani": (
            str(round(istat["ort_indirim"], 4)) if istat["ort_indirim"] is not None else None
        ),
        "sozlesme_bedeli": yuzdelik_sozluk(istat["bedel_p"], QUARTILES) if yeterli else None,
        "yillara_gore": _yillara_gore(qs) if yeterli else [],
        "rekabet": {
            "ortalama_teklif_sayisi": (
                round(istat["ort_teklif"], 1) if istat["ort_teklif"] is not None else None
            ),
            "ortalama_istekli_sayisi": (
                round(istat["ort_istekli"], 1) if istat["ort_istekli"] is not None else None
            ),
            "teklif_ornek_sayisi": istat["n_teklif"],
        },
        # Teklif stratejisi uyarısı — bu bayrak varsa en düşük fiyat tek başına kazandırmaz
        "fiyat_disi_unsur_var": tender.fiyat_disi_unsur_var,
        "benzer_ihaleler": _benzerler(qs, limit) if n else [],
        "uyari": _uyari(n, n_ind),
    }
    return veri, None


def _uyari(n, n_indirim):
    if n == 0:
        return "Seçilen pencerede karşılaştırılabilir sonuçlanmış iş bulunamadı."
    if n_indirim == 0:
        return (
            "Bu kümede indirim oranı bilinen sözleşme yok — indirim oranı yalnızca Sonuç "
            "İlanı yayımlanmış sözleşmelerde hesaplanabiliyor."
        )
    if n_indirim < MIN_INDIRIM_ORNEK:
        return (
            f"İndirim oranı yalnızca {n_indirim} sözleşmeden hesaplandı; "
            "temsil gücü sınırlıdır."
        )
    return (
        f"İndirim oranı {n_indirim}/{n} sözleşmeden hesaplandı (yalnızca Sonuç İlanı "
        "yayımlanmış sözleşmelerde bilinir)."
    )
