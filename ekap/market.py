"""
Pazar panosu (Adım 6) — "kamu bu yıl neye ne kadar harcadı, kim kazandı?"

HTTP'den bağımsız; `views.py` yalnızca sarmalar (benchmark.py / authority_profile.py
ile aynı desen).

## Materialize / canlı ayrımı — ÖLÇÜMLE belirlendi

Plan "materialize, kesin" diyordu ama o karar veri dolmadan verilmişti. Prod ölçümü
(2026-08-13, 1.407.379 sözleşme / 1 GB):

| Sorgu şekli                              | Süre     | Karar      |
|------------------------------------------|----------|------------|
| pano ana ekranı (yıl → iş grubu)         | 1.488 ms | materialize|
| yıl özeti (`count(DISTINCT firma)` ile)  |   668 ms | materialize|
| tek grubun yıllara göre seyri            |   419 ms | materialize|
| firma kırılımı (yıl + grup)              |   103 ms | **canlı**  |
| il kırılımı (yıl + grup)                 |    85 ms | **canlı**  |

Ayrım nettir: **yıl boyunu tarayan** sorgular yavaş (bir yıl ≈ 161 bin satır heap),
**iş grubuyla daraltılanlar** hızlı (indeksten dar aralık). Bu yüzden yalnızca ilk üçü
materialize edildi; il/firma kırılımları canlı hesaplanır. Materialize tablo bedava
değil (yenileme görevi, bayatlık, toplanabilirlik kuralları) — gerekmeyeni yazmadık.

## Toplanabilirlik (plan R10 — API dokümanına da yazılacak)

- **Toplanabilir**: `sozlesme_sayisi`, `toplam_bedel`, `indirim_toplam`,
  `indirim_ornek`, `teklif_toplam`, `teklif_ornek`.
  Ağırlıklı ortalama indirim = `Σindirim_toplam / Σindirim_ornek` — **kesindir**.
- **Toplanamaz**: `count(DISTINCT firma/idare)`, medyanlar. Bu yüzden tekil sayılar
  `MarketYearStat`'ta kendi kesin grain'inde durur, `MarketStat`'ta hiç yoktur.
"""
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from .models import Contract, MarketStat, MarketYearStat, OkasCode, Tender

# Yoğunlaşma (HHI) hesabı için firma tavanı: aşılırsa sonuç `yaklasik: True` döner.
# Kesilmiş bir küme üzerinden hesaplanan HHI tam değildir; sessizce vermek yanlış olur.
HHI_MAX_FIRMA = 5000
# Pano listelerinin varsayılan/azami uzunluğu
VARSAYILAN_LIMIT = 20
MAX_LIMIT = 100
# "Sınıflandırılmamış" grubun kullanıcıya görünen adı. ⚠️ `okas_bucket=""` GERÇEK
# veridir (sözleşmelerin ~%15'i); sessizce düşürmek pazar toplamlarını bozar.
SINIFLANDIRILMAMIS = "Sınıflandırılmamış"

# ── İndirim ortalamasının güven eşikleri ──────────────────────────────────────
# ⚠️ **Neden gerekli** (prod'da yakalandı, 2026-08-13): Sonuç İlanı — `indirim_orani`nin
# tek kaynağı — imzadan **aylar sonra** yayımlanır. Bu yüzden en güncel yılda kapsam
# neredeyse sıfırdır: 2026'da 82.687 sözleşmenin yalnızca 1.374'ünde (%1,7) indirim
# biliniyordu ve tek tek gruplarda durum daha kötüydü — "3.192 sözleşme · ortalama
# indirim %54,8 (n=6)". Altı örnekten çıkan bir manşet sayı YANLIŞTIR.
# Kod tabanının kuralı nettir: **yanlış sayı göstermektense "veri yok" demek doğrudur**
# (aynı ilke `indirim_orani` ölçek hatasında ve "ornek_sayisi olmadan ortalama gösterme"
# kuralında da geçerli).
# ⚠️ **İki kapı: mutlak örneklem tabanı VE kapsam oranı.** Üretim verisiyle (2026-08-13)
# kalibre edildi — iki kez yanlış ayarladım, üçüncüde ölçtüm:
#
#   yıl  | kapsam | ort. indirim        Olgun yıllar (%32+ kapsam) 0,17-0,24 dar
#   -----|--------|-------------        bandında. %1,7 kapsamlı 2026 ise 0,3894 —
#   2026 |   1,7% | 0,3894  ← BOZUK     bandın tepesinin iki katı. Seçim yanlılığı
#   2025 |  30,9% | 0,2358              GERÇEK: Sonuç İlanı imzadan aylar sonra
#   2024 |  48,9% | 0,1846              yayımlandığı için erken yayımlananlar
#   2023 |  39,9% | 0,2115              sistematik bir alt kümedir.
#   2022 |  37,7% | 0,1676
#   2019 |  48,6% | 0,1787
#
# Kırılma noktası %1,7 ile %6 arasında: grup seviyesinde %6,0 (n=427) ve %8,7 (n=496)
# kapsamlı örnekler 0,109-0,115 verdi, yani ŞİŞMEDİ (inşaat işlerinin indirimi zaten
# düşük). Bu yüzden kapı **%5**: ölçülen felaket vakayı keser, kullanılabilir grupları
# korur.
#
# ⚠️ **Bu eşikleri sezgiyle oynatmayın.** İlk sürüm kapsamı %10 yapıp 427 gözlemlik
# örneklemleri elemişti; düzeltirken kapıyı tümden kaldırdım ve 2026'nın %1,7 kapsamlı
# 0,3894'ü "orta" etiketiyle geri geldi. Doğru ayar ancak yıl bazlı kapsam/ortalama
# tablosuna bakılarak yapılır (yukarıdaki sorgu: `scripts/` yok, tek satırlık GROUP BY).
MIN_INDIRIM_ORNEK = 30
MIN_INDIRIM_KAPSAM = 0.05
# `guven` eşikleri (bastırmaz, etiketler)
GUVEN_YUKSEK_ORNEK = 100
GUVEN_YUKSEK_KAPSAM = 0.25
GUVEN_ORTA_KAPSAM = 0.10


def _indirim(toplam, ornek, sozlesme_sayisi):
    """Ortalama indirim + güven etiketi.

    `(deger, guven)` çifti döner. `yetersiz` ise değer `None` — uydurma oran yerine
    "veri yok" demek bu üründe kuraldır (kullanıcı bu sayıya bakıp teklif fiyatı
    belirliyor).
    """
    if not ornek or toplam is None or ornek < MIN_INDIRIM_ORNEK:
        return None, "yetersiz"
    kapsam = (ornek / sozlesme_sayisi) if sozlesme_sayisi else 0
    if kapsam < MIN_INDIRIM_KAPSAM:
        return None, "yetersiz"
    if ornek >= GUVEN_YUKSEK_ORNEK and kapsam >= GUVEN_YUKSEK_KAPSAM:
        guven = "yuksek"
    elif kapsam >= GUVEN_ORTA_KAPSAM:
        guven = "orta"
    else:
        guven = "dusuk"
    return _ort_str(toplam, ornek), guven


# ── Yenileme (gece görevi) ────────────────────────────────────────────────────
def _bucket_adlari(bucketlar):
    """
    Gruplara temsili ad üretir.

    Önce OKAS ağacında 4 haneli birebir karşılık aranır (varsa doğru ad odur); yoksa
    grup içindeki **en sık geçen kalem adı** kullanılır. İkincisi bir yaklaşımdır ve
    bu yüzden yalnızca sunum içindir — filtreleme her zaman `okas_bucket` ile yapılır.
    """
    adlar = {}
    for kod, adi in OkasCode.objects.filter(kod__in=list(bucketlar)).values_list("kod", "adi"):
        adlar[kod] = adi

    eksik = [b for b in bucketlar if b and b not in adlar]
    if eksik:
        # Tek sorgu: (bucket, kalem adı) → adet. Farklı çift sayısı ≈ farklı OKAS kodu
        # sayısı (birkaç bin), döngü ucuz.
        sayac = {}
        for r in (
            Tender.objects.filter(okas_bucket__in=eksik)
            .exclude(okas_ana_adi="")
            .order_by()
            .values("okas_bucket", "okas_ana_adi")
            .annotate(n=Count("id"))
        ):
            b = r["okas_bucket"]
            if r["n"] > sayac.get(b, (0, ""))[0]:
                sayac[b] = (r["n"], r["okas_ana_adi"])
        for b, (_, adi) in sayac.items():
            adlar[b] = adi
    return adlar


def refresh_market_stats():
    """
    `MarketStat` + `MarketYearStat`'ı sıfırdan yeniden hesaplar.

    ⚠️ **`.values().annotate()` kullanır → `detail_raw` TOAST'ına HİÇ dokunmaz.**
    Bu yüzden `sync_contractors` süpürmesiyle pencere çakışması sorunu yoktur ve
    gece penceresi kısıtı gerekmez (`detect_recurring_series` ile aynı gerekçe).

    Tam yeniden hesap **üretimde 94 sn** ölçüldü (9.800 grup / 12 yıl). Sürenin büyük
    kısmı `_bucket_adlari`'nın `Tender` üzerindeki (bucket, kalem adı) GROUP BY'ı ve
    yıl bazlı `count(DISTINCT)`'lardır. `CELERY_TASK_TIME_LIMIT=300`'ün altında kalır,
    bu yüzden yıl rotasyonu/süre bütçesi eklenmedi — ama sınır **iki katı değil üç
    katı** uzakta, grup sayısı belirgin büyürse yeniden ölçün.

    Upsert + buda: turda dokunulmayan eski satırlar silinir (ör. bir grup artık hiç
    sözleşme üretmiyorsa).
    """
    simdi = timezone.now()

    # ── 1) (yıl, iş grubu) ────────────────────────────────────────────────────
    # ⚠️ `.order_by()` ŞART: `Contract.Meta.ordering` GROUP BY'a sızarsa gruplama
    # bozulur (bu kod tabanında `detsis_tree.py:30` dersi).
    satirlar = list(
        Contract.objects.filter(sozlesme_tarihi__isnull=False)
        .order_by()
        .values("sozlesme_tarihi__year", "okas_bucket")
        .annotate(
            adet=Count("id"),
            bedel=Sum("sozlesme_bedeli_num"),
            ind_top=Sum("indirim_orani"),
            ind_n=Count("indirim_orani"),
            tek_top=Sum("teklif_sayisi"),
            tek_n=Count("teklif_sayisi"),
        )
    )
    adlar = _bucket_adlari({r["okas_bucket"] for r in satirlar})

    nesneler = [
        MarketStat(
            yil=r["sozlesme_tarihi__year"],
            okas_bucket=r["okas_bucket"] or "",
            ad=adlar.get(r["okas_bucket"], "") if r["okas_bucket"] else SINIFLANDIRILMAMIS,
            sozlesme_sayisi=r["adet"],
            toplam_bedel=r["bedel"],
            indirim_toplam=r["ind_top"],
            indirim_ornek=r["ind_n"],
            teklif_toplam=r["tek_top"],
            teklif_ornek=r["tek_n"],
        )
        for r in satirlar
    ]
    MarketStat.objects.bulk_create(
        nesneler,
        update_conflicts=True,
        unique_fields=["yil", "okas_bucket"],
        update_fields=[
            "ad", "sozlesme_sayisi", "toplam_bedel",
            "indirim_toplam", "indirim_ornek", "teklif_toplam", "teklif_ornek",
            "guncelleme",
        ],
        batch_size=1000,
    )
    MarketStat.objects.filter(guncelleme__lt=simdi).delete()

    # ── 2) (yıl) — tekil sayılar burada, çünkü TOPLANAMAZLAR ──────────────────
    yil_satir = list(
        Contract.objects.filter(sozlesme_tarihi__isnull=False)
        .order_by()
        .values("sozlesme_tarihi__year")
        .annotate(
            adet=Count("id"),
            bedel=Sum("sozlesme_bedeli_num"),
            ind_top=Sum("indirim_orani"),
            ind_n=Count("indirim_orani"),
            tek_top=Sum("teklif_sayisi"),
            tek_n=Count("teklif_sayisi"),
            firma=Count("yuklenici", distinct=True),
            idare=Count("idare_id", distinct=True),
        )
    )
    MarketYearStat.objects.bulk_create(
        [
            MarketYearStat(
                yil=r["sozlesme_tarihi__year"],
                sozlesme_sayisi=r["adet"],
                toplam_bedel=r["bedel"],
                indirim_toplam=r["ind_top"],
                indirim_ornek=r["ind_n"],
                teklif_toplam=r["tek_top"],
                teklif_ornek=r["tek_n"],
                firma_tekil=r["firma"],
                idare_tekil=r["idare"],
            )
            for r in yil_satir
        ],
        update_conflicts=True,
        unique_fields=["yil"],
        update_fields=[
            "sozlesme_sayisi", "toplam_bedel", "indirim_toplam", "indirim_ornek",
            "teklif_toplam", "teklif_ornek", "firma_tekil", "idare_tekil", "guncelleme",
        ],
        batch_size=100,
    )
    MarketYearStat.objects.filter(guncelleme__lt=simdi).delete()

    return {"gruplar": len(nesneler), "yillar": len(yil_satir)}


# ── Okuma (uçlar) ─────────────────────────────────────────────────────────────
def _ortalama(toplam, ornek):
    """Ağırlıklı ortalama — **her zaman örneklem sayısıyla birlikte** sunulur.

    ⚠️ Örneklem sayısı olmadan ortalama göstermek bu üründe yasak: `indirim_orani`
    kapsamı kısmi (%37,6) ve tek başına gösterilen bir ortalama yanıltır.
    """
    if not ornek or toplam is None:
        return None
    return (Decimal(toplam) / Decimal(ornek)).quantize(Decimal("0.0001"))


def _ort_str(toplam, ornek):
    """Ortalamayı string'e çevirir.

    ⚠️ `str(_ortalama(...) or "") or None` YAZMAYIN: `Decimal("0.0000")` Python'da
    **falsy**'dir → gerçek bir "ortalama sıfır" değeri sessizce `None`'a düşer ve
    istemci "veri yok" sanır. `is not None` ile açıkça kontrol edilir.
    """
    v = _ortalama(toplam, ornek)
    return str(v) if v is not None else None


def _grup_satiri(s):
    ind_deger, ind_guven = _indirim(s.indirim_toplam, s.indirim_ornek, s.sozlesme_sayisi)
    return {
        "okas_bucket": s.okas_bucket,
        "ad": s.ad or (SINIFLANDIRILMAMIS if not s.okas_bucket else ""),
        "sozlesme_sayisi": s.sozlesme_sayisi,
        "toplam_bedel": str(s.toplam_bedel) if s.toplam_bedel is not None else None,
        "ortalama_indirim": ind_deger,
        "indirim_guven": ind_guven,
        "indirim_ornek_sayisi": s.indirim_ornek,
        "ortalama_teklif": _ort_str(s.teklif_toplam, s.teklif_ornek),
        "teklif_ornek_sayisi": s.teklif_ornek,
    }


def mevcut_yillar():
    return list(MarketYearStat.objects.order_by("-yil").values_list("yil", flat=True))


def _yil_coz(istenen, yillar):
    """İstenen yıl geçerliyse onu, değilse en güncel yılı döner."""
    if not yillar:
        return None
    try:
        y = int(istenen)
    except (TypeError, ValueError):
        return yillar[0]
    return y if y in yillar else yillar[0]


def genel_bakis(yil=None, limit=VARSAYILAN_LIMIT):
    """Pano ana ekranı: yıl özeti + o yılın en büyük iş grupları."""
    yillar = mevcut_yillar()
    y = _yil_coz(yil, yillar)
    if y is None:
        return None, "Pazar verisi henüz hesaplanmadı."

    ys = MarketYearStat.objects.filter(yil=y).first()
    limit = max(1, min(MAX_LIMIT, limit))
    gruplar = MarketStat.objects.filter(yil=y).order_by("-toplam_bedel")[:limit]

    return {
        "yil": y,
        "yillar": yillar,
        "ozet": {
            "sozlesme_sayisi": ys.sozlesme_sayisi if ys else 0,
            # ⚠️ Bu iki sayı `MarketStat` satırlarından TÜRETİLEMEZ (toplanamaz) —
            # kendi kesin grain'inden gelir.
            "firma_sayisi": ys.firma_tekil if ys else None,
            "idare_sayisi": ys.idare_tekil if ys else None,
            "toplam_bedel": str(ys.toplam_bedel) if ys and ys.toplam_bedel is not None else None,
            "ortalama_indirim": (
                _indirim(ys.indirim_toplam, ys.indirim_ornek, ys.sozlesme_sayisi)[0]
            ) if ys else None,
            "indirim_guven": (
                _indirim(ys.indirim_toplam, ys.indirim_ornek, ys.sozlesme_sayisi)[1]
            ) if ys else "yetersiz",
            "indirim_ornek_sayisi": ys.indirim_ornek if ys else 0,
            "ortalama_teklif": _ort_str(ys.teklif_toplam, ys.teklif_ornek) if ys else None,
            "teklif_ornek_sayisi": ys.teklif_ornek if ys else 0,
        },
        "is_gruplari": [_grup_satiri(s) for s in gruplar],
        "guncelleme": ys.guncelleme.isoformat() if ys else None,
    }, None


def _iller(bucket, yil, limit):
    """İl kırılımı — CANLI (ölçüm 85 ms). Materialize edilmedi, bkz. modül başlığı."""
    from .models import City

    satir = list(
        Contract.objects.filter(
            okas_bucket=bucket, sozlesme_tarihi__year=yil, il_id__isnull=False
        )
        .order_by()
        .values("il_id")
        .annotate(adet=Count("id"), bedel=Sum("sozlesme_bedeli_num"))
        .order_by("-bedel")[:limit]
    )
    adlar = dict(
        City.objects.filter(ekap_il_id__in=[r["il_id"] for r in satir])
        .values_list("ekap_il_id", "ad")
    )
    return [
        {
            "il_id": r["il_id"],
            "ad": adlar.get(r["il_id"], ""),
            "sozlesme_sayisi": r["adet"],
            "toplam_bedel": str(r["bedel"]) if r["bedel"] is not None else None,
        }
        for r in satir
    ]


def _firmalar(bucket, yil, limit):
    """
    Firma kırılımı + yoğunlaşma (HHI) — CANLI (ölçüm 103 ms).

    HHI = Σ(pay²); 1'e yakın = tek firma hâkimiyeti. ⚠️ Firma sayısı `HHI_MAX_FIRMA`'yı
    aşarsa `yaklasik: True` döner — kesilmiş bir küme üzerinden hesaplanan HHI tam
    değildir ve bunu söylememek yanıltıcı olurdu.
    """
    from .models import Contractor

    temel = Contract.objects.filter(
        okas_bucket=bucket, sozlesme_tarihi__year=yil, yuklenici__isnull=False
    ).order_by()

    tum = list(
        temel.values("yuklenici").annotate(
            adet=Count("id"), bedel=Sum("sozlesme_bedeli_num")
        ).order_by("-bedel")[:HHI_MAX_FIRMA]
    )
    firma_sayisi = temel.values("yuklenici").distinct().count()

    toplam = sum((r["bedel"] or 0) for r in tum)
    hhi = None
    if toplam:
        hhi = sum(((r["bedel"] or 0) / toplam) ** 2 for r in tum)
        hhi = float(round(Decimal(hhi), 4))

    ilk = tum[:limit]
    # ⚠️ `Contractor`'da alan adı `kanonik_ad` (düz `ad` YOK) — firma kimliği
    # kanonikleştirilmiş ünvandır, bkz. ekap/contractors.py.
    adlar = dict(
        Contractor.objects.filter(id__in=[r["yuklenici"] for r in ilk])
        .values_list("id", "kanonik_ad")
    )
    return (
        [
            {
                "contractor_id": r["yuklenici"],
                "ad": adlar.get(r["yuklenici"], ""),
                "sozlesme_sayisi": r["adet"],
                "toplam_bedel": str(r["bedel"]) if r["bedel"] is not None else None,
            }
            for r in ilk
        ],
        {
            "hhi": hhi,
            "firma_sayisi": firma_sayisi,
            "yaklasik": firma_sayisi > HHI_MAX_FIRMA,
        },
    )


def grup_detayi(bucket, yil=None, limit=VARSAYILAN_LIMIT):
    """Drill-down: bir iş grubunun yıllara göre seyri + il ve firma kırılımı."""
    bucket = (bucket or "").strip()
    seri = list(MarketStat.objects.filter(okas_bucket=bucket).order_by("yil"))
    if not seri:
        return None, "İş grubu bulunamadı."

    yillar = [s.yil for s in seri]
    y = _yil_coz(yil, sorted(yillar, reverse=True))
    limit = max(1, min(MAX_LIMIT, limit))
    firmalar, yogunlasma = _firmalar(bucket, y, limit)

    return {
        "okas_bucket": bucket,
        "ad": seri[-1].ad or (SINIFLANDIRILMAMIS if not bucket else ""),
        "yillar": sorted(yillar, reverse=True),
        "yillara_gore": [
            {
                "yil": s.yil,
                "sozlesme_sayisi": s.sozlesme_sayisi,
                "toplam_bedel": str(s.toplam_bedel) if s.toplam_bedel is not None else None,
                "ortalama_indirim": (
                    _indirim(s.indirim_toplam, s.indirim_ornek, s.sozlesme_sayisi)[0]
                ),
                "indirim_guven": (
                    _indirim(s.indirim_toplam, s.indirim_ornek, s.sozlesme_sayisi)[1]
                ),
                "indirim_ornek_sayisi": s.indirim_ornek,
            }
            for s in seri
        ],
        "yil": y,
        "iller": _iller(bucket, y, limit),
        "firmalar": firmalar,
        "yogunlasma": yogunlasma,
        "guncelleme": seri[-1].guncelleme.isoformat(),
    }, None


# ── Free maskeleme ────────────────────────────────────────────────────────────
# ⚠️ Maskeleme SUNUCUDA yapılır, istemcide değil: yüklenici uçlarında maskelemenin
# yalnızca istemcide olması bilinen bir açıktı (plan "Hijyen" tablosu). Sayılar
# (kaç sözleşme, kaç firma) görünür kalır — teaser değeri buradan gelir; para ve
# indirim değerleri `null`'lanır.
_PARA_ALANLARI = ("toplam_bedel", "ortalama_indirim", "ortalama_teklif")


def _maskele_sozluk(d):
    for alan in _PARA_ALANLARI:
        if alan in d:
            d[alan] = None
    return d


def maskele(veri):
    """Free kullanıcı için para/indirim değerlerini gizler, sayıları bırakır."""
    if not veri:
        return veri
    if "ozet" in veri:
        _maskele_sozluk(veri["ozet"])
    for anahtar in ("is_gruplari", "yillara_gore", "iller", "firmalar"):
        for satir in veri.get(anahtar) or []:
            _maskele_sozluk(satir)
    if veri.get("yogunlasma"):
        veri["yogunlasma"]["hhi"] = None
    return veri
