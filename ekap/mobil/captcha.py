"""
EKAP Mobil CAPTCHA akışı — OCR ile otomatik çözüm + insan yedeği.

Hız sınırı aşılınca mobil API **`HTTP 300`** ve gövde olarak tam 16 baytlık
`CAPTCHA_REQUIRED` döndürür. Engel kalkması için:

    Captcha/Getir  → {captchaId, captchaImage (base64 PNG)}
    (cevap üretilir)
    Captcha/Sonuc  → {"success": true}  → sorgular kaldığı yerden devam eder

⚠️ **`resp.status_code == 200` kontrolü YETMEZ.** HTTP 300 standartta "Multiple
Choices"tır; istemci kütüphaneleri hata saymaz ve `r.status_code == 200` kontrolü
yapan kod sessizce boş veriyle devam eder. Bu yüzden tespit gövdeye de bakar.

**Otomatik çözüm** `ekap/tools/ocr.py` (Tesseract, yerel, ücretsiz) ile yapılır —
EKAP'a resmî başvuru sonrası onaylanmış kullanım biçimi budur. Başarısız olursa
akış **insan-döngüye** düşer: resim admin panosunda gösterilir, bir kişi 6 karakteri
yazar. ⚠️ Sistem hiçbir koşulda **sessizce durmaz**; ya çözer ya operatöre sorar.

⚠️ Çözüm **tek süreçte** yapılır (Redis kilidi): paralel denemeler aynı oturumu
birbirine kırdırır ve captcha'ları boşa tüketir.
⚠️ `success:false` gelirse aynı `captchaId` **tekrar denenmez** — yeni `Getir` şarttır.
"""
import json
import logging
import string

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..tools import OCRHatasi, PSM_TEK_SATIR, resimden_metin_cikar

logger = logging.getLogger("ihaletakip")

# AppSetting anahtarları — SSH'sız operatör yolu (admin → Uygulama Ayarları) ve
# panonun okuduğu durum satırı.
ANAHTAR_BEKLEYEN = "ekap_mobil_captcha"
ANAHTAR_DURUM = "ekap_mobil_captcha_durum"

_KILIT = "ekap:mobil:captcha:kilit"
_KILIT_TTL = 180
# Çözülemeyen captcha sonrası bekleme bayrağı. Taban 30 dk: ölçülen soğuma
# süresi 4 dk ile 30 dk arasında, 4 dk'da hâlâ engelliydi.
_BEKLET = "ekap:mobil:captcha:beklet"
_BEKLET_TABAN = 1800
_BEKLET_TAVAN = 4 * 3600

# Harf + rakam, büyük/küçük karışık; sonda genellikle 2 rakam ("ExbT61", "SBbfqB67").
WHITELIST = string.ascii_letters + string.digits
# ⚠️ **Uzunluk SABİT DEĞİL.** İlk sürüm `== 6` kontrolü yapıyordu; ölçümde 8 örneğin
# 4'ü 7-9 karakterdi ve doğru okunan cevaplar bu yüzden atılıyordu (isabet %50'ye
# düşmüştü). Alt/üst sınır yalnızca patolojik çıktıyı (tek harf, sayfa dolusu gürültü)
# elemek içindir.
UZUNLUK_ALT = 4
UZUNLUK_UST = 12


def captcha_mi(status_code: int, metin: str) -> bool:
    """Yanıt bir CAPTCHA duvarı mı? ⚠️ Yalnızca status koduna bakmak yetmez."""
    if status_code == 300:
        return True
    return "CAPTCHA" in (metin or "")[:200].upper()


def bekliyor_mu() -> bool:
    """Çözülemeyen bir captcha yüzünden geri çekilme süresi doluyor mu?"""
    try:
        return bool(cache.get(_BEKLET))
    except Exception:                                   # noqa: BLE001
        return False


def _beklet_uzat():
    """Üstel geri çekilme — her başarısız turda süre iki katına çıkar."""
    try:
        mevcut = int(cache.get(_BEKLET) or 0)
        yeni = min(_BEKLET_TAVAN, max(_BEKLET_TABAN, mevcut * 2))
        cache.set(_BEKLET, yeni, timeout=yeni)
        return yeni
    except Exception:                                   # noqa: BLE001
        return _BEKLET_TABAN


def _beklet_temizle():
    try:
        cache.delete(_BEKLET)
    except Exception:                                   # noqa: BLE001
        pass


# ── Durum kaydı (pano + operatör) ──────────────────────
def _durum_yaz(mesaj: str):
    from core.models import AppSetting
    AppSetting.objects.update_or_create(
        key=ANAHTAR_DURUM,
        defaults={"value": f"{timezone.now():%Y-%m-%d %H:%M} — {mesaj}"[:2000],
                  "description": "EKAP mobil captcha durumu (otomatik yazılır)"},
    )


def bekleyen_kaydet(captcha_id: str, resim_b64: str):
    """İnsan yedeği için bekleyen captcha'yı saklar (admin ekranı bunu okur)."""
    from core.models import AppSetting
    AppSetting.objects.update_or_create(
        key=ANAHTAR_BEKLEYEN,
        defaults={"value": json.dumps({
            "captchaId": captcha_id,
            "resim": resim_b64,
            "ts": timezone.now().isoformat(),
        }), "description": "Operatör çözümü bekleyen EKAP mobil captcha"},
    )


def bekleyen_oku() -> dict | None:
    from core.models import AppSetting
    ham = AppSetting.get(ANAHTAR_BEKLEYEN, "")
    if not ham:
        return None
    try:
        return json.loads(ham)
    except ValueError:
        return None


def bekleyen_durum() -> str:
    """Son captcha durumu (pano ve komut için)."""
    from core.models import AppSetting
    return AppSetting.get(ANAHTAR_DURUM, "")


def bekleyen_temizle():
    from core.models import AppSetting
    AppSetting.objects.filter(key=ANAHTAR_BEKLEYEN).delete()


# ── OCR ────────────────────────────────────────────────
def ocr_coz(resim_b64: str) -> str:
    """
    Captcha resmini metne çevirir. Çözemezse **boş string** döner (hata değil).

    Parametreler **ölçümle** seçildi (8 etiketli örnek, 12 kombinasyon):

        psm=7  buyut=2 → 7/8   psm=8  buyut=2 → 6/8
        psm=6  buyut=2 → 7/8   psm=13 buyut=2 → 6/8
        psm=7  buyut=3 → 6/8   psm=7  buyut=4 → 6/8

    ⚠️ **Büyütmek burada KÖTÜLEŞTİRİYOR.** `tools/ocr.py`'nin genel kuralı (küçük
    görseli büyüt) bu captcha'da geçerli değil: harfler zaten ~40 px ve temiz, 3-4×
    büyütme kenarları yumuşatıp `s→S`, `D→J` karışmasına yol açıyor.
    ⚠️ Tek hata, metni kenardan **kırpılmış** (canvas'a sığmamış) örnekti — o kare
    bir insan için de eksik; OCR'ı zorlamak çözmez, yeni captcha istemek çözer.
    """
    try:
        ham = resimden_metin_cikar(
            resim_b64,
            dil=getattr(settings, "EKAP_MOBIL_CAPTCHA_DIL", "eng"),
            psm=getattr(settings, "EKAP_MOBIL_CAPTCHA_PSM", PSM_TEK_SATIR),
            whitelist=WHITELIST,
            buyut=getattr(settings, "EKAP_MOBIL_CAPTCHA_BUYUT", 2),
        )
    except OCRHatasi as e:
        logger.warning("captcha OCR hatası: %s", e)
        return ""
    # Tesseract araya boşluk/satır sonu koyabiliyor; captcha tek blok.
    cevap = "".join(ch for ch in ham if ch in WHITELIST)
    if not (UZUNLUK_ALT <= len(cevap) <= UZUNLUK_UST):
        logger.info("captcha OCR uzunluk dışı (%s karakter): %r", len(cevap), cevap)
        return ""
    return cevap


# ── Ana akış ───────────────────────────────────────────
def coz(client) -> bool:
    """
    CAPTCHA duvarını aşmayı dener. Başarılıysa `True`, değilse `False`.

    `client` = `EkapMobilClient`. Düşük seviye `_ham_istek` kullanılır: normal
    `_post` captcha tespitinde buraya geri döner ve sonsuz özyineleme olurdu.
    """
    if not cache.add(_KILIT, "1", timeout=_KILIT_TTL):
        logger.info("captcha çözümü başka bir süreçte, atlanıyor")
        return False
    try:
        return _coz(client)
    finally:
        cache.delete(_KILIT)


def _coz(client) -> bool:
    otomatik = getattr(settings, "EKAP_MOBIL_CAPTCHA_OCR", True)
    deneme = getattr(settings, "EKAP_MOBIL_CAPTCHA_DENEME", 3)
    son_id, son_resim = "", ""

    for i in range(deneme if otomatik else 1):
        veri = client.captcha_getir()
        son_id = str(veri.get("captchaId") or "")
        son_resim = str(veri.get("captchaImage") or "")
        if not son_id or not son_resim:
            logger.error("Captcha/Getir beklenen alanları döndürmedi: %s",
                         list(veri)[:10])
            break

        if not otomatik:
            break

        cevap = ocr_coz(son_resim)
        if not cevap:
            continue
        if client.captcha_sonuc(son_id, cevap):
            logger.info("captcha OCR ile çözüldü (deneme %s)", i + 1)
            _beklet_temizle()
            bekleyen_temizle()
            _durum_yaz(f"OCR ile çözüldü (deneme {i + 1})")
            return True
        # ⚠️ Aynı captchaId tekrar denenmez — yeni Getir ile döngü devam eder.
        logger.info("captcha cevabı reddedildi (deneme %s): %r", i + 1, cevap)

    # Otomatik çözüm tükendi → insan-döngü. Sistem sessizce durmaz.
    if son_id and son_resim:
        bekleyen_kaydet(son_id, son_resim)
    süre = _beklet_uzat()
    _durum_yaz(
        f"ÇÖZÜLEMEDİ — operatör bekleniyor. {süre // 60} dk geri çekilme. "
        f"Admin → EKAP Mobil Captcha ya da `manage.py mobil_captcha --cevap XXXXXX`."
    )
    logger.error(
        "EKAP mobil captcha çözülemedi — toplama %s dk duraklatıldı. "
        "Admin panosundan ya da `manage.py mobil_captcha` ile çözün.", süre // 60,
    )
    return False


def insan_cevapla(client, cevap: str) -> bool:
    """Operatörün girdiği cevabı bekleyen captcha için gönderir."""
    bekleyen = bekleyen_oku()
    if not bekleyen:
        raise ValueError("Bekleyen captcha yok.")
    ok = client.captcha_sonuc(bekleyen["captchaId"], cevap.strip())
    if ok:
        _beklet_temizle()
        bekleyen_temizle()
        _durum_yaz("Operatör tarafından çözüldü")
    else:
        # ⚠️ Reddedilen id yeniden kullanılamaz → bekleyeni düşür, yeni tur gerekli.
        bekleyen_temizle()
        _durum_yaz("Operatör cevabı reddedildi — yeni captcha gerekiyor")
    return ok
