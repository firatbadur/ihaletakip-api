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

- Aynı `idare_id` şartı aranır (idare bilinmiyorsa anahtar yok).
- İskelette **en az 2 anlamlı token** olmalı; yoksa anahtar üretilmez.
- Bedeli: "… ALIMI" ile "… ALIM İŞİ" gibi varyantları kaçırmak. Stopword listesi bunu
  azaltır, kalanı kabul edilir.

## ⚠️⚠️ `okas_ana_kod` ANAHTARDAN ÇIKARILDI (2026-09-24) — serileri BÖLÜYORDU

Anahtar eskiden `(idare_id, okas_ana_kod, iskelet)` üçlüsüydü; OKAS eşitliği yanlış
birleştirmeye karşı bir güvenlik kemeri olarak konmuştu. Üretim ölçümü bu kemerin
taşıdığı riski **ters yöne** çevirdiğini gösterdi:

- `okas_ana_kod` **kararlı değil**: `ihtiyacKalemiOkasList[0].kodu`'ndan türetiliyor,
  yani kalem sıralaması değişince değişiyor; üstelik sözleşmelerin ancak %76-88'inde
  dolu. Aynı idarenin aynı işi bir yıl "4523", ertesi yıl boş ya da başka kod alıyor.
- Ölçüm: `(idare_id, iskelet)` ile kurulan **48.795** seriden **18.023'ü (%36,9)**
  birden çok `okas_ana_kod` taşıyor → mevcut anahtarla ikiye/üçe bölünüyordu.
- Kanıt ikna edici: aynı idarede **birebir aynı iskelete** sahip ihaleler farklı
  seriye düşüyordu ("ODTÜ 2025 Yılı 6 Grup Bakım Onarım Hizmeti" ↔ "ODTÜ 2026 Yılı
  6 Grup Bakım Onarım Hizmetleri Alımı", "TIBBİ GAZ ALIMI (6 KALEM)" ↔ "6 KALEM
  TIBBİ GAZ ALIMI"). Tek fark OKAS koduydu.
- Geriye dönük test (kesim 2025-09-24, 1 yıllık ufuk) OKAS'sız gruplamanın hem daha
  çok hem daha isabetli tahmin ürettiğini gösterdi:

  | | tahmin | "hiç ihale çıkmadı" | ±30 gün isabet | ±90 gün |
  |---|---|---|---|---|
  | OKAS'lı (yüksek güven) | 574 | %52,6 | %30,5 | %42,5 |
  | **OKAS'sız (yüksek güven)** | **778** | **%41,5** | **%39,3** | **%53,7** |

⚠️ **Yanlış birleştirme riski denetlendi** (18 grup tek tek okundu, 12'si en riskli
sınıf olan 2 token'lı iskeletlerden): hepsi gerçekten aynı işti ("Hazır Beton Satın
Alınması", "SODYUM HİPOKLORİT ALIMI", "Elektrik Enerjisi", "Çağrı Merkezi Hizmet
Alımı"). İdare kapsamı zaten dar olduğu için 2 token yeterli ayırt ediciliği
sağlıyor. İsabet oranlarının **yükselmesi** de yaygın bir yanlış birleştirme
olmadığının dolaylı kanıtıdır — gürültü karışsaydı aralık medyanları bozulurdu.

⚠️ Anahtar değiştiği için arşiv **yeniden hesaplanmalıdır**:
`python manage.py fix_seri_anahtar`. Kolon satır-içi alanlardan türetildiği için
(`idare_id`, `ihale_adi`) `detail_raw` TOAST'ına dokunulmaz.
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


def series_key(idare_id: str, ihale_adi: str) -> str:
    """
    `(idare, iskelet)` çiftinin sha1'i — ya da ayırt edici değilse boş string.

    Boş dönen durumlar (bilinçli): idare bilinmiyor, ya da iskelet 2 token'dan az.
    Boş anahtar hiçbir seriye katılmaz → yanlış birleştirme riski sıfırlanır.

    ⚠️ `okas_ana_kod` **bilerek girdi değildir** — modül başlığındaki ölçüme bakın.
    Eski imza `(idare_id, okas_ana_kod, ihale_adi)` idi; OKAS kararsız olduğu için
    gerçek serileri bölüyordu.
    """
    if not idare_id:
        return ""
    iskelet = series_skeleton(ihale_adi)
    if len(iskelet.split()) < 2:
        return ""
    ham = f"{idare_id}|{iskelet}"
    return hashlib.sha1(ham.encode("utf-8")).hexdigest()
