"""
Pro paketlerinin mağaza fiyatları (web portalı sihirbazı için) — `GET /subscription/fiyatlar/`.

Fiyatın tek doğru kaynağı mağazalardır: mobil SDK fiyatı cihazdaki mağazadan okur, RC REST
API'sinde Türkiye fiyatı yoktur (yalnızca USD `indicative_price`). Bu yüzden:

1. **RevenueCat** → hangi ürünler satılıyor: current (default) offering'in paketleri ve her
   paketin App Store / Play ürün kimliği. Web'in gösterdiği paketler mobilin ödeme ekranıyla
   böylece hep aynı kalır.
2. **App Store Connect API** → Türkiye (TUR) müşteri fiyatı + ücretsiz deneme.
3. **Google Play Developer API** → Türkiye (TR) bölge fiyatı + ücretsiz deneme.

Sonuç `core.AppSetting("magaza_fiyatlari")`a JSON olarak yazılır; uç yalnızca bunu okur,
mağazaya istek anında GİTMEZ. Günlük görev + `manage.py magaza_fiyatlarini_guncelle`.

Hata sınıfları (kabul kriterleri bunlara dayanır):
- Mağaza **yapılandırılmamış** (kimlik yok) → o mağaza `null`, hata değil.
- Ürün mağazada **yok** / TR fiyatı yok / RC'de o mağazanın ürünü yok → o hücre `null`.
- Ağ / yetki / 5xx → `MagazaFiyatHatasi`: tur başarısız, **önceki kayıt korunur**.
- ⚠️ Fiyat ASLA tahmin edilmez, `0` yazılmaz. Hiçbir fiyat okunamadıysa kayıt yazılmaz.
"""
from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

KAYIT_ANAHTARI = "magaza_fiyatlari"
DURUM_ANAHTARI = "magaza_fiyatlari_durum"
ULKE = "TR"
_ASC_BOLGE = "TUR"
_ASC_TABAN = "https://api.appstoreconnect.apple.com"
_PLAY_TABAN = "https://androidpublisher.googleapis.com/androidpublisher/v3"
_TIMEOUT = 20

# RC paket anahtarı → süre (ürünün kendi süresi okunamazsa yedek).
_PAKET_SURE = {
    "$rc_weekly": "P1W", "$rc_monthly": "P1M", "$rc_two_month": "P2M",
    "$rc_three_month": "P3M", "$rc_six_month": "P6M", "$rc_annual": "P1Y",
}
_SURE_KOD = {
    "P1W": "haftalik", "P1M": "aylik", "P2M": "iki_aylik", "P3M": "uc_aylik",
    "P6M": "alti_aylik", "P1Y": "yillik",
}
_SURE_GUN = {"D": 1, "W": 7, "M": 30, "Y": 365}
# ASC deneme süresi sabitleri → ISO 8601.
_ASC_SURE = {
    "THREE_DAYS": "P3D", "ONE_WEEK": "P1W", "TWO_WEEKS": "P2W", "ONE_MONTH": "P1M",
    "TWO_MONTHS": "P2M", "THREE_MONTHS": "P3M", "SIX_MONTHS": "P6M", "ONE_YEAR": "P1Y",
}


class MagazaFiyatHatasi(Exception):
    """Mağaza / RC isteği başarısız — tur iptal, önceki kayıt korunur."""


class Yapilandirilmamis(Exception):
    """Mağaza kimlikleri tanımlı değil — o mağaza `null`."""


# ── yardımcılar ─────────────────────────────────────────

def fiyat_metni(deger) -> str | None:
    """Ondalık metin ("249.99"). Kuruşsuz/kayıpsız 2 haneye iner, inemiyorsa olduğu gibi."""
    try:
        d = Decimal(str(deger))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    q = d.quantize(Decimal("0.01"))
    return str(q) if q == d else str(d.normalize())


def _sure_carp(sure: str | None, kat) -> str | None:
    m = re.fullmatch(r"P(\d+)([DWMY])", sure or "")
    if not m:
        return sure or None
    try:
        kat = int(kat or 1)
    except (TypeError, ValueError):
        kat = 1
    return f"P{int(m.group(1)) * max(kat, 1)}{m.group(2)}"


def _sure_gun(sure: str | None) -> int:
    m = re.fullmatch(r"P(\d+)([DWMY])", sure or "")
    return int(m.group(1)) * _SURE_GUN[m.group(2)] if m else 10**6


def _bugun_iso() -> str:
    return timezone.localdate().isoformat()


def _ozel_anahtar(deger: str) -> str:
    """Env'de PEM içeriği (`\\n` kaçışlı olabilir) ya da dosya yolu."""
    deger = (deger or "").strip()
    if "BEGIN" in deger:
        return deger.replace("\\n", "\n")
    with open(deger, encoding="utf-8") as f:
        return f.read()


def _json_kimlik(deger: str) -> dict:
    """Env'de servis hesabı JSON içeriği ya da dosya yolu."""
    deger = (deger or "").strip()
    if deger.startswith("{"):
        return json.loads(deger)
    with open(deger, encoding="utf-8") as f:
        return json.load(f)


def _get_json(url: str, headers: dict, params=None, *, kaynak: str):
    """GET → JSON. 404 → None (kaynak yok), diğer 4xx/5xx/ağ → `MagazaFiyatHatasi`."""
    import requests

    try:
        r = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise MagazaFiyatHatasi(f"{kaynak}: istek başarısız: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise MagazaFiyatHatasi(f"{kaynak}: HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError as e:
        raise MagazaFiyatHatasi(f"{kaynak}: yanıt JSON değil") from e


# ── RevenueCat: satılan paketler ────────────────────────

def _liste(deger) -> list:
    """RC v2 liste nesnesi (`{"items": [...]}`) ya da düz liste."""
    if isinstance(deger, dict):
        return deger.get("items") or []
    return deger if isinstance(deger, list) else []


def rc_paketleri_ayristir(offerings: dict, apps: dict) -> list[dict]:
    """Current offering → `[{rc_paket, sure, sira, urunler: {app_store|play_store: id}}]`.

    ⚠️ Current offering yoksa hata: mobilin ödeme ekranında ne gösterildiği bilinmiyor.
    """
    app_turu = {a.get("id"): a.get("type") for a in _liste(apps) if a.get("id")}
    offering = next((o for o in _liste(offerings) if o.get("is_current")), None)
    if offering is None:
        raise MagazaFiyatHatasi("RevenueCat: current offering yok")

    paketler = []
    for i, p in enumerate(_liste(offering.get("packages"))):
        anahtar = p.get("lookup_key") or ""
        urunler, sure = {}, None
        for oge in _liste(p.get("products")):
            urun = oge.get("product") if isinstance(oge.get("product"), dict) else oge
            tur = (urun.get("app") or {}).get("type") or app_turu.get(urun.get("app_id"))
            kimlik = urun.get("store_identifier")
            if tur in ("app_store", "play_store") and kimlik and tur not in urunler:
                urunler[tur] = kimlik
            sure = sure or (urun.get("subscription") or {}).get("duration")
        sure = sure or _PAKET_SURE.get(anahtar)
        paketler.append({
            "rc_paket": anahtar, "sure": sure, "urunler": urunler,
            "sira": p.get("position") if isinstance(p.get("position"), int) else i,
        })
    paketler.sort(key=lambda p: p["sira"])
    return paketler


def rc_paketleri() -> list[dict]:
    from .revenuecat import RevenueCatError, _request

    try:
        offerings = _request("offerings?expand=items.package.product&limit=20")
        apps = _request("apps?limit=50")
    except RevenueCatError as e:
        raise MagazaFiyatHatasi(f"RevenueCat: {e}") from e
    return rc_paketleri_ayristir(offerings or {}, apps or {})


# ── App Store Connect ───────────────────────────────────

def asc_fiyat_sec(yanit: dict, bugun: str) -> dict | None:
    """`subscriptions/{id}/prices` yanıtı → `{fiyat, para_birimi}` ya da None.

    Yürürlükteki fiyat: başlangıcı boş ya da bugün/geçmiş olan EN YENİ kayıt (ileri tarihli
    fiyat değişikliği de listede olabilir). `preserved=True` mevcut aboneler için korunan eski
    fiyattır, yeni müşterinin ödeyeceği değil → atlanır.
    """
    dahil = {(x.get("type"), x.get("id")): x.get("attributes") or {}
             for x in (yanit or {}).get("included") or []}
    adaylar = []
    for kayit in (yanit or {}).get("data") or []:
        a = kayit.get("attributes") or {}
        if a.get("preserved"):
            continue
        baslangic = a.get("startDate") or ""
        if baslangic and baslangic > bugun:
            continue
        iliski = kayit.get("relationships") or {}
        nokta = (iliski.get("subscriptionPricePoint") or {}).get("data") or {}
        bolge = (iliski.get("territory") or {}).get("data") or {}
        fiyat = fiyat_metni(dahil.get(("subscriptionPricePoints", nokta.get("id")), {})
                            .get("customerPrice"))
        para = dahil.get(("territories", bolge.get("id")), {}).get("currency")
        if bolge.get("id") not in (None, _ASC_BOLGE):
            continue
        if fiyat:
            adaylar.append((baslangic, fiyat, para or "TRY"))
    if not adaylar:
        return None
    _, fiyat, para = max(adaylar, key=lambda x: x[0])
    return {"fiyat": fiyat, "para_birimi": para}


def asc_deneme_sec(yanit: dict, bugun: str) -> str | None:
    """`introductoryOffers` yanıtı → yürürlükteki ücretsiz denemenin süresi (ISO) ya da None."""
    for kayit in (yanit or {}).get("data") or []:
        a = kayit.get("attributes") or {}
        if a.get("offerMode") != "FREE_TRIAL":
            continue
        if (a.get("startDate") or "") > bugun or (a.get("endDate") and a["endDate"] < bugun):
            continue
        sure = _ASC_SURE.get(a.get("duration"))
        if sure:
            return _sure_carp(sure, a.get("numberOfPeriods"))
    return None


class AppStoreIstemcisi:
    def __init__(self):
        if not (settings.ASC_ISSUER_ID and settings.ASC_KEY_ID and settings.ASC_PRIVATE_KEY):
            raise Yapilandirilmamis("ASC_ISSUER_ID / ASC_KEY_ID / ASC_PRIVATE_KEY")
        import jwt

        simdi = int(time.time())
        try:
            anahtar = _ozel_anahtar(settings.ASC_PRIVATE_KEY)
            token = jwt.encode(
                {"iss": settings.ASC_ISSUER_ID, "iat": simdi, "exp": simdi + 600,
                 "aud": "appstoreconnect-v1"},
                anahtar, algorithm="ES256", headers={"kid": settings.ASC_KEY_ID, "typ": "JWT"},
            )
        except Exception as e:  # noqa: BLE001 — bozuk anahtar tur hatasıdır, sessiz null değil
            raise MagazaFiyatHatasi(f"App Store: anahtar okunamadı: {e}") from e
        self.basliklar = {"Authorization": f"Bearer {token}"}
        self._urun_kimlikleri = None

    def _get(self, yol, params=None):
        return _get_json(f"{_ASC_TABAN}{yol}", self.basliklar, params, kaynak="App Store")

    def urun_kimlikleri(self) -> dict:
        """productId → ASC abonelik id'si (uygulamanın tüm abonelik gruplarından)."""
        if self._urun_kimlikleri is None:
            yanit = self._get(f"/v1/apps/{settings.ASC_APP_ID}/subscriptionGroups",
                              {"include": "subscriptions", "limit": 50,
                               "limit[subscriptions]": 50}) or {}
            self._urun_kimlikleri = {
                (x.get("attributes") or {}).get("productId"): x.get("id")
                for x in yanit.get("included") or [] if x.get("type") == "subscriptions"
            }
        return self._urun_kimlikleri

    def fiyat(self, urun: str) -> dict | None:
        asc_id = self.urun_kimlikleri().get(urun)
        if not asc_id:
            logger.warning("mağaza fiyatı: App Store'da ürün yok: %s", urun)
            return None
        bugun = _bugun_iso()
        fiyat = asc_fiyat_sec(self._get(
            f"/v1/subscriptions/{asc_id}/prices",
            {"filter[territory]": _ASC_BOLGE, "include": "subscriptionPricePoint,territory",
             "limit": 200}), bugun)
        if fiyat is None:
            logger.warning("mağaza fiyatı: App Store'da TR fiyatı yok: %s", urun)
            return None
        fiyat["deneme"] = asc_deneme_sec(self._get(
            f"/v1/subscriptions/{asc_id}/introductoryOffers",
            {"filter[territory]": _ASC_BOLGE, "limit": 50}), bugun)
        return fiyat


# ── Google Play ─────────────────────────────────────────

def play_fiyat_sec(abonelik: dict, temel_plan: str) -> dict | None:
    """Play `Subscription` → TR bölge fiyatı (yalnız ACTIVE temel plan, yeni aboneye açık)."""
    for plan in (abonelik or {}).get("basePlans") or []:
        if plan.get("basePlanId") != temel_plan or plan.get("state") != "ACTIVE":
            continue
        for bolge in plan.get("regionalConfigs") or []:
            if bolge.get("regionCode") != ULKE or bolge.get("newSubscriberAvailability") is False:
                continue
            p = bolge.get("price") or {}
            try:
                tutar = Decimal(str(p.get("units") or 0)) + Decimal(int(p.get("nanos") or 0)) / 10**9
            except (InvalidOperation, ValueError, TypeError):
                return None
            fiyat = fiyat_metni(tutar)
            if fiyat and p.get("currencyCode"):
                return {"fiyat": fiyat, "para_birimi": p["currencyCode"]}
    return None


def play_deneme_sec(teklifler: dict) -> str | None:
    """Temel planın teklifleri → TR'de açık, ilk fazı ücretsiz ACTIVE teklifin süresi."""
    for teklif in (teklifler or {}).get("subscriptionOffers") or []:
        if teklif.get("state") != "ACTIVE":
            continue
        tr = [b for b in teklif.get("regionalConfigs") or [] if b.get("regionCode") == ULKE]
        if not tr or tr[0].get("newSubscriberAvailability") is False:
            continue
        fazlar = teklif.get("phases") or []
        if not fazlar:
            continue
        faz = fazlar[0]
        if any(b.get("regionCode") == ULKE and "free" in b
               for b in faz.get("regionalConfigs") or []):
            return _sure_carp(faz.get("duration"), faz.get("recurrenceCount"))
    return None


class PlayIstemcisi:
    def __init__(self):
        if not settings.PLAY_SERVICE_ACCOUNT_JSON:
            raise Yapilandirilmamis("PLAY_SERVICE_ACCOUNT_JSON")
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            kimlik = service_account.Credentials.from_service_account_info(
                _json_kimlik(settings.PLAY_SERVICE_ACCOUNT_JSON),
                scopes=["https://www.googleapis.com/auth/androidpublisher"],
            )
            kimlik.refresh(Request())
        except Exception as e:  # noqa: BLE001
            raise MagazaFiyatHatasi(f"Google Play: kimlik doğrulanamadı: {e}") from e
        self.basliklar = {"Authorization": f"Bearer {kimlik.token}"}
        self.kok = f"{_PLAY_TABAN}/applications/{settings.PLAY_PACKAGE_NAME}"

    def _get(self, yol):
        return _get_json(f"{self.kok}{yol}", self.basliklar, kaynak="Google Play")

    def fiyat(self, urun: str) -> dict | None:
        # RC Play ürün kimliği "<subscriptionId>:<basePlanId>". Temel plan yoksa tahmin
        # edilmez — birden çok plan olabilir ve yanlışı seçmek yanlış fiyat göstermektir.
        if ":" not in urun:
            logger.warning("mağaza fiyatı: Play ürününde temel plan yok: %s", urun)
            return None
        abonelik_id, plan = urun.split(":", 1)
        fiyat = play_fiyat_sec(self._get(f"/subscriptions/{abonelik_id}"), plan)
        if fiyat is None:
            logger.warning("mağaza fiyatı: Play'de TR fiyatı yok: %s", urun)
            return None
        fiyat["deneme"] = play_deneme_sec(
            self._get(f"/subscriptions/{abonelik_id}/basePlans/{plan}/offers"))
        return fiyat


# ── toplama + kayıt ─────────────────────────────────────

_ISTEMCILER = {"app_store": AppStoreIstemcisi, "play_store": PlayIstemcisi}


def topla() -> dict:
    """Mağazalardan güncel fiyat kaydını kurar. Ağ/yetki hatasında `MagazaFiyatHatasi`."""
    paketler = rc_paketleri()
    istemciler = {}
    for magaza, sinif in _ISTEMCILER.items():
        try:
            istemciler[magaza] = sinif()
        except Yapilandirilmamis as e:
            logger.info("mağaza fiyatı: %s yapılandırılmamış (%s) → null", magaza, e)

    sonuc = []
    for p in paketler:
        magazalar = {}
        for magaza in _ISTEMCILER:
            urun = p["urunler"].get(magaza)
            istemci = istemciler.get(magaza)
            magazalar[magaza] = istemci.fiyat(urun) if (urun and istemci) else None
        sonuc.append({
            "kod": _SURE_KOD.get(p["sure"]) or p["rc_paket"].removeprefix("$rc_") or None,
            "rc_paket": p["rc_paket"],
            "sure": p["sure"],
            "magazalar": magazalar,
        })
    return {"ulke": ULKE, "guncellenme": timezone.localtime().isoformat(timespec="seconds"),
            "paketler": sonuc}


def fiyat_var(kayit: dict) -> bool:
    return any(m for p in kayit.get("paketler") or [] for m in p["magazalar"].values())


def _yaz(anahtar: str, deger: dict, aciklama: str) -> None:
    from core.models import AppSetting

    AppSetting.objects.update_or_create(
        key=anahtar,
        defaults={"value": json.dumps(deger, ensure_ascii=False), "description": aciklama},
    )


def oku(anahtar: str = KAYIT_ANAHTARI) -> dict | None:
    from core.models import AppSetting

    try:
        return json.loads(AppSetting.get(anahtar) or "null")
    except ValueError:
        return None


def guncelle() -> dict:
    """Görev/komut gövdesi. Başarıda kaydı yazar; hatada ÖNCEKİ kayıt olduğu gibi kalır."""
    deneme = timezone.localtime().isoformat(timespec="seconds")
    try:
        kayit = topla()
        if not fiyat_var(kayit):
            raise MagazaFiyatHatasi("hiçbir mağazadan fiyat okunamadı")
    except MagazaFiyatHatasi as e:
        logger.error("mağaza fiyatları güncellenemedi (önceki kayıt korunuyor): %s", e)
        _yaz(DURUM_ANAHTARI, {"son_deneme": deneme, "ok": False, "hata": str(e)[:500]},
             "Mağaza fiyatı görevinin son denemesi (otomatik).")
        return {"ok": False, "hata": str(e)}
    _yaz(KAYIT_ANAHTARI, kayit,
         "Pro paketlerinin mağaza fiyatları (otomatik — elle düzenlemeyin).")
    _yaz(DURUM_ANAHTARI, {"son_deneme": deneme, "ok": True, "hata": ""},
         "Mağaza fiyatı görevinin son denemesi (otomatik).")
    logger.info("mağaza fiyatları güncellendi: %s paket", len(kayit["paketler"]))
    return {"ok": True, "paket": len(kayit["paketler"])}
