"""
İhale adı → anahtar kelime (keyword) katmanı.

Fiyat/rakip analizindeki "benzer iş" seçimi bugüne kadar **yalnızca OKAS kodu + idare/il**
üzerinden yapılıyordu. İki yapısal kusuru var: OKAS ana kodu çok kaba (aynı kod altında
alakasız işler var) ve **ihalelerin ~%19'unda OKAS kalemi hiç yok**. Bu modül ihale
adından üretilen keyword'lerle üçüncü bir benzerlik ekseni açar.

## İki ayrı iş, bilinçli olarak ayrılmış

1. **Ad kalıbı** (`kalip_norm`/`kalip_hash`) — yıl/miktar arındırılmış normalize ad.
   Amacı **dedup**: `"2024 YILI AKARYAKIT ALIMI"` ve `"2025 YILI AKARYAKIT ALIMI
   (12 AYLIK)"` aynı kalıba düşer, yani AI'ya **bir kez** gider ve sonuç kalıbı paylaşan
   tüm ihalelere yayılır. 1M ihalelik arşivde AI maliyetini bire indiren şey budur.
2. **Kanonikleştirme** (`kanonik_keyword`) — AI'nın döndürdüğü metni deterministik bir
   biçime indirger.

## ⚠️ Kanonikleştirme: model tavsiye eder, kod zorlar

Prompt "yalın hâl kullan, jenerik ek üretme" der ama model bunu **garanti etmez**;
1M ihalelik bir arşivde %1'lik sapma bile on binlerce çöp keyword demektir. Bu yüzden
her keyword `kanonik_keyword`'den geçer: `"akaryakıt alımı"` → `akaryakit`,
`"tıbbi sarf malzemeleri"` → `tibbi sarf malzeme`. Varyantlar böylece **aynı `Keyword`
satırına** düşer.

Kalite güvencesini prompt'a yaslamak bu üründe kabul edilmez — aynı ilke
`ai/services/summary.py::sesli_temizle` içinde de var: *prompt tavsiyedir, kod garantidir.*

## ⚠️ `series_skeleton`'dan neden ayrı

`ekap/series.py` de ihale adını normalize eder ama **başka bir iş için**: seri tespiti
adları *eşitlemek* ister, biz *AI'ya okutmak* isteriz. İki fark kritik:

| | `series_skeleton` | `kalip_norm` (burası) |
|---|---|---|
| Token sırası | `sorted()` — eşitleme için | **korunur** — AI'ya sırasız kelime çuvalı verilemez |
| Stopword | atılır | **korunur** — "alımı" bağlam taşır |

Yani "alımı" AI'nın *üretmemesi* gereken bir şeydir, *görmemesi* gereken değil:
`"KÖMÜR ALIMI"` ile `"KÖMÜR NAKLİ"` farklı işlerdir ve fark tam da o kelimededir.
"""
import hashlib
import re

from .constants import CITIES, SEKTOR_ANAHTARLARI
from .series import _HAS_DIGIT, _NON_WORD
from .utils import normalize_tr

# Kalıptan atılan gürültü: **yalnızca** miktar/süre bildiren token'lar. Bunlar aynı işin
# yıldan yıla tekrarını farklı kalıplara bölerdi (dedup oranını düşürür), oysa anlam
# taşımıyorlar. ⚠️ `series._STOPWORDS`'ün aksine işlem türü ("alimi", "hizmeti") ve idari
# ekler burada YOK — onlar AI'ya bağlam olarak gitmeli.
_KALIP_GURULTU = frozenset({
    "yili", "yil", "yillik", "aylik", "ay", "gun", "gunluk", "donem", "donemi",
    "adet", "kalem", "kalemi", "kalemlik",
    # ⚠️ AY ADLARI — üretimde görüldü: "tibbi sarf malzeme alimi OCAK alimi" ile
    # "... ARALIK alimi" ayrı kalıplara düşüyordu, yani aynı iş AI'ya 12 kez
    # sorulabiliyordu. Ay adı bir işi diğerinden ayırmaz; yıl/miktar ile aynı
    # kategoride gürültüdür.
    "ocak", "subat", "mart", "nisan", "mayis", "haziran",
    "temmuz", "agustos", "eylul", "ekim", "kasim", "aralik",
})

# Coğrafi token'lar kalıptan atılır.
#
# ⚠️ **BU BİR DEDUP ÇÖZÜMÜ DEĞİL — denendi ve neredeyse hiçbir şey kazandırmadı.**
# Hipotez şuydu: kalıpların %85'i benzersiz, sebebi ad içindeki yer adları ("hakkari
# semdinli ilcesi karsiyaka mahallesi istinat duvari yapim isi"), yani aynı iş her
# ilde ayrı kalıp üretiyor. Üretimde ölçüldü (2026-08-28, 1.047.976 ihale):
#
#     temizlemeden önce : 676.384 tekil kalıp  (dedup 1,50×)
#     temizlemeden sonra: 669.463 tekil kalıp  (dedup 1,52×)   → kazanç %1
#
# Yani benzersizliğin kaynağı il adı DEĞİL; ilçe/mahalle/mevkii adları (listesi
# elimizde yok) ve işin kendine özgü tanımı. Kalıbı daha agresif budayarak dedup
# aramak bu veride çıkmaz sokak — bu ölçüm tekrarlanmasın diye buraya yazıldı.
#
# Temizlik yine de KORUNUYOR, ama gerekçesi maliyet değil: yer adı zaten yasak
# keyword (bir işin nerede yapıldığı, ona benzeyen işleri bulmaya yaramaz), o yüzden
# prompt'a hiç gitmemesi modelin dikkatini dağıtmaz ve istek başına birkaç token
# kazandırır.
_COGRAFI = frozenset(
    [normalize_tr(ad) for _, _, ad, _ in CITIES] + [
        "ili", "ilce", "ilcesi", "mahalle", "mahallesi", "mah", "koyu", "koy",
        "beldesi", "belde", "mevkii", "mevki", "buyuksehir", "bolge", "bolgesi",
        "cadde", "caddesi", "sokak", "sokagi", "bulvari", "kume", "evleri",
        "ada", "parsel", "pafta", "nolu",
    ]
)

# Kalıp uzunluk tavanı. Bazı ihale adları kalem listesinin tamamını içeriyor (2000+
# karakter); prompt'u şişirir ve dedup değeri sıfırdır (öyle bir ad zaten benzersizdir).
KALIP_AZAMI_KARAKTER = 240

# Keyword biçim kuralları.
KEYWORD_MIN_KARAKTER = 3
KEYWORD_AZAMI_TOKEN = 3
KEYWORD_AZAMI_KARAKTER = 64      # Keyword.metin max_length ile aynı olmalı

# ⚠️ Keyword'e özel stopword listesi — `series._STOPWORDS` BURADA KULLANILAMAZ.
#
# İki listenin işi farklı. Seri eşleştirme adları *eşitlemek* ister, o yüzden iş türünü
# ("bakım", "onarım", "kiralama") de atar: "X ALIMI" ile "X BAKIMI" aynı seriye düşsün
# diye değil, o token ayırt etmediği için. Keyword katmanında ise **iş türü bilginin ta
# kendisidir**: "asansör alımı" ile "asansör bakımı" farklı işlerdir ve fiyatları
# karşılaştırılamaz. `series._STOPWORDS` kullanılsaydı ikisi de "asansor"a düşer, prompt'un
# açıkça SERBEST bıraktığı "asansör bakım" keyword'ü yok edilirdi.
#
# Buradaki liste yalnızca **hiçbir bağlamda bilgi taşımayan** token'ları içerir.
_KEYWORD_STOPWORDS = frozenset({
    # satın alma fiili — her ihale adında var, hiçbir şey ayırt etmez
    "alimi", "alim", "alinmasi", "satin", "satinalma", "temini", "tedariki",
    "isi", "is", "isler", "isleri", "hizmeti", "hizmet", "hizmetleri",
    "yapim", "yapimi", "yaptirilmasi", "yapilmasi",
    # bağlaç/edat
    "ve", "ile", "icin", "adina", "bagli", "dahil", "haric", "veya",
    # zaman/miktar
    "yili", "yil", "yillik", "aylik", "ay", "gun", "gunluk", "donem", "donemi",
    "adet", "kalem", "kalemi", "kalemlik",
    # idari ek (kurum adı benzerlik taşımaz — her idare aynı işi alıyor olabilir)
    "mudurlugu", "mudurluk", "baskanligi", "bakanligi", "belediyesi", "belediye",
    "hastanesi", "universitesi", "valiligi", "kaymakamligi", "rektorlugu", "genel",
    # içi boş nitelemeler
    "muhtelif", "cesitli", "degisik",
})

# ⚠️ Bu token'lar **tek başına** keyword olamaz ama **birleşikte serbesttir**.
# Prompt'un kuralı birebir bu: YASAK "bakım onarım" · SERBEST "asansör bakım".
# Token bazlı atılsalardı ikinci hâl de yok olurdu; bu yüzden ayrı bir kapı:
# kanonik sonucun TAMAMI bu kümedeyse keyword düşer.
# ⚠️ Değerler aynı zamanda **çekim normalizasyonudur**: "onarımı" ile "onarım" aynı
# işi anlatır ama farklı token'dır → normalize edilmeseler aynı iş için iki ayrı
# `Keyword` satırı doğar (keyword patlamasının sessiz kaynağı). Genel bir iyelik-eki
# kırpıcı riskli olurdu ("kanalı" ≠ "kanal" değil ama "sanayi" → "sanay" olurdu);
# burada küme küçük ve elle doğrulanmış olduğu için tablo güvenli.
_JENERIK_KANONIK = {
    "bakim": "bakim", "bakimi": "bakim",
    "onarim": "onarim", "onarimi": "onarim",
    "tamir": "tamir", "tamiri": "tamir",
    "montaj": "montaj", "montaji": "montaj",
    "tadilat": "tadilat", "tadilati": "tadilat",
    "yenileme": "yenileme", "yenilenmesi": "yenileme",
    "kiralama": "kiralama", "kiralanmasi": "kiralama",
    "tasima": "tasima", "tasinmasi": "tasima", "nakli": "tasima",
    "malzeme": "malzeme", "malzemesi": "malzeme", "malzemeleri": "malzeme",
    "urun": "urun", "urunu": "urun",
    "sarf": "sarf", "yedek": "yedek",
}
_TEK_BASINA_JENERIK = frozenset(_JENERIK_KANONIK.values())

# Kanonikleştirmede atılan çekim ekleri. ⚠️ Liste **muhafazakâr**: yalnızca kamu ihale
# adlarında sık geçen ve anlamı değiştirmeyen ekler var. Agresif bir stemmer yanlış
# birleştirme üretirdi ("kanal" ≠ "kanalizasyon") ve bu üründe yanlış birleştirme
# kaçırılandan kötüdür (aynı değer `ekap/contractors.py` ve `series.py`'de de geçerli).
_COGUL_EKLERI = ("lerinin", "larinin", "lerini", "larini", "lerine", "larina",
                 "lerde", "larda", "leri", "lari", "ler", "lar")

# ⚠️ 3. tekil iyelik eki — **keyword patlamasının ana kaynağı** (üretimde ölçüldü:
# 1,70 tekil/kalıp; model "elbisesi" ve "elbise"yi, "projesi" ve "proje"yi ayrı ayrı
# üretiyordu ve ikisi ayrı `Keyword` satırına düşüyordu).
#
# ⚠️ Yalnızca `-si/-sı/-su/-sü` atılır, çıplak `-i/-ı/-u/-ü` ATILMAZ. Sebep: `-si`
# eki sesli harfle biten köke gelir, yani kırpınca geriye anlamlı bir kök kalır
# ("elbisesi"→elbise, "projesi"→proje, "makinesi"→makine, "tesisi"→tesis). Çıplak
# `-i` ise sıfat ve kök son harfiyle karışır ("tıbbi"→"tıbb", "sanayi"→"sanay") —
# yanlış birleştirme kaçırılandan kötüdür (aynı değer `contractors.py`/`series.py`).
_IYELIK_EKLERI = ("si", "su")   # normalize_tr sonrası "sı/sü" da bu biçime düşer

# Ek atıldıktan sonra kökte kalması gereken en az harf. 3 seçildi: "hatlarinin" → "hat"
# geçerli bir köktür. 4'te bu kelime hiç sadeleşmiyordu (ölçüldü).
_KOK_MIN = 3


def kalip_norm(ihale_adi: str) -> str:
    """
    İhale adının yıl/miktar arındırılmış, **sırası korunmuş** normalize hâli.

    >>> kalip_norm("2025 YILI TEKSTİL MALZEMELERİ ALIMI (12 AYLIK)")
    'tekstil malzemeleri alimi'
    >>> kalip_norm("2024 YILI TEKSTİL MALZEMELERİ ALIM İŞİ")
    'tekstil malzemeleri alim isi'
    >>> kalip_norm("Hakkari Şemdinli İlçesi Karşıyaka Mahallesi İstinat Duvarı Yapım İşi")
    'semdinli karsiyaka istinat duvari yapim isi'
    >>> kalip_norm("TIBBİ SARF MALZEME ALIMI OCAK 2025 ALIMI")   # ay + yıl + tekrar
    'tibbi sarf malzeme alimi'
    """
    metin = _NON_WORD.sub(" ", normalize_tr(ihale_adi or ""))
    tokenlar = [
        t for t in metin.split()
        if t and not _HAS_DIGIT.search(t)
        and t not in _KALIP_GURULTU and t not in _COGRAFI
    ]
    # ⚠️ Ardışık tekrarlar temizlenir. Gürültü token'ları atılınca ortaya çıkıyorlar:
    # "ALIMI OCAK 2025 ALIMI" → (ay ve yıl atılır) → "alimi alimi". Yalnızca ARDIŞIK
    # olanlar atılır; uzaktaki tekrar anlamlı olabilir ("kum cakil kum").
    sade = [t for i, t in enumerate(tokenlar) if i == 0 or t != tokenlar[i - 1]]
    return " ".join(sade)[:KALIP_AZAMI_KARAKTER].strip()


def kalip_hash(ihale_adi: str) -> str:
    """
    `kalip_norm`'un sha1'i — ya da ayırt edici değilse boş string.

    Boş dönen durum (bilinçli): 2 token'dan az kalıp. `"MAL ALIMI"` gibi bir ad hiçbir
    benzerlik taşımaz; AI'ya göndermek hem para hem de sonradan gürültü keyword demektir.
    """
    n = kalip_norm(ihale_adi)
    if len(n.split()) < 2:
        return ""
    return hashlib.sha1(n.encode("utf-8")).hexdigest()


def _cogul_kok(token: str) -> str:
    """Sondaki çokluk/iyelik ekini atar — yalnızca kökte `_KOK_MIN` harf kalıyorsa."""
    for ek in _COGUL_EKLERI:
        if token.endswith(ek) and len(token) - len(ek) >= _KOK_MIN:
            return token[: -len(ek)]
    for ek in _IYELIK_EKLERI:
        # ⚠️ Kök 5+ harf olmalı. 4'te ölçülen bozulma: "tesisi" → "tesi" (doğrusu
        # "tesis" — o kelime `tesis`+`i` alır, `tesi`+`si` değil; ikisini ayırt
        # edecek bir sinyal yok). 5'te "tesisi" hiç kırpılmaz: "tesisi"/"tesis"
        # ayrı keyword kalır (KAÇIRMA), ama uydurma bir kök üretilmez (YANLIŞ
        # BİRLEŞTİRME). Bu projede tercih hep bu yönde — bkz. contractors.py.
        if token.endswith(ek) and len(token) - len(ek) >= 5:
            return token[: -len(ek)]
    return token


def kanonik_keyword(metin: str) -> str | None:
    """
    AI'dan gelen keyword'ü deterministik kanonik biçime indirger; kullanılamazsa `None`.

    >>> kanonik_keyword("Akaryakıt Alımı")
    'akaryakit'
    >>> kanonik_keyword("TIBBİ SARF MALZEMELERİ")
    'tibbi sarf malzeme'
    >>> kanonik_keyword("asansör bakım onarımı")   # iş türü KORUNUR
    'asansor bakim onarim'
    >>> kanonik_keyword("2025 yılı")          # yalnızca gürültü kalır → düşer
    >>> kanonik_keyword("işi")                # tek stopword → düşer
    >>> kanonik_keyword("bakım onarım")       # tamamı jenerik → düşer
    """
    ham = _NON_WORD.sub(" ", normalize_tr(metin or ""))
    tokenlar = [
        _cogul_kok(t) for t in ham.split()
        if t and not _HAS_DIGIT.search(t) and t not in _KEYWORD_STOPWORDS
    ]
    # ⚠️ Ek atıldıktan SONRA yeniden bakılır: "malzemeleri" kökü "malzeme"dir ve
    # stopword değildir, ama "işleri" → "is" stopword'e düşer.
    tokenlar = [_JENERIK_KANONIK.get(t, t) for t in tokenlar
                if t and t not in _KEYWORD_STOPWORDS and len(t) >= 2]
    if not tokenlar:
        return None
    # Tamamı tek-başına-jenerik token'lardan oluşuyorsa keyword değildir
    # ("bakım onarım" ✗) — ama ayırt edici bir isimle birlikteyse geçer
    # ("asansör bakım" ✓). Prompt'un kuralının kod tarafındaki karşılığı.
    if all(t in _TEK_BASINA_JENERIK for t in tokenlar):
        return None
    sonuc = " ".join(tokenlar[:KEYWORD_AZAMI_TOKEN])
    if len(sonuc) < KEYWORD_MIN_KARAKTER:
        return None
    return sonuc[:KEYWORD_AZAMI_KARAKTER].strip()


def derece(kanonik: str) -> int:
    """Keyword'ün n-gram derecesi (1-3) — benzerlik ağırlığının çarpanı."""
    return min(len(kanonik.split()), KEYWORD_AZAMI_TOKEN)


# ── AI'siz taban çizgisi (baseline) ─────────────────────
#
# AI'nın parasını hak edip etmediğini ölçmek için gereken karşılaştırma noktası.
# Aynı kalıptan, model kullanmadan, yalnızca kelime istatistiğiyle keyword üretir.
#
# ⚠️ Bu bir "ucuz alternatif" değil, bir **kontrol grubu**. Pilotta gözlendi ki
# eşleşmelerin çoğunu ihale adında ZATEN GEÇEN kelimeler kuruyor ("arac kiralama",
# "insaat malzeme", "akaryakit") — bunlar için modele ihtiyaç yok. AI'nın ölçülmesi
# gereken katkısı, adda GEÇMEYEN üst kavramı ekleyip farklı kelimelerle yazılmış aynı
# işleri köprülemesi ("ameliyat eldiveni" → "tıbbi sarf malzeme"). Fark buradan çıkar.

# Bir token'ın keyword olmaya değmesi için gereken en az doküman frekansı. df==1 olan
# token hiçbir kesişim üretemez (tanım gereği tek ihalede var) — depolar ama işe yaramaz.
DF_MIN = 2
# Bu orandan fazla kalıpta geçen token ayırt etmez ("hizmet", "malzeme" gibi kalanlar).
DF_MAX_ORAN = 0.02


def ngram_adaylari(kalip: str, azami_n: int = KEYWORD_AZAMI_TOKEN):
    """
    Kalıptan bitişik n-gram adayları (1..azami_n), **sıra korunmuş**.

    >>> ngram_adaylari("tibbi sarf malzeme alimi", azami_n=2)
    ['tibbi', 'sarf', 'malzeme', 'alimi', 'tibbi sarf', 'sarf malzeme', 'malzeme alimi']
    """
    tokenlar = (kalip or "").split()
    adaylar = []
    for n in range(1, azami_n + 1):
        for i in range(len(tokenlar) - n + 1):
            adaylar.append(" ".join(tokenlar[i:i + n]))
    return adaylar


# Bir bileşiğin "gerçek terim" sayılması için gereken en az PMI. 0 = kelimeler
# birbirinden bağımsız geçiyor (tesadüfi komşuluk); yüksek = birlikte anılan bir terim.
PMI_MIN = 2.0
# PMI'nin güvenilir olması için bileşiğin en az kaç kalıpta geçmesi gerektiği. Nadir
# bir bigram matematiksel olarak çok yüksek PMI verir ama kanıt tek gözlemdir.
PMI_MIN_GOZLEM = 5


def deterministik_keywords(kalip, df=None, bigram_df=None, kalip_sayisi=0, azami=8):
    """
    Kalıptan **AI kullanmadan** keyword üretir — taban çizgisi.

    İki istatistik kullanır ve ikisi de gereklidir:

    * **IDF** (`df`) — hangi kelime ayırt edici? "akaryakit" nadir ve anlamlı;
      "malzeme" her yerde, tek başına hiçbir şey söylemiyor.
    * **PMI** (`bigram_df`) — hangi bitişik kelime çifti *gerçek bir terim*?
      Ham n-gram üretmek gürültü doğurur ("suyu hatti yapim"); ama "icme suyu"
      kelimelerinin birlikte geçme oranı tesadüfen beklenenden çok yüksekse bu
      dilde yerleşmiş bir terimdir. PMI tam olarak bu oranı ölçer.

    ⚠️ İkisi de verilmezse fonksiyon çalışır ama **taban çizgisi haksız yere kötü
    çıkar** ve AI olduğundan iyi görünür. Karşılaştırma yapıyorsanız ikisini de verin.

    ⚠️ Tasarım gereği yapamadığı şey: adda GEÇMEYEN hiçbir kavramı üretemez —
    "ameliyat eldiveni" için "tıbbi sarf malzeme" yazamaz. AI ile arasındaki fark
    tam olarak burada aranmalı, kelime örtüşmesinde değil.
    """
    import math

    n_dok = max(kalip_sayisi or 0, 1)
    adaylar = ngram_adaylari(kalip)
    tavan = max(DF_MIN, int(n_dok * DF_MAX_ORAN)) if kalip_sayisi else None

    def _pmi(tokenlar):
        """Bileşiğin bağlılık ölçüsü; bilinmiyorsa None (ceza da ödül de yok)."""
        if not bigram_df or len(tokenlar) < 2:
            return None
        skorlar = []
        for a, b in zip(tokenlar, tokenlar[1:]):
            ortak = bigram_df.get(f"{a} {b}", 0)
            if ortak < PMI_MIN_GOZLEM:
                return None
            pa, pb = df.get(a, 0) / n_dok, df.get(b, 0) / n_dok
            if pa <= 0 or pb <= 0:
                return None
            skorlar.append(math.log((ortak / n_dok) / (pa * pb)))
        return min(skorlar)          # en zayıf halka bileşiği belirler

    skorlu, gorulen = [], set()
    for i, ham in enumerate(adaylar):
        k = kanonik_keyword(ham)
        if not k or k in gorulen:
            continue
        gorulen.add(k)
        tokenlar = k.split()

        if df is None:
            skorlu.append((len(k) * len(tokenlar), i, k))    # df yoksa kaba vekil
            continue

        frekanslar = [df.get(t, 0) for t in tokenlar]
        en_sik = max(frekanslar) if frekanslar else 0
        if en_sik < DF_MIN:
            continue                     # tek ihalede geçiyor → kesişim üretemez
        if tavan and en_sik > tavan and len(tokenlar) == 1:
            continue                     # tek başına çok yaygın → gürültü

        idf = sum(math.log(n_dok / max(f, 1)) for f in frekanslar)
        pmi = _pmi(tokenlar)
        if len(tokenlar) > 1:
            if pmi is None:
                # Bileşik hakkında kanıt yok → 1-gram'lara göre hafif dezavantajlı
                # bırakılır; atılmaz, çünkü "kanıt yok" ≠ "kötü".
                agirlik = 0.8
            elif pmi < PMI_MIN:
                continue                 # kelimeler tesadüfen yan yana → terim değil
            else:
                agirlik = 1.0 + min(pmi, 8.0) / 4.0
        else:
            agirlik = 1.0
        skorlu.append((idf * agirlik, i, k))

    # Skora göre seç, sonra ORİJİNAL sırayı geri getir — okunabilirlik ve AI
    # çıktısıyla yan yana konduğunda karşılaştırılabilirlik için.
    skorlu.sort(key=lambda x: (-x[0], x[1]))
    return [k for _, _, k in sorted(skorlu[:azami], key=lambda x: x[1])]


def df_topla(kaliplar, bigram=True):
    """
    Kalıp akışından `(token_df, bigram_df, kalip_sayisi)` çıkarır.

    ⚠️ Bigram sözlüğü büyür (1M kalıpta milyonlarca farklı çift). Çağıran, dönen
    `bigram_df`'yi düşük frekanslılardan **budamalıdır** — PMI zaten `PMI_MIN_GOZLEM`
    altını kullanmıyor, yani budama bilgi kaybettirmez, yalnızca bellek kazandırır.
    """
    from collections import Counter

    token_df, bigram_df, toplam = Counter(), Counter(), 0
    for kalip in kaliplar:
        toplam += 1
        tokenlar = kalip.split()
        token_df.update(set(tokenlar))
        if bigram and len(tokenlar) > 1:
            bigram_df.update({f"{a} {b}" for a, b in zip(tokenlar, tokenlar[1:])})
    return token_df, bigram_df, toplam


# ── Kalite ölçümü (pilot) ───────────────────────────────
# Pilot bu regex'le "modelin yasak kuralı çiğneme oranını" ölçer. ⚠️ Kanonikleştirme
# ÖNCESİ ve SONRASI ayrı ölçülür: öncesi prompt kalitesini, sonrası gerçek sızıntıyı
# gösterir. Kapı sonrakine bakar (kanonikleştirici çoğunu zaten temizliyor).
_YASAK_DESEN = re.compile(
    r"\b(?:"
    r"\d{4}"                                        # yıl
    # ⚠️ Yalnızca KESİN idare ekleri. Bina/kurum TÜRÜ ("hastane", "okul",
    # "universite") bilerek dışarıda: "hastane binası yapımı" işinde "hastane"
    # ayırt edici bilgidir (hastane inşaatı ≠ okul inşaatı). Önceki sürüm
    # `hastane\w*` ile bunu da yasak sayıyor ve A kapısını sahte ihlallerle
    # şişiriyordu (ölçüldü: %2,7'nin büyük kısmı bu).
    r"|mudurlu\w*|baskanli\w*|bakanli\w*|belediyes\w*|valili\w*"
    r"|hastanesi|universitesi|rektorlu\w*|kaymakaml\w*"
    r"|mal\s+alim\w*|hizmet\s+alim\w*|yapim\s+is\w*|satin\s+alma"
    r"|^alim\w*$|^temin\w*$|^is\w*$|^hizmet\w*$"
    r")\b"
)


def yasak_ihlali(metin: str) -> bool:
    """Keyword yasak kalıplardan birini içeriyor mu (pilot metriği)."""
    return bool(_YASAK_DESEN.search(normalize_tr(metin or "")))


def kanonik_liste(hamlar, azami=8):
    """
    Ham keyword listesini kanonikleştirir; **sırayı koruyarak** tekilleştirir.

    Model aynı keyword'ü iki kez üretebiliyor (üretimde görüldü: "arac kiralama |
    arac kiralama") ve iki farklı yazım aynı kanonik biçime düşebiliyor
    ("elbisesi" + "elbise" → "elbise"). İkisi de burada tek satıra iner.
    """
    gorulen, sonuc = set(), []
    for ham in hamlar or []:
        k = kanonik_keyword(ham)
        if k and k not in gorulen:
            gorulen.add(k)
            sonuc.append(k)
        if len(sonuc) >= azami:
            break
    return sonuc


# ── Deterministik sektör sınıflandırma ──────────────────
#
# AI'nın en net üstünlüğü keyword üretmek değil, adda GEÇMEYEN bir kategoriye
# sınıflandırmaktı. Bu fonksiyon o üstünlüğü elle yazılmış bir anahtar kelime
# tablosuyla (`constants.SEKTOR_ANAHTARLARI`) kapatmayı dener — maliyeti sıfır.
#
# ⚠️ Skor **kelime sayısının karesi**: "elektrik" (1 kelime, puan 1) birçok sektörde
# geçer ve tek başına hiçbir şey söylemez; "elektrik enerjisi" (2 kelime, puan 4)
# spesifiktir. Düz sayım yapılsaydı, tek kelimelik anahtarı çok olan sektör her
# ihaleyi kendine çekerdi.
#
# ⚠️ Eşleşme yoksa "diger" döner ve bu bir başarısızlık DEĞİLDİR — zorlanmış bir
# etiket, dürüst bir "diger"den kötüdür (`benchmark.py` dürüstlük kuralıyla aynı).

def sektor_tahmin(kalip: str, keywords=None):
    """
    Kalıptan (ve varsa keyword'lerden) sektör etiketi tahmin eder — AI kullanmadan.

    Dönüş: `(sektor_kodu, skor)`. Skor 0 ise hiçbir kanıt yok, kod `"diger"`dir.

    >>> sektor_tahmin("akaryakit alimi")[0]
    'akaryakit_enerji'
    >>> sektor_tahmin("tibbi sarf malzeme alimi")[0]
    'saglik_tibbi_malzeme'
    >>> sektor_tahmin("ogrenci tasima hizmet alimi")[0]
    'personel_tasima'
    >>> sektor_tahmin("zzz qqq")[0]
    'diger'
    """
    ham = (kalip or "") + (" " + " ".join(keywords) if keywords else "")
    tokenlar = ham.split()
    # ⚠️ Türkçe ek çeşitliliği yüzünden TAM TOKEN eşleşmesi yetmez: kalıpta
    # "elbiseleri" yazar, anahtarda "elbise". Ölçüldü — düz eşleşmeyle "Kışlık İş
    # Elbiseleri" ve "Üretim Serası" `diger`e düşüyordu. Bu yüzden token'lar önce
    # kök kırpmasından geçirilir, sonra ayrıca önek eşleşmesine bakılır.
    kokler = {_cogul_kok(t) for t in tokenlar} | set(tokenlar)
    metin = " " + " ".join(tokenlar) + " "

    def _var(anahtar, n):
        if n > 1:
            return f" {anahtar} " in metin
        if anahtar in kokler:
            return True
        # Önek eşleşmesi — yalnızca yeterince uzun ve yakın anahtarlar için.
        # ⚠️ Sınırlar dar tutuldu: "sera" (4 harf) serbest bırakılsaydı "seramik"e
        # de eşleşirdi. Kısa anahtarlar tam eşleşmeye mahkûm; sözlükte onların
        # çekimli hâlleri ("serasi") ayrıca yazılır.
        if len(anahtar) < 5:
            return False
        return any(t.startswith(anahtar) and len(t) - len(anahtar) <= 4
                   for t in tokenlar)

    puanlar = {}
    for sektor, anahtarlar in SEKTOR_ANAHTARLARI.items():
        puan = 0
        for anahtar in anahtarlar:
            n = anahtar.count(" ") + 1
            if _var(anahtar, n):
                puan += n * n
        if puan:
            puanlar[sektor] = puan
    if not puanlar:
        return "diger", 0
    # Eşitlikte alfabetik değil, deterministik ve kararlı bir sıra: en yüksek puan,
    # sonra sektör kodu (tekrarlanabilirlik için).
    sektor = max(sorted(puanlar), key=lambda k: puanlar[k])
    return sektor, puanlar[sektor]


# ── DB katmanı ──────────────────────────────────────────
# ⚠️ Model importları fonksiyon içinde (proje kuralı): bu modül `sync.py` ve
# `tasks.py` tarafından import ediliyor, modül seviyesinde model çekmek app-loading
# sırasına bağımlılık yaratır.

def keyword_upsert(metinler):
    """
    Kanonik keyword metinlerini `Keyword` tablosuna yazar → `{metin: id}`.

    ⚠️ `get_or_create` DÖNGÜSÜ KULLANILMAZ: 669k kalıp × ~4 keyword'de bu, milyonlarca
    sorgu demektir (sessiz N+1). `bulk_create(ignore_conflicts=True)` + tek `filter`
    ile toplam **iki** sorgu yapılır.
    """
    from .models import Keyword

    metinler = [m for m in dict.fromkeys(metinler) if m]
    if not metinler:
        return {}
    Keyword.objects.bulk_create(
        [Keyword(metin=m, metin_ham=m, derece=derece(m)) for m in metinler],
        ignore_conflicts=True,
    )
    return dict(Keyword.objects.filter(metin__in=metinler).values_list("metin", "id"))


def oneri_keywordleri(kaliplar, limit=80):
    """
    Verilen kalıplarla kesişen, KULLANIMDA olan keyword'ler — AI'ya bağlam olarak gider.

    Amaç tutarlılık: aynı iş her seferinde aynı kelimeyle etiketlenmezse ("elbise" bir
    yerde, "elbisesi" başka yerde) benzer işler birbirini bulamaz. Model listeden
    seçince varyant üretimi düşer.

    ⚠️ **`kullanim_sayisi >= 2` şartı kritik.** Tek kullanımlık bir keyword henüz
    doğrulanmamıştır; öneri listesine girerse model onu tekrar seçer, seçim sayacı
    artırır, o da onu daha güçlü bir öneri yapar — hatalı bir keyword'ü arşive yayan
    kendi kendini besleyen döngü. Eşik, keyword'ün en az iki bağımsız kalıpta
    doğrulanmış olmasını şart koşar.
    """
    from django.db.models import Q

    from .models import Keyword

    tokenlar = set()
    for kalip in kaliplar:
        tokenlar.update(t for t in (kalip or "").split() if len(t) > 2)
    if not tokenlar:
        return []

    # Tek kelimelik keyword'ler doğrudan eşleşir; çok kelimeliler için "token ile
    # başlayan" yeterli bir yaklaşım (tam kesişim sorgusu ters indeks isterdi ve
    # bu listenin mükemmel olması gerekmiyor — yalnızca bir öneri).
    kosul = Q(metin__in=tokenlar)
    for t in list(tokenlar)[:40]:              # sorguyu şişirmemek için tavan
        kosul |= Q(metin__startswith=f"{t} ")
    return list(
        Keyword.objects.filter(kosul, pasif=False, kullanim_sayisi__gte=2)
        .order_by("-kullanim_sayisi")
        .values_list("metin", flat=True)[:limit]
    )


def probe_keywordleri(tender_pk, limit=None):
    """
    Bir ihalenin benzerlik sorgusunda kullanılacak keyword'leri `[(id, ağırlık), ...]`.

    ⚠️ **df filtresi PERFORMANS kontrolüdür.** Benzerlik sorgusunun maliyeti taranan
    index tuple sayısıyla, o da seçilen keyword'lerin df toplamıyla doğru orantılıdır.
    Bu yüzden `pasif` olanlar atılır ve kalanlardan **en düşük df'li** birkaç tanesi
    seçilir: hem en ayırt edici olanlar bunlardır (IDF), hem de en ucuz olanlar.

    Ağırlık `derece × log(N/df)`: 3 kelimelik bir ifade 1 kelimelikten daha güçlü
    kanıttır.
    """
    import math

    from django.conf import settings

    from .models import Keyword, TenderKeyword

    limit = limit or getattr(settings, "KEYWORD_PROBE_LIMIT", 5)
    satirlar = list(
        TenderKeyword.objects.filter(tender_id=tender_pk, keyword__pasif=False)
        .values_list("keyword_id", "keyword__derece", "keyword__kullanim_sayisi")
    )
    if not satirlar:
        return []
    toplam = Keyword.objects.filter(pasif=False).count() or 1
    skorlu = [
        (kid, d * math.log(toplam / max(df, 1)))
        for kid, d, df in satirlar
        if df >= 1
    ]
    skorlu.sort(key=lambda x: -x[1])
    return skorlu[:limit]


def benzer_ihale_idleri(tender_pk, keyword_agirliklari, limit):
    """
    Keyword örtüşmesine göre en benzer ihalelerin id'leri.

    Beklenen plan: `Limit → Sort → HashAggregate → Index Only Scan
    (ekap_tk_kw_tender_idx)`. Heap'e hiç gidilmez — `ekap_tender` 11 GB olduğu için
    bu tasarımın tek sebebi budur (bkz. `TenderKeyword` docstring'i).
    """
    from django.conf import settings
    from django.db.models import Case, FloatField, Sum, Value, When

    from .models import TenderKeyword

    if not keyword_agirliklari:
        return []
    when = [When(keyword_id=k, then=Value(w)) for k, w in keyword_agirliklari]
    esik = getattr(settings, "KEYWORD_SIMILAR_MIN_SKOR", 0.0)
    return (
        TenderKeyword.objects
        .filter(keyword_id__in=[k for k, _ in keyword_agirliklari])
        .exclude(tender_id=tender_pk)
        .values("tender_id")
        .annotate(skor=Sum(Case(*when, default=Value(0.0), output_field=FloatField())))
        .filter(skor__gte=esik)
        .order_by("-skor")
        .values_list("tender_id", flat=True)[:limit]
    )


def uygula(tender, kalip=None):
    """
    İhaleye kalıp sözlüğünden keyword + sektör uygular — **ingest hızlı yolu**.

    Yeni gelen bir ihalenin kalıbı sözlükte `durum="ok"` ile varsa AI'ya HİÇ gidilmez:
    tek indeksli bir SELECT + birkaç INSERT. Günde ~700 yeni ihalenin maliyeti budur.

    Kalıp sözlükte yoksa `pending` olarak açılır ve bir sonraki `dispatch` turunda
    AI'ya gider. Dönüş: uygulandı mı (bool).
    """
    from django.db.models import F
    from django.utils import timezone

    from .models import TenderKeyword, TenderNamePattern

    h = kalip_hash(tender.ihale_adi)
    if not h:
        return False
    norm = kalip_norm(tender.ihale_adi)

    kayit = (TenderNamePattern.objects
             .filter(kalip_hash=h)
             .only("id", "durum", "keyword_ids", "sektor")
             .first())
    if kayit is None:
        TenderNamePattern.objects.get_or_create(
            kalip_hash=h,
            defaults={"kalip_norm": norm, "ornek_ad": (tender.ihale_adi or "")[:500],
                      "ihale_sayisi": 1, "durum": "pending"},
        )
        return False

    TenderNamePattern.objects.filter(pk=kayit.pk).update(
        ihale_sayisi=F("ihale_sayisi") + 1)
    if kayit.durum != "ok" or not kayit.keyword_ids:
        return False

    TenderKeyword.objects.bulk_create(
        [TenderKeyword(tender_id=tender.pk, keyword_id=k) for k in kayit.keyword_ids],
        ignore_conflicts=True,
    )
    if kayit.sektor and tender.sektor != kayit.sektor:
        tender.sektor = kayit.sektor
    tender.kalip_hash = h
    return True
