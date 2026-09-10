"""
İdare adından `idare_id` çözümü — mobil API bu alanı vermediği için.

⚠️ **Neden gerekli:** mobil uçların hiçbiri `idare_id` döndürmüyor (2026-09-10'da
tüm uçlar tek tek tarandı: ihale detayı, ilan/şartname HTML'i, doküman listesi,
sonuç ilanı XML'i). Alan boş kalırsa favori idare bildirimi, idare profili, DETSIS
ağacıyla filtreleme ve `seri_anahtar` mobil kaynaklı ihalelerde çalışmaz.

⚠️ **Ad eşleştirmesi kusurludur ve bu bilinçli bir kabuldür.** Ölçüldü (üretim,
`Tender.idare_adi` → `Authority.ad_norm`, gerçek `idare_id` ile karşılaştırma):

    | yöntem                | doğru | yanlış | kesinlik |
    |-----------------------|-------|--------|----------|
    | tam ad (tek aday)     |  137  |   3    |  %97,9   |
    | trigram ≥ 0,90        |   47  |   4    |  %92,2   |
    | trigram ≥ 0,85        |   51  |   7    |  %87,9   |
    | trigram ≥ 0,50        |   78  |  65    |  %54,5   |

Bu yüzden **iki kademe** vardır ve altında kalan hiçbir şey yazılmaz:
  1. `ad_norm` birebir eşleşiyor **ve tek bir `idare_id`ye** gidiyorsa → yaz.
     (Aynı ada sahip 3.155 idare var; birden çok adaya giden ad **belirsizdir**,
     "en uygun"u seçmek yazı tura atmaktır → boş bırakılır.)
  2. Değilse trigram benzerliği `EKAP_MOBIL_IDARE_ESIK` (vars. 0,90) üstündeyse yaz.
  3. Hiçbiri değilse **boş bırakılır** — "eksik veri, yanlış veriden iyidir".

⚠️ Hangi yolla yazıldığı `Tender.idare_kaynak` kolonuna işlenir (`v2` | `tam` |
`benzer`): sonuç denetlenebilir ve gerekirse yalnızca `benzer` olanlar geri alınabilir.
"""
import hashlib
import logging

from django.conf import settings
from django.core.cache import cache

from ..utils import normalize_tr

logger = logging.getLogger("ihaletakip")

KAYNAK_TAM = "tam"
KAYNAK_BENZER = "benzer"

# Aynı idare yüzlerce ihalede geçiyor → ad başına tek çözüm yeter.
_CACHE_ONEK = "ekap:mobil:idare:"
_CACHE_TTL = 24 * 3600


def coz(idare_adi: str):
    """`(idare_id, kaynak)` döner; çözülemezse `("", "")`."""
    n = normalize_tr(idare_adi)
    if not n:
        return "", ""

    anahtar = f"{_CACHE_ONEK}{hashlib.sha1(n.encode('utf-8')).hexdigest()[:16]}"
    try:
        onbellek = cache.get(anahtar)
    except Exception:                                   # noqa: BLE001
        onbellek = None
    if onbellek is not None:
        return tuple(onbellek)

    sonuc = _coz(n)
    try:
        cache.set(anahtar, list(sonuc), _CACHE_TTL)
    except Exception:                                   # noqa: BLE001
        pass
    return sonuc


def _coz(n: str):
    from ..models import Authority

    # 1) Birebir ad — tek `idare_id`ye gidiyorsa güvenilir (ölçüm: %97,9).
    adaylar = set(
        Authority.objects.filter(ad_norm=n).exclude(idare_id="")
        .values_list("idare_id", flat=True)[:5]
    )
    if len(adaylar) == 1:
        return adaylar.pop(), KAYNAK_TAM
    if len(adaylar) > 1:
        # ⚠️ Belirsiz: aynı ada sahip birden çok idare. "En uygun"u seçmek için
        # ayırt edici veri yok (mobil il/üst idare adı veriyor ama `Authority`de
        # il alanı yok) → boş bırakılır.
        logger.info("idare adı belirsiz (%s aday): %s", len(adaylar), n[:80])
        return "", ""

    # 2) Bulanık eşleşme — yalnızca YÜKSEK eşikte (ölçüm: 0,90 → %92,2; 0,50 → %54,5)
    esik = getattr(settings, "EKAP_MOBIL_IDARE_ESIK", 0.90)
    if esik <= 0:
        return "", ""
    try:
        from django.contrib.postgres.search import TrigramSimilarity
    except ImportError:                                 # SQLite/yerel geliştirme
        return "", ""
    try:
        en_iyi = (
            Authority.objects.exclude(idare_id="")
            .annotate(benzerlik=TrigramSimilarity("ad_norm", n))
            .filter(benzerlik__gte=esik)
            .order_by("-benzerlik")
            .values_list("idare_id", "benzerlik")
            .first()
        )
    except Exception as e:                              # noqa: BLE001
        # pg_trgm yoksa ya da sorgu düşerse: eşleştirme bir zenginleştirmedir,
        # ingest'i düşürmemeli.
        logger.warning("idare trigram eşleştirmesi başarısız: %s", e)
        return "", ""
    if en_iyi:
        return en_iyi[0], KAYNAK_BENZER
    return "", ""
