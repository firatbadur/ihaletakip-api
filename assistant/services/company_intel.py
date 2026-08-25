"""
Firma istihbaratı — profil haritası prompt'una giren iki dış kaynak.

1. `contractor_text()`  : EKAP sözleşme geçmişi (elimizdeki 100k+ yüklenici kaydı).
2. `website_text()`     : firmanın kendi web sitesinden kısa metin.

⚠️ EKAP yalnızca İMZALANMIŞ sözleşmeleri yayımlar: burada üretilen metin firmanın
ALDIĞI işlerdir; katıldığı/kaybettiği ihaleler veri kaynağında YOKTUR. Prompt'ta da
böyle anlatılır — "kazanma oranı" gibi bir çıkarım istenmez.
"""
import logging
import re

logger = logging.getLogger("ihaletakip")

# Sözleşme geçmişinden prompt'a taşınan tavanlar. Amaç profilin ne yaptığını
# anlatmak; tam döküm değil (token maliyeti + LLM'in uzun listede odağı dağılır).
MAX_IS_ADI = 40
MAX_IDARE = 10
MAX_OKAS = 15

# Web sitesi okuma sınırları
WEBSITE_TIMEOUT = 8          # saniye
WEBSITE_MAX_BYTES = 400_000  # indirilen ham HTML tavanı
WEBSITE_MAX_CHARS = 4_000    # prompt'a giren düz metin tavanı

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>", re.I | re.S
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def _money(v):
    return f"{v:,.0f}".replace(",", ".") if v is not None else None


def contractor_text(contractor) -> str:
    """
    EKAP yüklenici kaydından prompt'a eklenecek geçmiş özeti.

    Kırılımlar `ekap.views.ContractorDetailView._dagilim` ile aynı mantıkta ama
    LLM için düzleştirilmiş metin olarak üretilir (JSON değil — model metni daha
    az token'la okuyor ve alan adı uydurma riski kalmıyor).
    """
    from django.db.models import Count, Sum
    from django.db.models.functions import ExtractYear

    from ekap.constants import CITIES, IHALE_TURU
    from ekap.models import Contract, OkasItem

    base = Contract.objects.filter(yuklenici=contractor)
    il_adlari = {ekap_il_id: ad for ekap_il_id, _plaka, ad, _big in CITIES}

    lines = [
        "## EKAP SÖZLEŞME GEÇMİŞİ (doğrulanmış veri)",
        f"- Kanonik ünvan: {contractor.kanonik_ad}",
        f"- Tür: {contractor.get_kind_display()}",
    ]
    if contractor.il_adi:
        lines.append(f"- Kayıtlı il: {contractor.il_adi}")
    # ⚠️ sozlesme_sayisi ≠ ihale_sayisi (kısımlı ihalede bir ihale birden çok sözleşme)
    lines.append(
        f"- İmzalanmış sözleşme: {contractor.sozlesme_sayisi} adet "
        f"({contractor.ihale_sayisi} ayrı ihale, {contractor.idare_sayisi} ayrı idare)"
    )
    if contractor.toplam_sozlesme_bedeli is not None:
        lines.append(f"- Toplam sözleşme bedeli: {_money(contractor.toplam_sozlesme_bedeli)} TL")
    if contractor.ilk_sozlesme_tarihi and contractor.son_sozlesme_tarihi:
        lines.append(
            f"- Faaliyet aralığı: {contractor.ilk_sozlesme_tarihi:%Y} — "
            f"{contractor.son_sozlesme_tarihi:%Y}"
        )

    def _rows(qs, key, limit=None):
        qs = qs.values(key).annotate(adet=Count("id")).order_by("-adet")
        return list(qs[:limit] if limit else qs)

    tip_rows = _rows(base.exclude(ihale_tip__isnull=True), "ihale_tip")
    if tip_rows:
        lines.append(
            "- İhale türü dağılımı: "
            + ", ".join(
                f"{IHALE_TURU.get(r['ihale_tip'], r['ihale_tip'])} ({r['adet']})"
                for r in tip_rows
            )
        )

    il_rows = _rows(base.exclude(il_id__isnull=True), "il_id", 12)
    if il_rows:
        lines.append(
            "- İl dağılımı: "
            + ", ".join(f"{il_adlari.get(r['il_id'], r['il_id'])} ({r['adet']})" for r in il_rows)
        )

    yil_rows = _rows(
        base.exclude(sozlesme_tarihi__isnull=True).annotate(yil=ExtractYear("sozlesme_tarihi")),
        "yil",
    )
    if yil_rows:
        lines.append(
            "- Yıllara göre: "
            + ", ".join(f"{r['yil']}: {r['adet']}" for r in sorted(yil_rows, key=lambda r: r["yil"]))
        )

    idare_rows = list(
        base.exclude(tender__idare_adi="")
        .values("tender__idare_adi")
        .annotate(adet=Count("id"))
        .order_by("-adet")[:MAX_IDARE]
    )
    if idare_rows:
        lines.append("- En çok iş aldığı idareler:")
        lines += [f"  * {r['tender__idare_adi']} ({r['adet']})" for r in idare_rows]

    # OKAS kodları: firmanın gerçekten iş aldığı kategoriler. Profil haritasındaki
    # `okas_prefixes` böylece tahmin değil, geçmişten türeyen veri olur.
    okas_rows = list(
        OkasItem.objects.filter(tender__sozlesmeler__yuklenici=contractor)
        .exclude(kodu="")
        .values("kodu", "adi")
        .annotate(adet=Count("id"))
        .order_by("-adet")[:MAX_OKAS]
    )
    if okas_rows:
        lines.append("- Geçmiş işlerinde geçen OKAS kodları:")
        lines += [f"  * {r['kodu']} — {r['adi']} ({r['adet']})" for r in okas_rows]

    is_adlari = list(
        base.exclude(tender__ihale_adi="")
        .order_by("-sozlesme_tarihi")
        .values_list("tender__ihale_adi", flat=True)[:MAX_IS_ADI]
    )
    if is_adlari:
        lines.append(f"- Aldığı işlerden örnekler (en yeni {len(is_adlari)}):")
        lines += [f"  * {ad}" for ad in dict.fromkeys(is_adlari)]

    return "\n".join(lines)


def contractor_facets(contractor) -> tuple[list, list]:
    """
    Sözleşme geçmişinden (il_id listesi, ihale_tip listesi) türetir.

    Kural tabanlı eşleştirme (`services/matching.py`) `profile.cities` /
    `profile.tender_types` alanlarını ÖN FİLTRE olarak kullanıyor. Sihirbaz artık
    bunları sormadığı için firma bağlıysa buradan doldurulur; yoksa eşleştirme
    tüm Türkiye'yi tarar ve öneriler alakasızlaşır.

    İl seçimi kasten geniş: firmanın sözleşmelerinin en az %5'ini oluşturan iller
    (en çok 10). Tek bir ilde çalışan firmayı o ile kilitler, gezici müteahhidi
    kilitlemez.
    """
    from django.db.models import Count

    from ekap.models import Contract

    base = Contract.objects.filter(yuklenici=contractor)
    toplam = base.count()
    if not toplam:
        return [], []

    esik = max(1, round(toplam * 0.05))
    iller = [
        r["il_id"]
        for r in base.exclude(il_id__isnull=True)
        .values("il_id")
        .annotate(adet=Count("id"))
        .order_by("-adet")[:10]
        if r["adet"] >= esik
    ]
    turler = [
        r["ihale_tip"]
        for r in base.exclude(ihale_tip__isnull=True)
        .values("ihale_tip")
        .annotate(adet=Count("id"))
        .order_by("-adet")
        if r["adet"] >= esik
    ]
    return iller, turler


def website_text(url: str) -> str:
    """
    Firmanın web sitesini kısaca okur; prompt'a eklenecek düz metni döner.

    Erişilemezse BOŞ string döner — profil haritası üretimi web sitesi yüzünden
    ASLA başarısız olmaz (site kapalı/yavaş olabilir, kullanıcı yanlış yazmış
    olabilir). HTML ayrıştırması regex ile yapılır: amaç "firma ne iş yapıyor"
    sinyali, birebir içerik çıkarımı değil; ek bağımlılık getirmeye değmez.
    """
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        import requests

        resp = requests.get(
            url,
            timeout=WEBSITE_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
                ),
                "Accept-Language": "tr-TR,tr;q=0.9",
            },
            stream=True,
        )
        resp.raise_for_status()
        if "html" not in (resp.headers.get("Content-Type") or "").lower():
            return ""
        raw = resp.raw.read(WEBSITE_MAX_BYTES, decode_content=True) or b""
        html = raw.decode(resp.encoding or "utf-8", errors="ignore")
    except Exception as e:  # ağ/TLS/DNS/timeout — hepsi "site okunamadı" demek
        logger.info("Firma web sitesi okunamadı (%s): %s", url, e)
        return ""

    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[:WEBSITE_MAX_CHARS]
