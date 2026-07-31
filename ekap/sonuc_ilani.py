"""
Sonuç İlanı (`ilanTip=4`) HTML ayrıştırıcısı.

**Neden gerekli:** EKAP'ın `sozlesmeBilgiList[].yaklasikMaliyet` string'i bozuk üretilir
(float'ın ondalık noktası silinip yeniden gruplanır → 10×/100× şişme, geri kazanılamaz) ve
`yaklasikMaliyetDegeri` daima `0.0` gelir. Yaklaşık maliyetin, teklif sayılarının ve
yüklenici adresi/uyruğunun **tek doğru kaynağı** bu ilan HTML'idir.

İlan, sözleşmeye `ilanList[].sozlesmeId == sozlesmeBilgiList[].id` ile bağlanır.

Ayrıştırıcı asla exception fırlatmaz; bulunamayan etiket sonuç sözlüğünde YER ALMAZ
(None yazılmaz) — böylece çağıran "veri yok" ile "değer 0" arasını ayırabilir.
"""
import html
import re

from .constants import CITIES
from .utils import parse_money

# İl adı → ekap il id (adres sonundaki "İLÇE/İL" token'ından şehir çıkarmak için)
_CITY_BY_NAME = {ad.upper(): ekap_il_id for ekap_il_id, _plaka, ad, _big in CITIES}


def _plain_text(veri_html: str) -> str:
    """HTML → düz metin (tag sıyır, entity çöz, boşluk sıkıştır)."""
    text = re.sub(r"<[^>]+>", " ", veri_html or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _money_after(text: str, label_pattern: str):
    """`label ... : <tutar> TRY` kalıbını yakalar."""
    m = re.search(label_pattern + r"\s*:?\s*([\d.,]+)\s*(?:TRY|TL|₺)", text, re.IGNORECASE)
    return parse_money(m.group(1)) if m else None


def _int_after(text: str, label_pattern: str):
    m = re.search(label_pattern + r"\s*:?\s*(\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _text_after(text: str, label_pattern: str, stop_pattern: str):
    m = re.search(label_pattern + r"\s*:?\s*(.+?)(?=" + stop_pattern + r")", text, re.IGNORECASE)
    return m.group(1).strip(" .,") if m else None


def parse_sonuc_ilani(veri_html: str) -> dict:
    """
    Sonuç İlanı HTML'inden yapılandırılmış veri çıkarır.

    Dönen olası anahtarlar:
        yaklasik_maliyet, kisim_yaklasik_maliyet, sozlesme_bedeli,
        teklif_sayisi, gecerli_teklif_sayisi, dokuman_indiren_sayisi,
        yuklenici_adi, uyruk, adres, il_adi, il_id
    """
    out = {}
    text = _plain_text(veri_html)
    if not text:
        return out

    # ⚠️ SIRA KRİTİK: önce "Sözleşmeye Esas Kısımlarının Yaklaşık Maliyeti" yakalanıp o
    # aralık metinden ÇIKARILIR, sonra çıplak "Yaklaşık Maliyeti" aranır. Aksi halde genel
    # desen kısım satırını yutar. `d)` / `e)` harflerine tutunulmaz — mal/yapım/hizmet
    # şablonları arasında kayıyorlar.
    kisim_re = re.compile(
        r"S[öo]zle[şs]meye\s+Esas\s+K[ıi]s[ıi]mlar[ıi]n[ıi]n\s+Yakla[şs][ıi]k\s+Maliyeti"
        r"\s*:?\s*([\d.,]+)\s*(?:TRY|TL|₺)",
        re.IGNORECASE,
    )
    m = kisim_re.search(text)
    if m:
        val = parse_money(m.group(1))
        if val is not None:
            out["kisim_yaklasik_maliyet"] = val
        text_wo_kisim = text[:m.start()] + " " + text[m.end():]
    else:
        text_wo_kisim = text

    ym = _money_after(text_wo_kisim, r"Yakla[şs][ıi]k\s+Maliyeti")
    if ym is not None:
        out["yaklasik_maliyet"] = ym

    # 4- Sözleşmenin b) Bedeli ("… Başvuru Bedeli" ile karışmasın)
    bedel = _money_after(text, r"(?<!Ba[şs]vuru )Bedeli")
    if bedel is not None:
        out["sozlesme_bedeli"] = bedel

    # 3- Teklifler
    n = _int_after(text, r"Toplam\s+Ge[çc]erli\s+Teklif\s+Say[ıi]s[ıi]")
    if n is not None:
        out["gecerli_teklif_sayisi"] = n
    # "Toplam Teklif Sayısı" — "Toplam Geçerli Teklif Sayısı" ile karışmasın diye
    # araya "Geçerli" girmediğinden emin ol
    m = re.search(r"Toplam\s+Teklif\s+Say[ıi]s[ıi]\s*:?\s*(\d+)", text, re.IGNORECASE)
    if m:
        out["teklif_sayisi"] = int(m.group(1))
    n = _int_after(text, r"indiren\s+say[ıi]s[ıi]")
    if n is not None:
        out["dokuman_indiren_sayisi"] = n

    # 4- Sözleşmenin d) Yüklenicisi / e) uyruğu / f) adresi
    yuk = _text_after(text, r"Y[üu]klenicisi", r"\s*[a-z]\)\s*Y[üu]klenicinin|$")
    if yuk:
        out["yuklenici_adi"] = yuk[:500]
    uyruk = _text_after(text, r"Y[üu]klenicinin\s+uyru[ğg]u", r"\s*[a-z]\)\s*|$")
    if uyruk:
        out["uyruk"] = uyruk[:100]
    adres = _text_after(
        text, r"Y[üu]klenicinin\s+adresi",
        r"\s*Kamuoyuna\s+sayg[ıi]yla|\s*[a-z]\)\s*Y[üu]klenicinin|$",
    )
    if adres:
        out["adres"] = adres[:500]
        il = _il_from_adres(adres)
        if il:
            out["il_adi"], out["il_id"] = il

    return out


def _il_from_adres(adres: str):
    """
    Adresin sonundaki "İLÇE/İL" token'ından şehri çıkarır. **Muhafazakâr** — bilinen bir
    il adına birebir eşleşmezse hiçbir şey döndürmez.
    """
    if not adres:
        return None
    # "İLÇE / İL" kalıbında slash'tan SONRAKİ tek sözcük (boşluk yutmaz — aksi halde
    # "ATAKUM / SAMSUN ATAKUM/SAMSUN" adresinde "SAMSUN ATAKUM" yakalanıyordu).
    for cand in reversed(re.findall(r"/\s*([A-ZÇĞİÖŞÜa-zçğıöşü]+)", adres)):
        name = cand.strip().upper()
        if name in _CITY_BY_NAME:
            return name, _CITY_BY_NAME[name]
    return None
