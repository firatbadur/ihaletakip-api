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

from .constants import CITIES
from .series import _HAS_DIGIT, _NON_WORD
from .utils import normalize_tr

# Kalıptan atılan gürültü: **yalnızca** miktar/süre bildiren token'lar. Bunlar aynı işin
# yıldan yıla tekrarını farklı kalıplara bölerdi (dedup oranını düşürür), oysa anlam
# taşımıyorlar. ⚠️ `series._STOPWORDS`'ün aksine işlem türü ("alimi", "hizmeti") ve idari
# ekler burada YOK — onlar AI'ya bağlam olarak gitmeli.
_KALIP_GURULTU = frozenset({
    "yili", "yil", "yillik", "aylik", "ay", "gun", "gunluk", "donem", "donemi",
    "adet", "kalem", "kalemi", "kalemlik",
})

# ⚠️ Coğrafi token'lar kalıptan ATILIR — dedup'ın en büyük tek kaldıracı.
#
# Üretimde ölçüldü (2026-08-28, 1.047.976 ihale): kalıpların **%85'i benzersiz** ve
# benzersizliğin başlıca sebebi ad içindeki yer adları — "hakkari semdinli ilcesi
# karsiyaka mahallesi istinat duvari yapim isi". Aynı iş her ilde ayrı kalıp üretiyor,
# yani aynı soru AI'ya defalarca soruluyor.
#
# Bunları atmanın keyword kalitesine maliyeti YOK: yer adı zaten yasak keyword
# (bir işin nerede yapıldığı, ona benzeyen işleri bulmaya yaramaz). Yani atmak hem
# maliyeti düşürür hem prompt'u temizler.
#
# ⚠️ İlçe/mahalle adlarının listesi elimizde yok — yalnızca 81 il adı ve coğrafi ek
# token'ları atılabiliyor. Kalan yer adları benzersizlik üretmeye devam eder; bu bir
# eksiklik değil, veri sınırı.
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
    """
    metin = _NON_WORD.sub(" ", normalize_tr(ihale_adi or ""))
    tokenlar = [
        t for t in metin.split()
        if t and not _HAS_DIGIT.search(t)
        and t not in _KALIP_GURULTU and t not in _COGRAFI
    ]
    return " ".join(tokenlar)[:KALIP_AZAMI_KARAKTER].strip()


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
