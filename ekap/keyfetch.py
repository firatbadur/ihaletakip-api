"""
EKAP imza şemasının **çalışma anında** keşfi (anahtar + başlık adları).

⚠️ **Neden var**: EKAP imza anahtarını (`environment.r8fact`) ve başlık adlarını
döndürüyor — 2026-08-25'te **tek günde iki kez** (`X-Custom-Request-*` →
`X-Correlation-Id/X-Csrf-Token/…` → `X-Ekap-Sec-1..6`, anahtar 24 → 32 bayt).
Her rotasyonda tüm toplama durur ve `.env` elle güncellenene kadar EKAP'a giden
her istek `HTTP 401 "HataKodu: 1200"` alır. Sabit env değeri bu tempoda
sürdürülemez.

**Kaynak = portalın kendi JS paketi** (kullanıcının tarayıcısının okuduğu yer):
    ekapv2.kik.gov.tr/  →  main.<hash>.js  (webpack chunk haritası)
      →  common.<hash>.js   : `@environments` modülü, `r8fact:"<anahtar>"`
      →  <id>.<hash>.js     : `generateSecurityHeaders()`, başlık adları

Sonuç Redis'te önbelleklenir (`_TTL`); web + tüm worker'lar aynı değeri paylaşır,
yani keşif süreç başına değil küme başına bir kez yapılır.

⚠️ **Fallback ZORUNLU**: minify edilmiş gövdeyi regex'le okuyoruz; EKAP derleyici
sürümünü değiştirdiğinde bu parse sessizce boş dönebilir. O durumda
`settings.EKAP_SIGNING_KEY` + `DEFAULT_HEADERS` kullanılır, yani davranış en
kötü ihtimalle bugünkü sabit şemaya geriler — hiçbir zaman "imzasız istek" olmaz.
"""
import logging
import re

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("ihaletakip")

_CACHE_KEY = "ekap:imza:sema"
# Rotasyon günde birkaç kez olabiliyor; TTL kısa tutuldu. Asıl tazeleme yolu
# yine de 401 üzerine `invalidate()` — TTL yalnızca sessiz sapmaya karşı taban.
_TTL = 3600

# Bilinen son şema (2026-08-25 ikinci rotasyon). Keşif başarısız olursa bu kullanılır.
DEFAULT_HEADERS = {
    "guid": "X-Ekap-Sec-3",    # düz metin GUID
    "r8id": "X-Ekap-Sec-1",    # AES(GUID)
    "iv": "X-Ekap-Sec-2",      # IV, Base64
    "ts": "X-Ekap-Sec-4",      # AES(unix_ms)
    "method": "X-Ekap-Sec-5",  # AES("POST")  — opsiyonel
    "path": "X-Ekap-Sec-6",    # AES(url path) — opsiyonel
}

_BASE = "https://ekapv2.kik.gov.tr"


def _fetch(session, url, expect_js=True):
    r = session.get(url, timeout=settings.EKAP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"{url} → HTTP {r.status_code}")
    text = r.text
    # ⚠️ SPA fallback: bilinmeyen yol **HTTP 200 + index.html** döner. JS beklerken
    # HTML almak, parse'ı hatasız ama sonuçsuz bırakır (sessiz başarısızlık).
    if expect_js and text.lstrip().startswith("<!DOCTYPE"):
        raise RuntimeError(f"{url} → JS yerine HTML (chunk adı yanlış?)")
    return text


def _chunk_name(chunk_id, chunk_hash):
    # ⚠️ 2076 webpack'te `common` adıyla yayımlanıyor (`__webpack_require__.u`
    # içinde özel durum) — `@environments` modülü orada, yani anahtar da orada.
    return ("common" if chunk_id == "2076" else chunk_id) + f".{chunk_hash}.js"


def _parse_headers(body):
    """`generateSecurityHeaders()` gövdesinden başlık adı → rol eşlemesi.

    Rol, adın kendisinden DEĞİL yanındaki ifadeden çıkarılır: adlar anlamsız
    (`X-Ekap-Sec-1`) ve rotasyonda değişiyor, ifadeler ise şemanın kendisi.
    """
    roles = {}
    # ts: `"AD":<enc>(new Date(...).getTime().toString())`
    m = re.search(r'"([\w-]+)":\s*\w+\(new Date\([^)]*\)\.getTime\(\)', body)
    if m:
        roles["ts"] = m.group(1)
    # iv: `"AD":<CryptoJS>.enc.Base64.stringify(<iv>)`
    m = re.search(r'"([\w-]+)":\s*\w+\.enc\.Base64\.stringify\(', body)
    if m:
        roles["iv"] = m.group(1)
    # method / path: koşullu ekler — `X["AD"]=<enc>(m.toUpperCase())` ve kalan.
    m = re.search(r'\["([\w-]+)"\]\s*=\s*\w+\([\w$]+\.toUpperCase\(\)\)', body)
    if m:
        roles["method"] = m.group(1)
    for mm in re.finditer(r'\["([\w-]+)"\]\s*=\s*\w+\(([\w$]+)\)', body):
        if mm.group(1) != roles.get("method"):
            roles["path"] = mm.group(1)
    # Kalan iki ad: biri düz GUID, biri AES(GUID). Düz GUID, `newGuid()` sonucunu
    # tutan değişkendir; ciphertext ise `enc(...)` çağrısının sonucunu tutan
    # değişken. İkisini gövdedeki atama sırasından ayırt ederiz.
    guid_var = None
    m = re.search(r'const\s+([\w$]+)\s*=\s*this\.newGuid\(\)\.toString\(\)', body)
    if m:
        guid_var = m.group(1)
    pairs = re.findall(r'"([\w-]+)":\s*([\w$]+)\s*[,}]', body)
    for name, var in pairs:
        if name in roles.values():
            continue
        # `p=s` gibi takma adlar olabildiği için doğrudan eşitlik yeterli değil;
        # GUID değişkeninin takma adlarını da topla.
        if guid_var and (var == guid_var or re.search(
                rf'\b{re.escape(var)}\s*=\s*{re.escape(guid_var)}\b', body)):
            roles["guid"] = name
        else:
            roles["r8id"] = name
    return roles


def discover():
    """Canlı portalden `{"key": str, "headers": {rol: ad}}` çıkarır.

    Hata durumunda istisna fırlatır — çağıran `resolve()` fallback'e düşer.
    """
    from curl_cffi import requests as curl_requests

    session = curl_requests.Session(
        impersonate=getattr(settings, "EKAP_IMPERSONATE", "chrome")
    )
    index = _fetch(session, _BASE + "/", expect_js=False)
    main = re.search(r'src="(main\.[a-f0-9]+\.js)"', index)
    if not main:
        raise RuntimeError("index.html içinde main.<hash>.js bulunamadı")
    main_js = _fetch(session, f"{_BASE}/{main.group(1)}")

    chunks = re.findall(r'(\d{2,5}):"([a-f0-9]{16})"', main_js)
    if not chunks:
        raise RuntimeError("main.js içinde chunk haritası bulunamadı")

    key = None
    headers = {}
    for chunk_id, chunk_hash in chunks:
        if key and headers:
            break
        try:
            js = _fetch(session, f"{_BASE}/{_chunk_name(chunk_id, chunk_hash)}")
        except RuntimeError:
            continue
        if not key:
            m = re.search(r'r8fact:\s*"([^"]+)"', js)
            if m:
                key = m.group(1)
        if not headers and "generateSecurityHeaders" in js:
            i = js.find("generateSecurityHeaders")
            headers = _parse_headers(js[i:i + 1200])

    if not key:
        raise RuntimeError("r8fact bulunamadı")
    if len(key.encode()) not in (16, 24, 32):
        raise RuntimeError(f"r8fact {len(key)} bayt; AES anahtarı olamaz")
    # Zorunlu dört rolden biri eksikse eşleme güvenilmez → tümünü reddet.
    if not all(r in headers for r in ("guid", "r8id", "iv", "ts")):
        logger.warning("EKAP imza başlıkları çözülemedi (%s), varsayılan kullanılıyor",
                       headers)
        headers = dict(DEFAULT_HEADERS)
    return {"key": key, "headers": headers}


def resolve():
    """Önbellekli şema. Keşif başarısızsa env + son bilinen şemaya döner."""
    cached = cache.get(_CACHE_KEY)
    if cached:
        return cached
    try:
        scheme = discover()
        logger.info("EKAP imza şeması keşfedildi: anahtar=%s… başlıklar=%s",
                    scheme["key"][:4], scheme["headers"])
    except Exception as e:
        logger.warning("EKAP imza şeması keşfedilemedi (%s); env'e düşülüyor", e)
        scheme = {"key": settings.EKAP_SIGNING_KEY, "headers": dict(DEFAULT_HEADERS)}
        # Başarısızlığı da (kısa süre) önbellekle: her istekte portala gidip
        # 3 dosya indirmek 401 fırtınasında kendi başına bir yük olurdu.
        cache.set(_CACHE_KEY, scheme, 300)
        return scheme
    cache.set(_CACHE_KEY, scheme, _TTL)
    return scheme


def invalidate():
    """401 sonrası çağrılır → sonraki istek şemayı yeniden keşfeder."""
    cache.delete(_CACHE_KEY)
