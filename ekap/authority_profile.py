"""
İdare (alıcı) profili — "bu kurum ne alıyor, kime, kaça?"

Bugüne kadar ürün yalnızca *ihaleyi* gösteriyordu; alıcıyı hiç göstermiyordu. Oysa
teklif verecek firma için idarenin davranışı en az ihale kadar önemli: yılda ne kadar
harcıyor, hangi kategorilerde, işleri kimler alıyor (yoğunlaşma), ortalama ne indirim
çıkıyor, ne sıklıkla iptal ediyor, ihalelerini yılın hangi ayında açıyor.

## Canlı hesap — neden materialize edilmedi

`ContractorDetailView._dagilim`'in (`ekap/views.py`) gerekçesi burada da aynen geçerli:
tek bir idarenin sözleşmeleri tipik olarak yüzler mertebesindedir ve `Contract` üzerindeki
**ingest-kopyası** `idare_id`/`il_id`/`ihale_tip` sayesinde para tarafı `ekap_tender`
JOIN'i yapmaz. Bu boyutta özet tablo tutmak, tazelik kaybı karşılığında ölçülebilir bir
kazanç vermez.

⚠️ **Ölçek istisnası**: DETSIS alt ağacı (bir bakanlığın tamamı) on binlerce `idare_id`'ye
açılabilir; o `IN` listesi tek başına planlayıcı tahminlerini bozar. Bu yüzden
`PROFILE_MAX_IDARE` sınırı var ve `en_ust_idare_kod` **tek indeksli eşitlik** olarak
tercih edilir (bkz. `kapsam_coz`).

## Dürüstlük

Ortalamalar **her zaman örneklem sayısıyla** döner (`ortalama_indirim` +
`indirim_ornek_sayisi`). Kapsamı kısmi olan alanlarda bu şart: `indirim_orani` yalnızca
Sonuç İlanı ayrıştırılabilen sözleşmelerde, `istekli_sayisi` yalnızca değerlendirmesi
bitmiş ihalelerde dolu. Aynı sözleşme `docs/mobil-yukleniciler.md`'de de yazılı.
"""
import logging

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractMonth, ExtractYear

from .constants import CITIES, DURUM_IPTAL, DURUM_SONUCLANMIS, IHALE_TURU, IHALE_USUL, OZELLIK_MAP
from .models import Contract, Tender

logger = logging.getLogger("ihaletakip")

# Genişletilmiş idare kümesi bunu aşarsa ayrıntılı kırılım hesaplanmaz (yalnızca özet).
# Büyük bakanlıklar on binlerce alt birime açılıyor (MEB ~37k okul) ve o boyuttaki bir
# `IN` listesi GROUP BY'ı kullanılamaz hâle getirir.
PROFILE_MAX_IDARE = 2000

# Yoğunlaşma (HHI) hesabında dikkate alınan azami firma. Aşılırsa `yaklasik: True`.
HHI_MAX_FIRMA = 5000


class Kapsam:
    """
    Profilin hesaplanacağı idare kümesi.

    `tender_q` ile `contract_q` **ayrı** tutulur: `Contract` kendi `idare_id`
    ingest-kopyasını taşıdığı için o kapsamlarda `ekap_tender` JOIN'i yapılmaz
    (840k+ satırda kritik fark), ama `en_ust_idare_kod` henüz `Contract`'a
    kopyalanmadığından bakanlık kapsamı `tender__` üzerinden gitmek zorunda.
    Tek bir Q'dan türetmeye çalışmak (`kosul.children[0]` gibi) kırılgan olurdu.

    ⚠️ TODO (doldurma sonrası): `en_ust_idare_kod` `Contract`'a ingest-kopyası olarak
    taşınınca `contract_q` de düz eşitliğe iner. Şimdi taşınamaz — kaynak kolon boş.
    """

    __slots__ = ("tender_q", "contract_q", "bilgi")

    def __init__(self, tender_q, contract_q, bilgi):
        self.tender_q, self.contract_q, self.bilgi = tender_q, contract_q, bilgi


def kapsam_coz(params):
    """
    İstek parametrelerinden `(Kapsam, hata)` üretir.

    Öncelik sırası bilinçlidir:
      1. `en_ust_idare_kod` — **tek indeksli eşitlik**, en ucuz yol (bakanlık geneli)
      2. `idare_id` — yaprak seçim, doğrudan eşleşme
      3. `idare_detsis` — DETSIS alt ağacı; `descendant_idare_ids` ile genişletilir ve
         **ihalede gerçekten geçen** `idare_id`'lerle kesiştirilir (arama ucuyla ortak
         mantık, bkz. `views.apply_tender_filters`)
    """
    from .detsis_tree import descendant_idare_ids, tender_idare_id_set
    from .models import Authority

    bakanlik = (params.get("en_ust_idare_kod") or "").strip()
    if bakanlik:
        ad = (
            Tender.objects.filter(en_ust_idare_kod=bakanlik).order_by()
            .values_list("en_ust_idare_adi", flat=True).first()
        )
        return Kapsam(
            Q(en_ust_idare_kod=bakanlik),
            Q(en_ust_idare_kod=bakanlik),
            {"tur": "bakanlik", "anahtar": bakanlik, "ad": ad or "", "idare_sayisi": None},
        ), None

    idare_ids = [x.strip() for x in str(params.get("idare_id") or "").split(",") if x.strip()]
    if idare_ids:
        ad = (
            Tender.objects.filter(idare_id=idare_ids[0]).order_by()
            .values_list("idare_adi", flat=True).first()
        ) if len(idare_ids) == 1 else ""
        q = Q(idare_id__in=idare_ids)
        return Kapsam(q, q, {
            "tur": "idare", "anahtar": ",".join(idare_ids), "ad": ad or "",
            "idare_sayisi": len(idare_ids),
        }), None

    detsis = (params.get("idare_detsis") or "").strip()
    if detsis:
        genis = descendant_idare_ids([detsis]) & tender_idare_id_set()
        if not genis:
            return None, "Bu idare (ve alt birimleri) için ihale kaydı bulunamadı."
        ad = (
            Authority.objects.filter(detsis_no=detsis).order_by()
            .values_list("ad", flat=True).first()
        )
        q = Q(idare_id__in=sorted(genis))
        return Kapsam(q, q, {
            "tur": "detsis", "anahtar": detsis, "ad": ad or "", "idare_sayisi": len(genis),
        }), None

    return None, "İdare belirtin: `idare_id`, `idare_detsis` veya `en_ust_idare_kod`."


def _jsonb_var() -> bool:
    """
    JSONB `contains` araması yalnızca PostgreSQL'de var.

    `ozellikler__contains=[...]` (e-ihale payı) SQLite'ta `NotSupportedError` atıyor;
    `DATABASE_URL` yoksa yerel geliştirme SQLite'a düşüyor (bkz. settings) ve uç HER
    istekte 500 verirdi. Üretimde daima PostgreSQL. Aynı yaklaşım
    `aggregates.percentile_destekleniyor`da da var.
    """
    from django.db import connection

    return connection.vendor == "postgresql"


def _oran(pay, payda):
    return round(pay / payda, 4) if payda else None


def _ozet(t_qs, c_qs):
    """Toplam sayılar. ⚠️ `.order_by()` ŞART — Meta.ordering GROUP BY/agregaya sızmasın."""
    t_agg = {
        "n": Count("id"),
        "iptal": Count("id", filter=Q(ihale_durum__in=sorted(DURUM_IPTAL))),
        "sonuclanan": Count("id", filter=Q(ihale_durum__in=sorted(DURUM_SONUCLANMIS))),
        "istekli_toplam": Sum("istekli_sayisi"),
        "istekli_ornek": Count("istekli_sayisi"),
    }
    if _jsonb_var():
        t_agg["e_ihale"] = Count("id", filter=Q(ozellikler__contains=[OZELLIK_MAP["eIhale"]]))
    t = t_qs.order_by().aggregate(**t_agg)
    t.setdefault("e_ihale", None)
    c = c_qs.order_by().aggregate(
        n=Count("id"),
        toplam_bedel=Sum("sozlesme_bedeli_num"),
        indirim_toplam=Sum("indirim_orani"),
        indirim_ornek=Count("indirim_orani"),
        teklif_toplam=Sum("teklif_sayisi"),
        teklif_ornek=Count("teklif_sayisi"),
        firma_sayisi=Count("yuklenici_id", distinct=True),
    )
    return {
        "ihale_sayisi": t["n"],
        "sozlesme_sayisi": c["n"],
        "toplam_bedel": str(c["toplam_bedel"]) if c["toplam_bedel"] is not None else None,
        "farkli_yuklenici_sayisi": c["firma_sayisi"],
        "iptal_orani": _oran(t["iptal"], t["n"]),
        "sonuclanma_orani": _oran(t["sonuclanan"], t["n"]),
        "e_ihale_orani": _oran(t["e_ihale"], t["n"]) if t["e_ihale"] is not None else None,
        # ⚠️⚠️ **İTİRAZ ORANI KALDIRILDI — EKAP bu veriyi bize VERMİYOR.**
        # Kaynak alan `islemlerKuralSeti.itirazenESikayetMi` bir ihale özelliği değil,
        # o an giriş yapmış EKAP kullanıcısına özel bir arayüz bayrağıdır ("BEN itirazen
        # şikâyet ettim mi"). Üçüncü taraf olarak çağırdığımız için kalıcı `false` döner:
        # üretimde 1.043.450 ihalenin **sıfırında** true (ölçüm 2026-08-13).
        # Eskiden bu alan `%0` dönüyordu ve kullanıcı "bu idareye hiç itiraz edilmemiş"
        # sanıyordu — bilinmeyeni "hayır" diye sunmak, bu üründe yanlış sayı göstermenin
        # en kötü türü. `null` + gerekçe döner; istemci "veri yok" gösterir.
        "itiraz_orani": None,
        "itiraz_ornek_sayisi": 0,
        "itiraz_veri_yok_nedeni": (
            "EKAP itirazen şikâyet bilgisini üçüncü taraf çağrılarında paylaşmıyor."
        ),
        # Ortalamalar HER ZAMAN örneklemle birlikte (kapsam kısmi).
        "ortalama_indirim": (
            str(round(c["indirim_toplam"] / c["indirim_ornek"], 4))
            if c["indirim_ornek"] else None
        ),
        "indirim_ornek_sayisi": c["indirim_ornek"],
        "ortalama_teklif_sayisi": (
            round(c["teklif_toplam"] / c["teklif_ornek"], 1) if c["teklif_ornek"] else None
        ),
        "teklif_ornek_sayisi": c["teklif_ornek"],
        "ortalama_istekli_sayisi": (
            round(t["istekli_toplam"] / t["istekli_ornek"], 1) if t["istekli_ornek"] else None
        ),
        "istekli_ornek_sayisi": t["istekli_ornek"],
    }


def _yillara_gore(t_qs, c_qs):
    """Yıl bazlı ihale/sözleşme/harcama. İki taraf ayrı sorgulanıp Python'da birleşir."""
    ihale = {
        r["yil"]: r for r in t_qs.exclude(ihale_tarihi__isnull=True).order_by()
        .annotate(yil=ExtractYear("ihale_tarihi")).values("yil")
        .annotate(adet=Count("id"),
                  iptal=Count("id", filter=Q(ihale_durum__in=sorted(DURUM_IPTAL))))
    }
    sozlesme = {
        r["yil"]: r for r in c_qs.exclude(sozlesme_tarihi__isnull=True).order_by()
        .annotate(yil=ExtractYear("sozlesme_tarihi")).values("yil")
        .annotate(adet=Count("id"), toplam=Sum("sozlesme_bedeli_num"),
                  ind_toplam=Sum("indirim_orani"), ind_ornek=Count("indirim_orani"))
    }
    out = []
    for yil in sorted(set(ihale) | set(sozlesme), reverse=True):
        i, s = ihale.get(yil, {}), sozlesme.get(yil, {})
        out.append({
            "yil": yil,
            "ihale_sayisi": i.get("adet", 0),
            "iptal_sayisi": i.get("iptal", 0),
            "sozlesme_sayisi": s.get("adet", 0),
            "toplam_bedel": str(s["toplam"]) if s.get("toplam") is not None else None,
            "ortalama_indirim": (
                str(round(s["ind_toplam"] / s["ind_ornek"], 4)) if s.get("ind_ornek") else None
            ),
            "indirim_ornek_sayisi": s.get("ind_ornek", 0),
        })
    return out


def _yukleniciler(c_qs, limit=10):
    """
    İşleri kimler alıyor + yoğunlaşma (HHI).

    HHI = Σ(pay²) — 0'a yakın parçalı pazar, 1'e yakın tek firma hâkimiyeti. Bir idarenin
    işlerinin birkaç firmada toplanması, teklif verecek firma için doğrudan sinyaldir.
    """
    satirlar = list(
        c_qs.exclude(yuklenici__isnull=True).order_by()
        .values("yuklenici", "yuklenici__kanonik_ad")
        .annotate(adet=Count("id"), toplam=Sum("sozlesme_bedeli_num"))
        .order_by("-toplam")[:HHI_MAX_FIRMA]
    )
    if not satirlar:
        return {"liste": [], "yogunlasma": None}

    genel = sum((r["toplam"] or 0) for r in satirlar)
    hhi = (
        round(sum(float((r["toplam"] or 0) / genel) ** 2 for r in satirlar), 4)
        if genel else None
    )
    return {
        "liste": [
            {
                "id": r["yuklenici"],
                "ad": r["yuklenici__kanonik_ad"],
                "sozlesme_sayisi": r["adet"],
                "toplam_bedel": str(r["toplam"]) if r["toplam"] is not None else None,
                "pay": (round(float((r["toplam"] or 0) / genel), 4) if genel else None),
            }
            for r in satirlar[:limit]
        ],
        "yogunlasma": {
            "hhi": hhi,
            "ilk5_pay": (
                round(sum(float((r["toplam"] or 0) / genel) for r in satirlar[:5]), 4)
                if genel else None
            ),
            "firma_sayisi": len(satirlar),
            # Firma sayısı tavana dayandıysa HHI tam değil
            "yaklasik": len(satirlar) >= HHI_MAX_FIRMA,
        },
    }


def _kirilimlar(t_qs, c_qs):
    """Usul dağılımı, mevsimsellik, il ve OKAS kırılımı."""
    usul = [
        {"ihale_usul": r["ihale_usul"], "ad": IHALE_USUL.get(r["ihale_usul"], ""),
         "adet": r["adet"]}
        for r in t_qs.exclude(ihale_usul__isnull=True).order_by()
        .values("ihale_usul").annotate(adet=Count("id")).order_by("-adet")
    ]
    tur = [
        {"ihale_tip": r["ihale_tip"], "ad": IHALE_TURU.get(r["ihale_tip"], ""),
         "adet": r["adet"]}
        for r in t_qs.exclude(ihale_tip__isnull=True).order_by()
        .values("ihale_tip").annotate(adet=Count("id")).order_by("-adet")
    ]
    # İhale takvimi: idarenin ihaleleri yılın hangi ayında yoğunlaşıyor?
    # ⚠️ `ilan_tarihi` detay senkronundan dolar; boş olanlar dışlanır.
    mevsim = [
        {"ay": r["ay"], "adet": r["adet"]}
        for r in t_qs.exclude(ilan_tarihi__isnull=True).order_by()
        .annotate(ay=ExtractMonth("ilan_tarihi")).values("ay")
        .annotate(adet=Count("id")).order_by("ay")
    ]
    il_adlari = {ekap_il_id: ad for ekap_il_id, _p, ad, _b in CITIES}
    il = [
        {"il_id": r["il_id"], "ad": il_adlari.get(r["il_id"], ""), "adet": r["adet"],
         "toplam_bedel": str(r["toplam"]) if r["toplam"] is not None else None}
        for r in c_qs.exclude(il_id__isnull=True).order_by()
        .values("il_id").annotate(adet=Count("id"), toplam=Sum("sozlesme_bedeli_num"))
        .order_by("-adet")[:20]
    ]
    # OKAS kırılımı artık `Contract.okas_ana_kod` (ingest-kopyası) üzerinden — JOIN yok.
    # ⚠️ Adı da GROUP BY'a koymak grubu yeniden `ekap_tender` JOIN'ine bağlardı; aynı
    # JOIN benchmark'ta ölçüldü ve 11 GB'lık tabloyu seq scan edip 4,5 sn sürüyordu.
    # Ad çözümü bu yüzden AYRI ve küçük bir sorgu (yalnızca ilk 20 kod).
    okas_satir = list(
        c_qs.exclude(okas_ana_kod="").order_by()
        .values("okas_ana_kod")
        .annotate(adet=Count("id"), toplam=Sum("sozlesme_bedeli_num"))
        .order_by("-toplam")[:20]
    )
    okas_adlari = {}
    if okas_satir:
        for kod, ad in (
            Tender.objects.filter(okas_ana_kod__in=[r["okas_ana_kod"] for r in okas_satir])
            .exclude(okas_ana_adi="")
            .order_by()
            .values_list("okas_ana_kod", "okas_ana_adi")
            .distinct()
        ):
            okas_adlari.setdefault(kod, ad)
    okas = [
        {"okas_ana_kod": r["okas_ana_kod"], "ad": okas_adlari.get(r["okas_ana_kod"], ""),
         "adet": r["adet"],
         "toplam_bedel": str(r["toplam"]) if r["toplam"] is not None else None}
        for r in okas_satir
    ]
    return {"usul": usul, "tur": tur, "mevsimsellik": mevsim, "il": il, "okas": okas}


def profil(params, detay=True):
    """
    İdare profili. Döner: `(veri, hata_mesaji)`.

    `detay=False` ya da kapsam çok genişse ayrıntılı kırılımlar hesaplanmaz.
    """
    kapsam, hata = kapsam_coz(params)
    if hata:
        return None, hata

    t_qs = Tender.objects.filter(kapsam.tender_q)
    c_qs = Contract.objects.filter(kapsam.contract_q)

    cok_genis = (kapsam.bilgi.get("idare_sayisi") or 0) > PROFILE_MAX_IDARE
    veri = {
        "kapsam": {**kapsam.bilgi, "cok_genis": cok_genis},
        "toplam": _ozet(t_qs, c_qs),
        "yillara_gore": _yillara_gore(t_qs, c_qs),
    }
    if detay and not cok_genis:
        veri["yuklenici_dagilim"] = _yukleniciler(c_qs)
        veri["dagilim"] = _kirilimlar(t_qs, c_qs)
    else:
        veri["yuklenici_dagilim"] = None
        veri["dagilim"] = None
        if cok_genis:
            veri["mesaj"] = (
                f"Kapsam {kapsam.bilgi['idare_sayisi']} idareye açıldı; ayrıntılı kırılım "
                "hesaplanmadı. Daha dar bir idare seçin veya `en_ust_idare_kod` kullanın."
            )
    veri["uyari"] = (
        "Ortalama indirim yalnızca Sonuç İlanı yayımlanmış sözleşmelerden, istekli sayısı "
        "yalnızca değerlendirmesi bitmiş ihalelerden hesaplanır — her ortalamanın yanındaki "
        "örneklem sayısına bakın."
    )
    return veri, None
