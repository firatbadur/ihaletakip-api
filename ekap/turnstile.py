"""
EKAP v2 insan doğrulamasının (Cloudflare Turnstile) SUNUCUDA otomatik geçilmesi.

⚠️⚠️ **Karar değişti (2026-11-13, ürün sahibi kararı).** Eylülden beri "CAPTCHA çözücü
bilinçli olarak kullanılmaz" kuralı vardı; çerezi bir kişinin tarayıcısı taşıyordu
(`tools/ekap-cerez-eklentisi/`). EKAP 2026-11-12'de doğrulamayı geri açınca v2 görevleri
~11,5 saat durdu ve kuyruk birikti → ürün sahibi otomatik çözümü istedi. Köprü eklentisi
YEDEK olarak duruyor (aynı `AppSetting`'e yazar), bu modülle çakışmaz.

**Akış** (portalın kendi akışının aynısı, bkz. EKAP `common` + `7797` chunk'ları):
  1. Gerçek Chromium (patchright, Xvfb altında GÖRÜNÜR — headless reddediliyor) EKAP
     adresini açar; yol yakalanıp yerine yalnızca Turnstile kutusu olan bir sayfa konur.
     Böylece token `ekapv2.kik.gov.tr` alan adına üretilir (Cloudflare bunu denetler).
  2. Kutuya tıklanır, token gelir (Mac'te ölçüldü: ~3-4 sn, tek tık).
  3. `POST /b_han/api/human-verification/verify` + `X-Turnstile-Token` → EKAP
     `ekap.human-verification` çerezini doğrulanmış işaretler (ölçüldü: 20 dk geçerli,
     18. dk'da yenileme). Çerez IP'ye bağlı değil.
  4. Çerez `ekap_session.kaydet` ile yazılır → tüm görevler kullanır.

⚠️ **Yenileme de YENİ token ister**: portalın `refreshIfDue` → `verify(!0)` Turnstile'ı
yeniden çalıştırır. 7/24 için ~18 dk'da bir çözüm (~80/gün).

⚠️ **Headless KULLANILMAZ**: Mac'te headless Chromium 34/34 denemede `600010` aldı
(normal user-agent ile de). Konteyner Xvfb ile görünür tarayıcı çalıştırır.

⚠️ Site anahtarı EKAP paketinden **keşfedilir** (`site_anahtari`), değişirse kendiliğinden
izlenir; keşif çökerse `settings.EKAP_TURNSTILE_SITEKEY` yedeğine düşer.

**Durum ve müdahale panodadır** (`/admin/ekap/dogrulama/`): son başarı/hata, ardışık hata,
ekran görüntüsü, olay günlüğü; "Şimdi çöz", otomatiği aç/kapat, elle çerez.
"""
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import session as ekap_session

logger = logging.getLogger("ihaletakip")

DOGRULAMA_KOK = "/b_han/api/human-verification"

# ── Durum anahtarları ─────────────────────────────────────────────────────────
# Redis (kalıcı volume'lu): sık yazılan durum + günlük; DB'yi her dakika yormamak için.
_DURUM = "ekap:ts:durum"            # JSON — son başarı/hata, bitiş, ardışık hata, sayaçlar
_OLAYLAR = "ekap:ts:olaylar"        # JSON listesi — son 30 olay (pano tablosu)
_EKRAN = "ekap:ts:ekran"            # son başarısız denemenin ekran görüntüsü (base64 PNG)
_KALP = "ekap:ts:kalp"              # daemon nabzı (ISO zaman) — ölü daemon tespiti
_ISTEK = "ekap:ts:istek"            # panodan "şimdi çöz" isteği
_ANAHTAR = "ekap:ts:site_anahtari"  # keşfedilen (sitekey, action)
_SURE_UZUN = 30 * 86400
_OLAY_SAYISI = 30

# AppSetting: panodan açılıp kapatılan kalıcı ayar (Redis boşalsa da kaybolmasın).
ANAHTAR_OTOMATIK = "ekap_turnstile_otomatik"

# Bitişe bu kadar kala yenilenir. EKAP 2 dk kala yeniliyor; görevlerin ortasında
# pencere kapanmasın diye biraz daha erken (ve bir çözüm denemesi ~45 sn sürebilir).
YENILEME_ESIGI_SN = 4 * 60
# Ardışık hatalarda bekleme (sn): 1, 2, 4, 8 dk, sonra 15 dk'da sabit.
GERI_CEKILME = (60, 120, 240, 480, 900)
# Panoda "hata" seviyesi için ardışık hata eşiği ve nabız zaman aşımı.
UYARI_ARDISIK = 3
KALP_ZAMAN_ASIMI_SN = 180


class TurnstileHata(Exception):
    """Çözüm başarısız. `kod` panoda gösterilir (ör. widget `600010`, `verify_400`)."""

    def __init__(self, mesaj, kod="", ekran=b""):
        super().__init__(mesaj)
        self.kod = kod
        self.ekran = ekran


# ── Ayarlar / bayraklar ───────────────────────────────────────────────────────

def otomatik_acik() -> bool:
    from core.models import AppSetting
    return AppSetting.get(ANAHTAR_OTOMATIK, "1") != "0"


def otomatik_ayarla(acik: bool, kim: str = ""):
    from core.models import AppSetting
    AppSetting.objects.update_or_create(
        key=ANAHTAR_OTOMATIK,
        defaults={"value": "1" if acik else "0",
                  "description": "EKAP Turnstile otomatik çözücü açık mı (pano: /admin/ekap/dogrulama/)"},
    )
    olay_ekle("ayar", f"otomatik {'AÇILDI' if acik else 'KAPATILDI'}" + (f" ({kim})" if kim else ""))


def istek_birak(kim: str = ""):
    """Panodaki "Şimdi çöz" — daemon ≤5 sn içinde alır; geri çekilmeyi ve otomatik kapalıyı ezer."""
    cache.set(_ISTEK, kim or "pano", 600)
    olay_ekle("istek", f"elle çözüm istendi ({kim or 'pano'})")


def istek_al():
    """Bekleyen elle istek varsa alır ve siler (tek tüketici)."""
    kim = cache.get(_ISTEK)
    if kim:
        cache.delete(_ISTEK)
    return kim


# ── Durum kaydı ──────────────────────────────────────────────────────────────

def durum_oku() -> dict:
    ham = cache.get(_DURUM)
    try:
        return json.loads(ham) if ham else {}
    except (TypeError, ValueError):
        return {}


def _durum_yaz(d: dict):
    cache.set(_DURUM, json.dumps(d, ensure_ascii=False), _SURE_UZUN)


def olaylar() -> list:
    ham = cache.get(_OLAYLAR)
    try:
        return json.loads(ham) if ham else []
    except (TypeError, ValueError):
        return []


def olay_ekle(tur: str, metin: str, sure_sn=None):
    liste = olaylar()
    liste.insert(0, {"zaman": timezone.now().isoformat(), "tur": tur,
                     "metin": metin[:300], "sure_sn": sure_sn})
    cache.set(_OLAYLAR, json.dumps(liste[:_OLAY_SAYISI], ensure_ascii=False), _SURE_UZUN)


def ekran_oku() -> str:
    return cache.get(_EKRAN) or ""


def kalp_at():
    cache.set(_KALP, timezone.now().isoformat(), 3600)


def kalp_oku():
    ham = cache.get(_KALP)
    return datetime.fromisoformat(ham) if ham else None


def _gun_anahtari(simdi):
    return timezone.localtime(simdi).strftime("%Y-%m-%d")


def basari_kaydet(bitis_iso: str, sure_sn: float, sebep: str):
    simdi = timezone.now()
    d = durum_oku()
    gun = _gun_anahtari(simdi)
    if d.get("gun") != gun:
        d.update(gun=gun, bugun_basari=0, bugun_hata=0)
    d.update(son_basari=simdi.isoformat(), bitis=bitis_iso, ardisik_hata=0,
             sonraki_deneme=None, son_sure_sn=round(sure_sn, 1))
    d["bugun_basari"] = d.get("bugun_basari", 0) + 1
    _durum_yaz(d)
    olay_ekle("basari", f"doğrulandı ({sebep}) · bitiş {_yerel_saat(bitis_iso)}", round(sure_sn, 1))


def hata_kaydet(hata: Exception, sure_sn: float, sebep: str):
    simdi = timezone.now()
    d = durum_oku()
    gun = _gun_anahtari(simdi)
    if d.get("gun") != gun:
        d.update(gun=gun, bugun_basari=0, bugun_hata=0)
    n = d.get("ardisik_hata", 0) + 1
    bekle = GERI_CEKILME[min(n, len(GERI_CEKILME)) - 1]
    kod = getattr(hata, "kod", "") or type(hata).__name__
    d.update(son_hata=simdi.isoformat(), son_hata_metni=str(hata)[:300], son_hata_kodu=kod,
             ardisik_hata=n, sonraki_deneme=(simdi + timedelta(seconds=bekle)).isoformat())
    d["bugun_hata"] = d.get("bugun_hata", 0) + 1
    _durum_yaz(d)
    ekran = getattr(hata, "ekran", b"")
    if ekran:
        cache.set(_EKRAN, base64.b64encode(ekran).decode(), 2 * 86400)
    olay_ekle("hata", f"[{kod}] {str(hata)[:200]} ({sebep}) · {bekle // 60} dk sonra yeniden", round(sure_sn, 1))


def bitis_guncelle(bitis_iso):
    """Başka kaynaktan (köprü eklentisi / elle çerez) gelen çerezin bitişini öğrenince yazar."""
    d = durum_oku()
    d["bitis"] = bitis_iso
    _durum_yaz(d)


def _yerel_saat(iso):
    try:
        return timezone.localtime(_iso(iso)).strftime("%H:%M")
    except (TypeError, ValueError):
        return "?"


def _iso(deger):
    """EKAP `2026-11-13T13:24:23.3304305+00:00` (7 hane kesir) dahil ISO → aware datetime."""
    if not deger:
        return None
    s = str(deger).strip().replace("Z", "+00:00")
    # Python 6 haneden uzun kesri kabul etmez → kırp.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


# ── Karar (saf fonksiyon, testli) ─────────────────────────────────────────────

@dataclass
class Karar:
    eylem: str            # "coz" | "bekle" | "sor"
    sebep: str
    extra: dict = field(default_factory=dict)


def karar_ver(*, simdi, durum: dict, otomatik: bool, istek, gerekli, dustu: bool,
              cerez_var: bool) -> Karar:
    """Bu turda ne yapılacak?

    - `istek` (panodan "Şimdi çöz") her şeyi ezer: otomatik kapalı ya da geri çekilme
      sürüyor olsa da çözülür — operatör bilerek istedi.
    - EKAP doğrulama istemiyorsa (`gerekli is False`) çözülmez: kapalıyken çerez
      göndermek 428 tetikliyordu (session.py), boşuna token harcamayız.
    - `gerekli is None` (EKAP'a ulaşılamadı) → bekle; tahminle çözüm denemesi yok.
    - Geri çekilme süresi dolmadan otomatik deneme yok.
    - Görev 406/428 gördüyse (`dustu`) bitişe bakılmadan çözülür.
    - Bitişi bilinmeyen kayıtlı çerez (köprü/elle) → önce EKAP'a sorulur ("sor").
    """
    if istek:
        return Karar("coz", f"elle istek ({istek})")
    if not otomatik:
        return Karar("bekle", "otomatik kapalı")
    if gerekli is False:
        return Karar("bekle", "EKAP doğrulama istemiyor")
    if gerekli is None:
        return Karar("bekle", "EKAP durumu öğrenilemedi")
    sonraki = _iso(durum.get("sonraki_deneme"))
    if sonraki and simdi < sonraki:
        return Karar("bekle", f"geri çekilme ({int((sonraki - simdi).total_seconds())} sn)")
    if dustu:
        return Karar("coz", "görev doğrulama hatası gördü")
    if not cerez_var:
        return Karar("coz", "kayıtlı çerez yok")
    bitis = _iso(durum.get("bitis"))
    if bitis is None:
        return Karar("sor", "çerezin bitişi bilinmiyor")
    kalan = (bitis - simdi).total_seconds()
    if kalan <= YENILEME_ESIGI_SN:
        return Karar("coz", "süre doluyor" if kalan > 0 else "süre doldu")
    return Karar("bekle", f"geçerli ({int(kalan // 60)} dk kaldı)", {"kalan_sn": kalan})


def bekleme_suresi(karar: Karar, varsayilan: int = 60) -> int:
    """Bir sonraki karar turuna kadar uyku — bitişe yakınsa erken uyan."""
    kalan = karar.extra.get("kalan_sn")
    if kalan is not None:
        return int(max(5, min(varsayilan, kalan - YENILEME_ESIGI_SN + 1)))
    return varsayilan


# ── Site anahtarı keşfi ──────────────────────────────────────────────────────

_SITEKEY_RE = re.compile(r'turnstile:\{[^}]*?siteKey:"(0x[0-9A-Za-z_-]+)"[^}]*?action:"([^"]*)"')


def site_anahtari(yenile: bool = False):
    """`(sitekey, action)` — EKAP paketinden keşif, 6 sa önbellek, hata → settings yedeği."""
    if not yenile:
        ham = cache.get(_ANAHTAR)
        if ham:
            return tuple(ham.split("|", 1))
    try:
        anahtar, eylem = _kesfet()
        cache.set(_ANAHTAR, f"{anahtar}|{eylem}", 6 * 3600)
        return anahtar, eylem
    except Exception as e:                           # noqa: BLE001
        logger.warning("Turnstile site anahtarı keşfedilemedi, ayar yedeği: %s", e)
        return settings.EKAP_TURNSTILE_SITEKEY, settings.EKAP_TURNSTILE_ACTION


def _kesfet():
    from curl_cffi import requests as curl_requests

    from .keyfetch import _chunk_name, _fetch

    base = settings.EKAP_BASE_URL.rstrip("/")
    s = curl_requests.Session(impersonate=getattr(settings, "EKAP_IMPERSONATE", "chrome"))
    index = _fetch(s, f"{base}/ekap/search", expect_js=False)
    main = re.search(r'src="(main\.[a-f0-9]+\.js)"', index)
    if not main:
        raise RuntimeError("main.<hash>.js bulunamadı")
    main_js = _fetch(s, f"{base}/{main.group(1)}")
    # Ayar `common` chunk'ında (2026-11-13). Önce onu, bulunamazsa hepsini tara.
    chunks = re.findall(r'(\d{2,5}):"([a-f0-9]{16})"', main_js)
    chunks.sort(key=lambda c: c[0] != "2076")
    for cid, chash in chunks:
        try:
            js = _fetch(s, f"{base}/{_chunk_name(cid, chash)}")
        except RuntimeError:
            continue
        m = _SITEKEY_RE.search(js)
        if m:
            return m.group(1), m.group(2)
    raise RuntimeError("paketlerde turnstile.siteKey bulunamadı")


# ── Çözüm ────────────────────────────────────────────────────────────────────

_SAYFA = """<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script></head>
<body><div class="cf-turnstile" data-sitekey="{sitekey}" data-action="{action}"
data-callback="tsOk" data-error-callback="tsErr"></div>
<script>function tsOk(t){{document.body.dataset.tok=t}}
function tsErr(e){{document.body.dataset.err=String(e);return true}}</script></body></html>"""
# ⚠️ patchright `evaluate`'i yalıtılmış dünyada çalıştırır → sayfanın `window`
# değişkenleri GÖRÜNMEZ; durum DOM'a (data-*) yazılıp oradan okunur.
_OKU = ("({t: document.body.dataset.tok || (document.querySelector('[name=cf-turnstile-response]')||{}).value || '',"
        " e: document.body.dataset.err || ''})")


def token_al(sitekey: str, action: str, *, sure_sn: int = 45, headless: bool = False) -> str:
    """Gerçek Chromium'da Turnstile token'ı üretir. Başarısızsa `TurnstileHata` (+ ekran)."""
    from patchright.sync_api import sync_playwright

    base = settings.EKAP_BASE_URL.rstrip("/")
    url = f"{base}/ekap/search/"
    sayfa = _SAYFA.format(sitekey=sitekey, action=action)
    with sync_playwright() as p:
        tarayici = p.chromium.launch(headless=headless)
        try:
            # ⚠️ Bağlama `locale`/`user_agent`/viewport EMÜLASYONU VERİLMEZ: Mac'te
            # `locale="tr-TR"` ile 6/6 tık 600010 aldı, aynı makinede emülasyonsuz 1 tıkla
            # geçti (2026-11-13). CDP emülasyonu tarayıcının kendi değerleriyle çelişiyor.
            page = tarayici.new_context().new_page()
            page.route(url, lambda r: r.fulfill(body=sayfa, status=200, content_type="text/html"))
            page.goto(url, timeout=30000)
            t0, tik, son_hata = time.time(), 0, ""
            while time.time() - t0 < sure_sn:
                st = page.evaluate(_OKU)
                if st.get("t"):
                    return st["t"]
                son_hata = st.get("e") or son_hata
                # Kutu iframe'i çizilince 5 sn'de bir sol kenarındaki onay alanına tık.
                if int(time.time() - t0) % 5 == 0 and tik < 6:
                    kutu = page.locator("div.cf-turnstile").bounding_box()
                    if kutu and kutu["width"] > 0:
                        page.mouse.click(kutu["x"] + 30, kutu["y"] + kutu["height"] / 2)
                        tik += 1
                page.wait_for_timeout(1000)
            try:
                ekran = page.screenshot()
            except Exception:                        # noqa: BLE001
                ekran = b""
            kod = son_hata or "token_yok"
            raise TurnstileHata(f"{sure_sn} sn içinde token gelmedi (tık={tik}, widget={son_hata or '-'})",
                                kod=kod, ekran=ekran)
        finally:
            tarayici.close()


def dogrula(token: str):
    """Token'ı EKAP'a verir → `(cookie_basligi, durum_json)`. Başarısızsa `TurnstileHata`."""
    from curl_cffi import requests as curl_requests

    base = settings.EKAP_BASE_URL.rstrip("/")
    h = {"Accept": "application/json", "Accept-Language": "tr-TR,tr;q=0.9",
         "Origin": base, "Referer": f"{base}/ekap/search"}
    s = curl_requests.Session(impersonate=getattr(settings, "EKAP_IMPERSONATE", "chrome"))
    # F5 çerezi (TS01…) — portal da önce durumu sorar. ⚠️ GET'e Content-Type EKLENMEZ.
    s.get(base + DOGRULAMA_KOK + "/status", headers=h, timeout=settings.EKAP_TIMEOUT)
    r = s.post(base + DOGRULAMA_KOK + "/verify", json={},
               headers={**h, "Content-Type": "application/json", "X-Turnstile-Token": token},
               timeout=settings.EKAP_TIMEOUT)
    try:
        veri = r.json()
    except ValueError:
        veri = {}
    if not isinstance(veri, dict):
        veri = {}
    if r.status_code != 200 or not veri.get("verified"):
        kod = veri.get("code") or f"verify_{r.status_code}"
        govde = re.sub(r"\s+", " ", r.text)[:150]
        raise TurnstileHata(f"EKAP verify reddetti: HTTP {r.status_code} {govde}", kod=str(kod))
    cerez = "; ".join(f"{c.name}={c.value}" for c in s.cookies.jar)
    if "ekap.human-verification" not in cerez:
        raise TurnstileHata("verify 200 ama ekap.human-verification çerezi gelmedi", kod="cerez_yok")
    return cerez, veri


def coz(sebep: str = "") -> dict:
    """Bir tam tur: anahtar → token → verify → çerezi kaydet. Durum + olay yazar."""
    t0 = time.time()
    try:
        sitekey, action = site_anahtari()
        token = token_al(sitekey, action, sure_sn=settings.EKAP_TURNSTILE_SURE_SN,
                         headless=settings.EKAP_TURNSTILE_HEADLESS)
        try:
            cerez, veri = dogrula(token)
        except TurnstileHata as e:
            # Anahtar dönmüş olabilir → bir sonraki tur taze keşifle başlasın.
            if "TOKEN" in str(e.kod).upper():
                cache.delete(_ANAHTAR)
            raise
        ekap_session.kaydet(cerez)
        basari_kaydet(veri.get("expiresAtUtc"), time.time() - t0, sebep)
        logger.info("EKAP Turnstile çözüldü (%s) — bitiş %s, %.1f sn",
                    sebep, veri.get("expiresAtUtc"), time.time() - t0)
        return {"ok": True, "bitis": veri.get("expiresAtUtc")}
    except Exception as e:                           # noqa: BLE001
        hata_kaydet(e, time.time() - t0, sebep)
        logger.error("EKAP Turnstile çözülemedi (%s): %s", sebep, e)
        return {"ok": False, "hata": str(e)}


def pano_ozeti() -> dict:
    """Panonun okuduğu özet — EKAP'a istek ATMAZ (yalnızca Redis + AppSetting)."""
    simdi = timezone.now()
    d = durum_oku()
    kalp = kalp_oku()
    canli = bool(kalp and (simdi - kalp).total_seconds() <= KALP_ZAMAN_ASIMI_SN)
    bitis = None
    try:
        bitis = _iso(d.get("bitis"))
    except ValueError:
        pass
    gecerli = bool(bitis and bitis > simdi)
    ardisik = d.get("ardisik_hata", 0)
    otomatik = otomatik_acik()
    if not canli:
        seviye, metin = "hata", "çözücü servisi çalışmıyor (nabız yok)"
    elif ardisik >= UYARI_ARDISIK:
        seviye, metin = "hata", f"son {ardisik} deneme başarısız"
    elif not otomatik:
        seviye, metin = "uyari", "otomatik çözücü KAPALI"
    elif ardisik:
        seviye, metin = "uyari", f"{ardisik} ardışık hata, yeniden deneniyor"
    else:
        seviye, metin = "ok", "otomatik çözücü çalışıyor"
    return {
        "seviye": seviye, "metin": metin, "canli": canli, "kalp": kalp, "otomatik": otomatik,
        "gecerli": gecerli, "bitis": bitis, "ardisik_hata": ardisik,
        "son_basari": _iso(d.get("son_basari")) if d.get("son_basari") else None,
        "son_hata": _iso(d.get("son_hata")) if d.get("son_hata") else None,
        "son_hata_metni": d.get("son_hata_metni", ""), "son_hata_kodu": d.get("son_hata_kodu", ""),
        "sonraki_deneme": _iso(d.get("sonraki_deneme")) if d.get("sonraki_deneme") else None,
        "bugun_basari": d.get("bugun_basari", 0) if d.get("gun") == _gun_anahtari(simdi) else 0,
        "bugun_hata": d.get("bugun_hata", 0) if d.get("gun") == _gun_anahtari(simdi) else 0,
        "son_sure_sn": d.get("son_sure_sn"),
    }
