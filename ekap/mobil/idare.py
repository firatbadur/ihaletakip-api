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

⚠️⚠️ **MOBİL, ATA-YOLUNU BİRLEŞTİRİP TEK AD OLARAK VERİYOR** — bu, birebir ad
eşleşmesini ihalelerin çoğunda **yapısal olarak imkânsız** kılıyordu. Örnek
(İKN 2026/1789437, üretimde kullanıcı bildirdi):

    mobil      : "TEKİRDAĞ SU VE KANALİZASYON İDARESİ GENEL MÜDÜRLÜĞÜ
                  TİCARET İŞLERİ DAİRESİ BAŞKANLIĞI İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ"
    `Authority`: "İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ"  (detsis 18148693, idare_id 91646)
                  ataları: TEKİRDAĞ BÜYÜKŞEHİR BELEDİYE BAŞKANLIĞI › TESKİ GENEL
                  MÜDÜRLÜĞÜ › GENEL MÜDÜR YARDIMCILIĞI 1 › TİCARET İŞLERİ D. BŞK.

⚠️ **Yol birebir DEĞİLDİR: mobil ara düğüm atlayabilir** (yukarıda "GENEL MÜDÜR
YARDIMCILIĞI 1" mobil adında yok) ve **kökten başlamaz**. Bu yüzden eşleştirme
"yolun soneki" değil, ata adlarının **sıralı alt-dizisi** olarak doğrulanır
(`_onek_dogrula`): önekteki her parça gerçek bir ata adı olmalı ve sıra korunmalı.

Üretim ölçümü (2026-09-26): son 10 günün mobil kaynaklı ihalelerinin **yalnızca
%30,5'inde** `idare_id` doluydu; eşleşmeyen 1.177 kaydın **1.128'i (%95,8)** bu
kademeyle çözülüyor. Doğruluk iki bağımsız yolla ölçüldü — v2'nin gerçek
`idare_id`leriyle **%97,4** (n=2.898) ve gerçek mobil kayıtlarda tahmin edilen
id'nin v2 arşivindeki adıyla çapraz karşılaştırma **%96,3** (n=1.128; uyumsuz
görünen 10 vakanın tamamı elle incelendi, hepsi **doğru** eşleşmeydi — v2'nin
kısaltmalı adı token olarak örtüşmüyor: "GÖL.DZ.ÜS K.LIĞI" ↔ "GÖLCÜK DENİZ ANA
ÜS KOMUTANLIĞI").

Bu yüzden **üç kademe** vardır ve altında kalan hiçbir şey yazılmaz:
  1. `ad_norm` birebir eşleşiyor **ve tek bir `idare_id`ye** gidiyorsa → yaz.
     (Aynı ada sahip 3.155 idare var; birden çok adaya giden ad **belirsizdir**,
     "en uygun"u seçmek yazı tura atmaktır → boş bırakılır.)
  2. Ata-yolu: adın bir **soneki** bir düğümün tam adı ve kalan **önek** o düğümün
     ata adlarıyla sırayla doğrulanıyorsa → yaz (`yol`). En **uzun** sonek önce
     denenir; uzun sonek = daha çok ayırt edici bilgi.
  3. Değilse trigram benzerliği `EKAP_MOBIL_IDARE_ESIK` (vars. 0,90) üstündeyse yaz.
  4. Hiçbiri değilse **boş bırakılır** — "eksik veri, yanlış veriden iyidir".

⚠️ **Kademe 2, trigram'dan ÖNCE gelmeli**: ölçülen kesinliği daha yüksek
(%97,4 > %92,2) ve kararı **yapısal** (ata zinciri), bir benzerlik skoru değil.

⚠️ Hangi yolla yazıldığı `Tender.idare_kaynak` kolonuna işlenir (`v2` | `tam` |
`yol` | `benzer`): sonuç denetlenebilir, gerekirse tek bir kademe geri alınabilir.
"""
import hashlib
import logging

from django.conf import settings
from django.core.cache import cache

from ..utils import normalize_tr

logger = logging.getLogger("ihaletakip")

KAYNAK_TAM = "tam"
KAYNAK_YOL = "yol"
KAYNAK_BENZER = "benzer"

# Ağaç derinliği güvenlik tavanı (bozuk veri / döngü koruması) — `detsis_tree` ile aynı.
_MAX_ATA = 12

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

    # 2) Ata-yolu — mobil adı birleşik verdiği için ASIL kademe budur (ölçüm: %97,4).
    idare_id = _yol_kademesi(n)
    if idare_id:
        return idare_id, KAYNAK_YOL

    # 3) Bulanık eşleşme — yalnızca YÜKSEK eşikte (ölçüm: 0,90 → %92,2; 0,50 → %54,5)
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


def _yol_kademesi(n: str) -> str:
    """
    Adın bir **soneki** bir `Authority` düğümünün tam adıysa ve kalan **önek** o
    düğümün ata adlarıyla doğrulanıyorsa `idare_id` döner; yoksa "".

    En **uzun** sonekten başlanır: uzun sonek daha çok ayırt edici bilgi taşır
    ("İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ" tek başına yüzlerce kurumda var, ata yoluyla
    benzersizleşir).

    ⚠️ Birden çok `idare_id`ye giden bir sonekte **boş dönülür** — belirsizlikte
    "en uygun"u seçmek yazı turadır (kademe 1 ile aynı ilke). Ölçüm: son 10 günün
    eşleşmeyenlerinin yalnızca %3,4'ü bu dalda kalıyor.
    """
    from ..models import Authority

    kelimeler = n.split()
    if len(kelimeler) < 2:
        return ""

    # i=0 (tam ad) kademe 1'de denendi → 1'den başla. En uzun sonek önce gelir.
    sonekler = [" ".join(kelimeler[i:]) for i in range(1, len(kelimeler))]

    # ⚠️ **Tek sorgu**: sonek başına ayrı sorgu, ad başına ~10 gidiş-dönüş demekti.
    # `.order_by()` şart — `Authority.Meta.ordering = ["ad"]` aksi halde boşa sort ekler.
    gruplar = {}
    for ad_norm, parent_detsis, idare_id in (
        Authority.objects.filter(ad_norm__in=sonekler)
        .exclude(idare_id="")
        .order_by()
        .values_list("ad_norm", "parent_detsis", "idare_id")[:300]
    ):
        gruplar.setdefault(ad_norm, []).append((parent_detsis, idare_id))

    if not gruplar:
        return ""

    ata_bellek = {}
    for i, sonek in enumerate(sonekler, start=1):
        adaylar = gruplar.get(sonek)
        if not adaylar:
            continue
        onek = " ".join(kelimeler[:i])
        uygun = {
            idare_id
            for parent_detsis, idare_id in adaylar
            if _onek_dogrula(onek, _ata_adlari(parent_detsis, ata_bellek))
        }
        if len(uygun) == 1:
            return uygun.pop()
        if len(uygun) > 1:
            logger.info("idare yolu belirsiz (%s aday): %s", len(uygun), n[:80])
            return ""
    return ""


def _ata_adlari(parent_detsis: str, bellek: dict) -> list:
    """Bir düğümün ata `ad_norm`ları, **kök→ebeveyn** sırasıyla."""
    from ..models import Authority

    yol, gorulen, cur = [], set(), parent_detsis
    while cur and cur not in gorulen and len(yol) < _MAX_ATA:
        gorulen.add(cur)
        if cur in bellek:
            ad_norm, ust = bellek[cur]
        else:
            satir = (
                Authority.objects.filter(detsis_no=cur)
                .order_by()
                .values_list("ad_norm", "parent_detsis")
                .first()
            )
            if not satir:
                break
            ad_norm, ust = satir
            bellek[cur] = (ad_norm or "", ust)
        if ad_norm:
            yol.append(ad_norm)
        cur = ust
    yol.reverse()
    return yol


def _onek_dogrula(onek: str, ata_adlari: list) -> bool:
    """
    `onek`, `ata_adlari` (kök→ebeveyn) içinden **sıralı** seçilen adların birleşimi
    olarak tüketilebiliyor mu?

    ⚠️ **Ata ATLAMAYA izin verilir, sıra bozmaya izin verilmez.** Mobil ad idari ara
    katmanları göstermiyor ("GENEL MÜDÜR YARDIMCILIĞI 1") ve kökten başlamıyor —
    katı bir yol eşitliği bu yüzden neredeyse hiç tutmazdı. Buna karşılık önekteki
    **her** parçanın gerçek bir ata adı olması şartı yanlış birleştirmeyi engeller:
    "başka bir kurumun aynı adlı şubesi" önek doğrulamasını geçemez.
    """
    kalan, i = onek.strip(), 0
    while kalan:
        eslesti = False
        while i < len(ata_adlari):
            ata = ata_adlari[i]
            i += 1
            if not ata:
                continue
            if kalan == ata:
                kalan, eslesti = "", True
                break
            if kalan.startswith(ata + " "):
                kalan, eslesti = kalan[len(ata) + 1:], True
                break
        if not eslesti:
            return False
    return True
