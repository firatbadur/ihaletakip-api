"""
Sonuç İlanı `ilanXml` ayrıştırıcısı (mobil API).

⚠️ **Para alanları için `ekap/sonuc_ilani.py` YETERLİDİR** — ölçüldü (2024/1362677,
13 sonuç ilanı): mobilin `ilanHtml`'i v2'nin `veriHtml`'iyle aynı belgedir ve mevcut
ayrıştırıcı birebir aynı değerleri üretiyor (kısım YM, ihale YM, bedel, teklif
sayıları, yüklenici adresi/uyruğu). O yüzden burada para yeniden ayrıştırılmaz;
tek çıkarım kaynağı kuralı korunur.

Bu modül XML'in **HTML'de olmayan** iki katkısını alır:

  1. `IhaleKisimYMGosterilsinMi` — kısım maliyetinin gerçekten yayımlanıp
     yayımlanmadığının **kaynağın kendi beyanı**. Bugün bunu `sync._kisim_maliyeti_belirsiz`
     *tahmin ediyor* (çok sözleşmeli + kısım YM == ihale YM → belirsiz). Beyan varken
     tahmine gerek yok.
  2. `IhaleKazanan` — yüklenicinin **tam** ünvanı. HTML ayrıştırıcısı bu alanı
     durdurucu desende kesiyor ("OSEKA ÖZEL SAĞLIK HİZMETLERİ" ↔ XML'de tam ünvan);
     firma kimliği kanonik ünvandan üretildiği için (bkz. `ekap/contractors.py`)
     kesilmiş ad **mükerrer firma** doğurur.
"""
import logging
import xml.etree.ElementTree as ET

logger = logging.getLogger("ihaletakip")


def parse(xml_metin: str) -> dict:
    """
    Bulunan alanları döndürür. ⚠️ Bulunamayan etiket sözlüğe **KONMAZ** (None yazılmaz)
    — çağıran "veri yok" ile "değer 0"ı ayırt edebilmeli (`parse_sonuc_ilani` ile
    aynı sözleşme). Bozuk XML'de boş sözlük döner, asla exception fırlatmaz.
    """
    if not xml_metin:
        return {}
    try:
        root = ET.fromstring(xml_metin)
    except ET.ParseError as e:
        logger.warning("Sonuç ilanı XML ayrıştırılamadı: %s", e)
        return {}

    out = {}
    for etiket, alan in (
        ("IhaleKazanan", "yuklenici_adi"),
        ("IhaleKayitNo", "ikn"),
        ("SozlesmeTarih", "sozlesme_tarihi"),
        ("SozlesmeSuresi", "sozlesme_suresi"),
        ("IdareAdi", "idare_adi"),
        ("IhaleAdi", "ihale_adi"),
        ("IhaleUsul", "ihale_usul"),
        ("YukleniciUyruk", "uyruk"),
        ("IstekliAdres", "adres"),
    ):
        deger = (root.findtext(etiket) or "").strip()
        if deger:
            out[alan] = deger

    # ⚠️ Üç değerli: etiket yoksa anahtar hiç konmaz ("bilinmiyor"), varsa bool.
    ham = root.findtext("IhaleKisimYMGosterilsinMi")
    if ham is not None and str(ham).strip() != "":
        out["kisim_ym_gosterilsin"] = str(ham).strip() in ("1", "true", "True")

    # Çapraz kontrol için (yazıma girmez; HTML ayrıştırıcısı ile karşılaştırmak için).
    for etiket, alan in (
        ("IhaleYaklasikMaliyet", "yaklasik_maliyet"),
        ("IhaleKisimYaklasikMaliyet", "kisim_yaklasik_maliyet"),
        ("SozlesmeBedel", "sozlesme_bedeli"),
    ):
        deger = _ondalik(root.findtext(etiket))
        if deger is not None:
            out[alan] = deger
    return out


def _ondalik(ham):
    """
    XML sayısı → `Decimal`.

    ⚠️ `utils.parse_money` KULLANILMAZ: o, EKAP'ın karışık TR/EN biçimlerini tespit
    etmek için yazıldı ve tek `.` ayırıcılı üç haneli son grubu **binlik** sayar
    ("18,128" → 18128). Bu XML makine üretimi ve **daima `.` ondalıktır**
    ("2220750.00", "65596282.36") → tespit mantığı burada zarar verir.
    """
    from decimal import Decimal, InvalidOperation

    if ham is None:
        return None
    s = str(ham).strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None
