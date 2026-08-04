"""
Tekrar eden ihale serisi anahtarı — aynı idarenin yıldan yıla tekrarladığı işi tanır.

Örn. "2024 YILI TEKSTİL MALZEMELERİ ALIMI (12 AYLIK)" ile
     "2025 YILI TEKSTİL MALZEMELERİ ALIM İŞİ" aynı seriye düşer.

## Neden trigram self-join DEĞİL

Akla ilk gelen çözüm `similarity(a.ihale_adi_norm, b.ihale_adi_norm) > 0.6` ile idare
bazlı self-join'dir. Bu **reddedildi**: büyük alıcılarda grup boyutu (k) on binleri
bulur, maliyet O(k²)'dir ve trigram GIN'e rağmen 60-100 karakterlik bir başlık 60+
trigram üretip binlerce aday satır + recheck getirir. Bir milyon probe saatlerce CPU
**ve** arama çalışma kümesinin tamamen boşalması demektir — yüklenici süpürmesinde
belgelenmiş (heap cache isabeti %53) arıza modunun aynısı.

Yerine: **ingest sırasında hesaplanan deterministik iskelet**. Tespit görevi sonradan
yalnızca indeksli bir `varchar(40)` kolonuna GROUP BY yapar; metin karşılaştırması yok.

## Tasarım değeri: yanlış birleştirme, kaçırılandan kötüdür

`ekap/contractors.py`'nin kimlik kuralıyla aynı gerekçe. Bir seriye yanlış ihale
karışırsa "bu iş her yıl Mart'ta çıkıyor" tahmini sessizce bozulur ve kullanıcı buna
göre hazırlık yapar. Bu yüzden:

- Aynı `idare_id` **ve** aynı `okas_ana_kod` şartı aranır (ikisi de boşsa anahtar yok).
- İskelette **en az 2 anlamlı token** olmalı; yoksa anahtar üretilmez.
- Bedeli: "… ALIMI" ile "… ALIM İŞİ" gibi varyantları kaçırmak. Stopword listesi bunu
  azaltır, kalanı kabul edilir.
"""
import hashlib
import re

from .utils import normalize_tr

# İhale adlarında ayırt edici olmayan kelimeler. Hepsi `normalize_tr` geçmiş biçimde
# (ascii, küçük harf) yazılır — karşılaştırma da normalize edilmiş metinde yapılır.
_STOPWORDS = frozenset({
    # zaman
    "yili", "yil", "yillik", "aylik", "ay", "gun", "gunluk", "donem", "donemi",
    # miktar/birim
    "adet", "kalem", "kalemi", "kg", "ton", "litre", "metre", "m2", "m3",
    # işlem türü (ihale adlarının yarısında var, ayırt etmez)
    "alimi", "alim", "alinmasi", "satin", "satinalma",
    "isi", "is", "isler", "isleri", "hizmeti", "hizmet", "hizmetleri",
    "yapim", "yapimi", "yaptirilmasi", "yapilmasi", "onarim", "onarimi",
    "bakim", "bakimi", "temini", "tedariki", "kiralama", "kiralanmasi",
    # bağlaç/edat
    "ve", "ile", "icin", "adina", "bagli", "dahil", "haric", "veya",
    # idari
    "mudurlugu", "baskanligi", "bakanligi", "genel", "il", "ilce",
})

# Rakam içeren token'lar (yıl, miktar, ihale no) atılır: seriyi yıldan yıla ayıran
# şey tam olarak bunlardır.
_HAS_DIGIT = re.compile(r"\d")
_NON_WORD = re.compile(r"[^a-z0-9\s]+")


def series_skeleton(ihale_adi: str) -> str:
    """
    İhale adından yıl/rakam/stopword arındırılmış, token'ları **sıralanmış** iskelet.

    Sıralama şart: "MALZEME TEKSTİL ALIMI" ile "TEKSTİL MALZEME ALIMI" aynı işi
    tanımlar, kelime sırası EKAP'ta tutarlı değildir.

    >>> series_skeleton("2025 YILI TEKSTİL MALZEMELERİ ALIMI (12 AYLIK)")
    'malzemeleri tekstil'
    """
    metin = _NON_WORD.sub(" ", normalize_tr(ihale_adi or ""))
    tokenlar = {
        t for t in metin.split()
        if len(t) > 2 and t not in _STOPWORDS and not _HAS_DIGIT.search(t)
    }
    return " ".join(sorted(tokenlar))


def series_key(idare_id: str, okas_ana_kod: str, ihale_adi: str) -> str:
    """
    `(idare, okas, iskelet)` üçlüsünün sha1'i — ya da ayırt edici değilse boş string.

    Boş dönen durumlar (bilinçli): idare bilinmiyor, ya da iskelet 2 token'dan az.
    Boş anahtar hiçbir seriye katılmaz → yanlış birleştirme riski sıfırlanır.
    """
    if not idare_id:
        return ""
    iskelet = series_skeleton(ihale_adi)
    if len(iskelet.split()) < 2:
        return ""
    ham = f"{idare_id}|{okas_ana_kod or ''}|{iskelet}"
    return hashlib.sha1(ham.encode("utf-8")).hexdigest()
