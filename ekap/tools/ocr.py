"""
Base64 resimden metin çıkarma (OCR) — **ücretsiz, yerel**.

Tek genel amaçlı fonksiyon: :func:`resimden_metin_cikar`.

Motor: **Tesseract** (`pytesseract` + `Pillow`). Ücretsizdir, tamamen yerel
çalışır, dışarıya istek atmaz ve token harcamaz.

⚠️ Sistem bağımlılığı var: `tesseract` ikilisi kurulu olmalı
(Dockerfile'da `tesseract-ocr` + `tesseract-ocr-tur`, macOS'ta
`brew install tesseract tesseract-lang`). Kurulu değilse
:class:`OCRHatasi` net bir mesajla döner — sessizce boş string DEĞİL.

⚠️ Ağır importlar (`pytesseract`, `PIL`) fonksiyon içinde yapılır — proje kuralı
(`manage.py check` bu paketler olmadan da çalışmalı).
"""
import base64
import binascii
import io
import logging
import re
import shutil

from django.conf import settings

logger = logging.getLogger("ihaletakip")

AZAMI_BAYT = 10 * 1024 * 1024

# Page segmentation mode kısayolları. Tesseract'ın varsayılanı (3 = otomatik
# sayfa analizi) tek satırlık küçük görsellerde (kod/etiket/captcha) sık sık
# BOŞ döner — sayfa düzeni bulamaz. Bu yüzden kısa girdilerde 7/8 kullanılır.
PSM_SAYFA = 3      # çok satırlı belge
PSM_TEK_SATIR = 7  # tek satır metin
PSM_TEK_KELIME = 8 # tek kelime (kod, doğrulama metni)


class OCRHatasi(Exception):
    """Girdi/ortam hatası; çağıran istemciye mesajı gösterebilir."""


# ── Base64 çözme ───────────────────────────────────────
def _ham_bayt(veri) -> bytes:
    """`data:` öneki, boşluk ve satır sonlarını temizleyip base64'ü çözer."""
    if isinstance(veri, (bytes, bytearray)):
        veri = veri.decode("ascii", errors="ignore")
    if not isinstance(veri, str) or not veri.strip():
        raise OCRHatasi("Base64 resim verisi boş.")

    veri = veri.strip()
    if veri.startswith("data:"):
        # data:image/png;base64,iVBOR...
        _, _, veri = veri.partition(",")
    veri = "".join(veri.split())

    # ⚠️ `len % 4 == 1` matematiksel olarak İMKÂNSIZDIR (base64 4'lük bloklar hâlinde
    # 3 bayt kodlar; artık 2 ya da 3 karakter olabilir, 1 olamaz) → veri kopyalanırken
    # karakter kaybetmiştir. Dolgu ekleyip devam etmek çözülebilir ama BOZUK bir bayt
    # dizisi üretir ve hata çok sonra, anlamsız bir "resim çözümlenemedi" olarak çıkar.
    if len(veri) % 4 == 1:
        raise OCRHatasi(
            "Base64 verisi eksik/bozuk (uzunluk geçersiz) — metin kopyalanırken "
            "karakter kaybolmuş olabilir."
        )
    # Eksik dolgu (padding) yaygın bir kopyala-yapıştır hatası; sessizce tamamla.
    veri += "=" * (-len(veri) % 4)
    try:
        return base64.b64decode(veri, validate=True)
    except (binascii.Error, ValueError) as e:
        raise OCRHatasi("Geçersiz base64 resim verisi.") from e


# ── Ön işleme ──────────────────────────────────────────
def _hazirla(bayt: bytes, buyut: int):
    """
    PIL görüntüsünü OCR'a hazırlar: RGBA düzleştirme → gri ton → büyütme.

    ⚠️ **RGBA'yı doğrudan `convert("L")` YAPMAYIN**: saydam pikseller siyaha
    düşer ve açık zemindeki koyu metin siyah üstüne siyah olur (boş sonuç).
    Önce beyaz zemine yapıştırılır.

    ⚠️ Büyütme şart: Tesseract ~30 px'in altındaki harf yüksekliklerinde
    belirgin biçimde kötüleşir; captcha/etiket görselleri tipik olarak 60 px
    yüksekliğindedir.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(bayt))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        zemin = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(zemin, img)
    img = img.convert("L")

    if buyut > 1:
        img = img.resize((img.width * buyut, img.height * buyut), Image.LANCZOS)
    return img


def _temizle(metin: str) -> str:
    """Satır sonu/boşluk gürültüsünü toparlar; boş satırları düşürür."""
    satirlar = [re.sub(r"[ \t ]+", " ", s).strip() for s in metin.splitlines()]
    return "\n".join(s for s in satirlar if s).strip()


# ── Genel fonksiyon ────────────────────────────────────
def resimden_metin_cikar(
    base64_resim,
    *,
    dil: str = None,
    psm: int = None,
    whitelist: str = None,
    buyut: int = 2,
) -> str:
    """
    Base64 kodlanmış bir resmin içindeki metni döndürür.

    `base64_resim` düz base64 ya da `data:image/png;base64,...` biçiminde olabilir.
    Görselde metin yoksa boş string döner (hata DEĞİL).

    Parametreler:
      `dil`       — Tesseract dil kodu (`tur`, `eng`, `tur+eng`). Varsayılan
                    `settings.OCR_DIL`.
      `psm`       — sayfa segmentasyon modu; kısa/tek satırlık görsellerde
                    `PSM_TEK_SATIR` ya da `PSM_TEK_KELIME` verin. Verilmezse
                    görüntü boyutuna göre seçilir.
      `whitelist` — yalnızca bu karakterler tanınsın (ör. doğrulama kodları için
                    `"ABC...xyz0123456789"`). Arama uzayını daraltıp isabeti
                    ciddi biçimde artırır.
      `buyut`     — ön büyütme katsayısı (bkz. :func:`_hazirla`).

    Hatalar: :class:`OCRHatasi` (geçersiz girdi, tesseract kurulu değil).
    """
    import pytesseract

    bayt = _ham_bayt(base64_resim)
    if len(bayt) > AZAMI_BAYT:
        raise OCRHatasi(f"Resim çok büyük ({len(bayt) // 1024} KB); sınır 10 MB.")

    komut = getattr(settings, "TESSERACT_CMD", "") or ""
    if komut:
        pytesseract.pytesseract.tesseract_cmd = komut
    if not shutil.which(komut or pytesseract.pytesseract.tesseract_cmd):
        raise OCRHatasi(
            "Tesseract kurulu değil. Docker: `tesseract-ocr` + `tesseract-ocr-tur`, "
            "macOS: `brew install tesseract tesseract-lang`."
        )

    try:
        img = _hazirla(bayt, buyut)
    except OSError as e:  # PIL bozuk/desteklenmeyen dosyada bunu atar
        raise OCRHatasi("Resim çözümlenemedi (bozuk veya desteklenmeyen biçim).") from e

    if psm is None:
        # Alçak ve dar görsel = tek satır etiket/kod; sayfa analizi orada boş döner.
        psm = PSM_TEK_SATIR if img.height <= 200 * max(buyut, 1) else PSM_SAYFA

    config = f"--psm {psm}"
    if whitelist:
        # ⚠️ Whitelist tırnak içinde verilmeli; boşluk/özel karakter içerebilir.
        config += f' -c tessedit_char_whitelist={whitelist}'

    try:
        metin = pytesseract.image_to_string(
            img, lang=dil or getattr(settings, "OCR_DIL", "tur+eng"), config=config
        )
    except pytesseract.TesseractNotFoundError as e:
        raise OCRHatasi("Tesseract ikilisi bulunamadı (PATH?).") from e
    except pytesseract.TesseractError as e:
        # En sık sebep: dil paketi kurulu değil (`tur.traineddata` yok).
        logger.warning("Tesseract hatası: %s", e)
        raise OCRHatasi(f"OCR başarısız: {e}") from e

    return _temizle(metin)
