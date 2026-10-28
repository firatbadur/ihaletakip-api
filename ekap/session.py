"""
EKAP insan doğrulaması (Cloudflare Turnstile) oturumu.

⚠️ **Neden var**: EKAP 2026-09-08 akşamı yeni portalı yayımladı ve iki şeyi
birden değiştirdi:

  • AES imza katmanı (`r8fact` + `generateSecurityHeaders` + `X-Ekap-Sec-*`)
    paketten **tamamen kaldırıldı** — artık imzalı istek diye bir şey yok.
  • Yerine **Cloudflare Turnstile insan doğrulaması** kondu. Uygulama açılışta
    `GET /b_han/api/human-verification/status` sorar; `verified:false` ise
    "robot değilim" kutusunu gösterir, çözülen token'ı
    `POST /b_han/api/human-verification/verify` + `X-Turnstile-Token` ile
    gönderir ve sunucu **oturum çerezini** (`ekap.human-verification`)
    doğrulanmış işaretler.

Doğrulanmamış her API isteği F5 BIG-IP ASM tarafından **HTTP 406** + HTML
engel sayfasıyla ("Bu olay için referans numarası: …") reddedilir. Bu 401
DEĞİLDİR: imza şeması rotasyonuyla karıştırmayın, `keyfetch` kurtarma yolu
burada işe yaramaz.

**Çalışma modeli — doğrulamayı bir İNSAN geçer.** Turnstile token'ı sunucu
tarafında Cloudflare'e doğrulatılıyor, üretilemez (token'sız `verify` →
`400 HUMAN_VERIFICATION_INVALID_TOKEN`). Bu yüzden akış şudur:

  1. Bir kişi tarayıcıda `ekapv2.kik.gov.tr/ekap/search` açıp doğrulamayı geçer.
  2. O oturumun `Cookie` başlığı buraya kaydedilir
     (`manage.py ekap_dogrula --cookie …`).
  3. Toplayıcı her isteğe bu çerezi ekler; çerez düşünce görevler kendini geri
     çeker ve bir kişinin yenilemesi beklenir.

⚠️ **Çerez IP'ye bağlı değildir** (ölçüldü 2026-09-09: tarayıcıda üretilen çerez
sunucudan `200` döndürdü) — yani doğrulamayı sunucuda yapmak gerekmez.

⚠️ Çerez **sırdır**: EKAP oturumunun tamamını taşır. `AppSetting` içinde durur,
loglara **asla** tam basılmaz (`maskele`).

⚠️⚠️ **2026-10-27: EKAP doğrulamayı KAPATTI** — durum ucu `{"enabled": false}` dönüyor,
`ekap.human-verification` çerezi hiç oluşmuyor ve çerezsiz istekler `200` alıyor (Mac +
üretim IP'si). Aynı gün üretimden **bayat çerezle** giden istek `428` aldı: kapalıyken
eski çerezi göndermek ZARAR veriyor. EKAP doğrulamayı geri getirebilir → kapı
**uyarlanır** (`dogrulama_gerekli`): kapalıysa çerez gönderilmez, açıksa köprü çerezi
(`tools/ekap-cerez-eklentisi/`) gerekir. 406/428 görülünce durum yeniden sorulur.
"""
import logging
import re

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

# AppSetting anahtarları
ANAHTAR_CEREZ = "ekap_dogrulama_cerezi"
ANAHTAR_DURUM = "ekap_dogrulama_durum"

# Redis: çerez her EKAP isteğinde okunuyor (≈1 istek/sn, 8 worker) → DB'ye her
# seferinde gitmemek için kısa önbellek. TTL kısa ki `ekap_dogrula` ile konan
# yeni çerez tüm süreçlerde hızla görünsün.
_CEREZ_CACHE = "ekap:dogrulama:cerez"
_CEREZ_TTL = 60
# "Doğrulama düştü" bayrağı: 406 gören ilk istek koyar, görevler buna bakıp
# bedavaya atlar. Kalıcı DEĞİL — yeni çerez konunca ya da TTL dolunca yeniden
# denenir (EKAP tarafı geçici bir arıza yaşamış olabilir).
_DUSTU_CACHE = "ekap:dogrulama:dustu"
_DUSTU_TTL = 900

DOGRULAMA_YOLU = "/b_han/api/human-verification/status"

# "EKAP doğrulama istiyor mu?" — EKAP'ın kendi durum ucundan, küme geneli önbellekli.
# "1" açık · "0" kapalı · "?" öğrenilemedi (kısa TTL, kapı kapalı sayılır).
_GEREKLI_CACHE = "ekap:dogrulama:gerekli"
_GEREKLI_TTL = 300
_GEREKLI_HATA_TTL = 60
# Son KESİN cevap + zamanı, uzun ömürlü — yalnızca pano okur. Kapı önbelleği 5 dk'da
# düşüyor ve görev de 5 dk'da bir koşuyor → pano çoğu zaman boş önbelleğe bakıp eski
# "DÜŞTÜ" kaydını gösteriyordu (2026-10-28, v2 sorunsuz çalışırken).
_GEREKLI_SON = "ekap:dogrulama:gerekli:son"
_GEREKLI_SON_TTL = 2 * 86400

# Analitik/izleme çerezleri: doğrulamayla ilgisi yok, üstelik kaydeden kişinin
# kimliğini taşır → saklamayız.
_ATILACAK = ("_ga", "_gid", "_gcl", "_hj", "_fb", "_uet", "_clck", "_clsk")


def maskele(cerez: str) -> str:
    """Log/ekran için güvenli özet — çerezin kendisi asla tam basılmaz."""
    if not cerez:
        return "(yok)"
    adlar = [p.split("=", 1)[0].strip() for p in cerez.split(";") if "=" in p]
    return f"{len(cerez)} karakter, çerezler: {', '.join(adlar) or '(ayrıştırılamadı)'}"


def temizle(cerez: str) -> str:
    """Ham `Cookie` başlığından yalnızca gerekli çerezleri süzer.

    Tarayıcıdan kopyalanan başlık analitik çerezleri de taşır; onları saklamak
    hem gereksiz hem de kaydeden kişinin izini tutmak olur.
    """
    parcalar = []
    for p in (cerez or "").split(";"):
        p = p.strip()
        if "=" not in p:
            continue
        ad = p.split("=", 1)[0].strip()
        if any(ad.startswith(x) for x in _ATILACAK):
            continue
        parcalar.append(p)
    return "; ".join(parcalar)


def cerez():
    """Kayıtlı doğrulama çerezi (yoksa boş string). Redis önbellekli."""
    val = cache.get(_CEREZ_CACHE)
    if val is not None:
        return val
    from core.models import AppSetting
    val = AppSetting.get(ANAHTAR_CEREZ, "")
    cache.set(_CEREZ_CACHE, val, _CEREZ_TTL)
    return val


def kaydet(ham_cerez: str) -> str:
    """Yeni doğrulama çerezini kalıcı yazar, 'düştü' bayrağını kaldırır."""
    from core.models import AppSetting
    temiz = temizle(ham_cerez)
    if not temiz:
        raise ValueError("Çerez ayrıştırılamadı (ad=değer çifti bulunamadı).")
    AppSetting.objects.update_or_create(
        key=ANAHTAR_CEREZ,
        defaults={"value": temiz,
                  "description": "EKAP insan doğrulaması oturum çerezi (elle yenilenir)"},
    )
    AppSetting.objects.update_or_create(
        key=ANAHTAR_DURUM,
        defaults={"value": f"yenilendi {timezone.now():%Y-%m-%d %H:%M}",
                  "description": "EKAP doğrulama durumu (otomatik yazılır)"},
    )
    cache.set(_CEREZ_CACHE, temiz, _CEREZ_TTL)
    cache.delete(_DUSTU_CACHE)
    logger.info("EKAP doğrulama çerezi güncellendi (%s)", maskele(temiz))
    return temiz


def dustu(sebep: str = ""):
    """406 gören istek çağırır: görevler kendini geri çeksin.

    ⚠️ Kalıcı bir kayıt da yazılır (`AppSetting`) — bayrak Redis'te TTL'li
    olduğu için tek başına "ne zaman düştü" sorusunu cevaplayamaz ve arıza
    admin'de görünmez kalırdı.
    """
    # EKAP doğrulamayı yeniden açmış olabilir → bir sonraki kapı durumu yeniden sorsun.
    cache.delete(_GEREKLI_CACHE)
    if cache.add(_DUSTU_CACHE, "1", timeout=_DUSTU_TTL):
        from core.models import AppSetting
        AppSetting.objects.update_or_create(
            key=ANAHTAR_DURUM,
            defaults={"value": f"DÜŞTÜ {timezone.now():%Y-%m-%d %H:%M} — {sebep[:200]}",
                      "description": "EKAP doğrulama durumu (otomatik yazılır)"},
        )
        logger.error(
            "EKAP insan doğrulaması geçersiz — toplama durdu. Bir kişinin "
            "tarayıcıda doğrulayıp `manage.py ekap_dogrula` ile çerezi "
            "yenilemesi gerekiyor. (%s)", sebep[:200]
        )


def dogrulama_gerekli(canli: bool = True):
    """EKAP şu an insan doğrulaması istiyor mu? `True` / `False` / `None` (bilinmiyor).

    `canli=False` önbellekte yoksa EKAP'a sormaz (`None`) — admin panosu her açılışta
    dışarıya istek atmasın.
    """
    deger = cache.get(_GEREKLI_CACHE)
    if deger is None and canli:
        try:
            d = durum(cerezli=False)
            deger = "?" if d.get("http") != 200 else ("0" if d.get("enabled") is False else "1")
        except Exception as e:                      # noqa: BLE001
            logger.warning("EKAP doğrulama durumu sorulamadı: %s", e)
            deger = "?"
        cache.set(_GEREKLI_CACHE, deger,
                  _GEREKLI_HATA_TTL if deger == "?" else _GEREKLI_TTL)
        if deger != "?":
            cache.set(_GEREKLI_SON, f"{deger}|{timezone.now().isoformat()}", _GEREKLI_SON_TTL)
    return {"0": False, "1": True}.get(deger)


def son_bilinen_gerekli():
    """`(gerekli, zaman)` — son kesin cevap; hiç yoksa `(None, None)`. EKAP'a sormaz."""
    from datetime import datetime
    ham = cache.get(_GEREKLI_SON) or ""
    deger, _, zaman = ham.partition("|")
    if deger not in ("0", "1") or not zaman:
        return None, None
    return deger == "1", datetime.fromisoformat(zaman)


def istek_cerezi() -> str:
    """İsteğe eklenecek çerez: doğrulama KAPALIYKEN boş (bayat çerez 428 tetikliyor)."""
    if dogrulama_gerekli() is False:
        return ""
    return cerez()


def gecerli() -> bool:
    """Görev başlangıcı kapısı.

    Yakın zamanda düştüyse kapalı. Doğrulama kapalıysa çerezsiz açık; açıksa köprü
    çerezi gerekir. EKAP'ın durumu öğrenilemediyse kapalı (tahminle istek atılmaz).
    """
    if cache.get(_DUSTU_CACHE):
        return False
    gerekli = dogrulama_gerekli()
    if gerekli is None:
        return False
    return (not gerekli) or bool(cerez())


def durum(cerezli: bool = True):
    """EKAP'a sorup doğrulamanın canlı durumunu döner.

    `cerezli=False`: kayıtlı çerez eklenmez — "EKAP doğrulama istiyor mu" küme geneli
    bir sorudur, bayat çerez cevabı bozmasın.

    ⚠️ **GET isteğine `Content-Type` EKLEMEYİN** — F5 ASM bunu protokol ihlali
    sayıp `406` döndürür ve teşhis "doğrulama geçersiz" sanılır (ölçüldü).
    """
    from curl_cffi import requests as curl_requests
    from django.conf import settings

    base = settings.EKAP_BASE_URL.rstrip("/")
    h = {
        "Accept": "application/json",
        "Accept-Language": "tr-TR,tr;q=0.9",
        "api-version": "v1",
        "Origin": base,
        "Referer": f"{base}/ekap/search",
    }
    c = cerez() if cerezli else ""
    if c:
        h["Cookie"] = c
    s = curl_requests.Session(impersonate=getattr(settings, "EKAP_IMPERSONATE", "chrome"))
    r = s.get(base + DOGRULAMA_YOLU, headers=h, timeout=settings.EKAP_TIMEOUT)
    if r.status_code != 200:
        return {"http": r.status_code, "hata": re.sub(r"\s+", " ", r.text)[:200]}
    try:
        veri = r.json()
    except ValueError:
        return {"http": 200, "hata": "JSON değil"}
    veri["http"] = 200
    return veri
