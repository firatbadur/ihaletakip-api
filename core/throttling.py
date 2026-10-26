"""
Hız sınırı + sayfa derinliği — veri madenciliğine (scraping) karşı backend koruması.

Uçlar `throttle_kapsam = "<kapsam>"` özniteliğiyle katılır; özniteliği olmayan uç hiç
sayılmaz. Sınıf `DEFAULT_THROTTLE_CLASSES`'ta global durur.

**Kimlik** (`istemci()`):
- Girişli istek → `u:<kullanıcı id>` (mobil + web aynı kova).
- Belge proxy'sinde geçerli `imza` → `u:<imzadaki id>` (mobil indirmesi başlıksız).
- Web portalı (`X-IT-Portal-Key` doğru) → `ip:<X-IT-Client-IP>`.
- Diğer → `ip:<X-Real-IP>` (nginx Cloudflare'in gerçek IP'sini buraya yazar).

⚠️⚠️ **Portal tek IP'den gelir** (web konteyneri). Anahtar doğrulanmadan
`X-IT-Client-IP` kabul edilseydi herkes kendine sahte IP yazıp sınırı atlardı;
doğrulanmadan YOK SAYILIRSA da portalın bütün kullanıcıları tek kovaya düşerdi.

**Mod** (`kapali` · `gozlem` · `uygula`), portal ve diğerleri için AYRI:
- `API_THROTTLE_PORTAL_MODU` (vars. `uygula`) — portal yeni, mevcut kullanıcısı yok.
- `API_THROTTLE_MODU` (vars. `gozlem`) — mobil uygulama + doğrudan API.
⚠️⚠️ Mobil için `gozlem` BİLİNÇLİ: mobilde 429 için özel bir dal yok (genel hata ya
da yanıltıcı "Yüklenici bulunamadı" ekranı çıkıyor) ve anonim mobil kullanıcılar
operatör NAT'ı (CGNAT) arkasında aynı IP'yi paylaşabiliyor. Gözlem modu hiçbir isteği
reddetmez; yalnızca "uygula olsaydı reddedilirdi" sayaçlarını yazar
(`manage.py throttle_rapor`). Eşikler o ölçümle ayarlanıp sonra `uygula`ya geçilir.

⚠️ **Bu modül asla isteği düşürmez**: Redis/kod hatası olursa istek GEÇER ve loglanır.
Koruma uğruna API'yi kırmak, korunacak bir şey bırakmazdı.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.throttling import BaseThrottle

logger = logging.getLogger("ihaletakip")

MESAJ_429 = "Çok fazla istek gönderildi. Lütfen biraz bekleyip tekrar deneyin."

MODLAR = ("kapali", "gozlem", "uygula")

# (pencere saniye, istek sınırı). Başlangıç değerleri — gözlem ölçümüyle ayarlanır;
# `API_THROTTLE_ORANLAR` (env JSON) kapsam bazında ezebilir.
VARSAYILAN_ORANLAR = {
    "ihale_arama": {"anonim": [(60, 20), (3600, 300)], "girisli": [(60, 60), (3600, 1200)]},
    "detay": {"anonim": [(60, 30), (3600, 400)], "girisli": [(60, 90), (3600, 1500)]},
    "firma_liste": {"anonim": [(60, 20), (3600, 300)], "girisli": [(60, 60), (3600, 1200)]},
    # Belge uçları canlı EKAP'a gider ve sınırlı bir günlük bütçeyi harcar.
    "belge": {"anonim": [(60, 20), (86400, 200)], "girisli": [(60, 20), (86400, 200)]},
}

# Kademeli geçici engel: farklı dakikalarda tekrarlanan aşım sayısı → engel süresi.
_CEZA_KADEMELERI = ((10, 24 * 3600), (6, 3600), (3, 15 * 60))
_CEZA_HAFIZA_SN = 24 * 3600
_GOZLEM_TTL = 8 * 24 * 3600


@dataclass(frozen=True)
class Istemci:
    kimlik: str
    portal: bool
    bot: bool
    girisli: bool

    @property
    def tur(self) -> str:
        return f"{'portal' if self.portal else 'dogrudan'}-{'girisli' if self.girisli else 'anonim'}"


def portal_dogrulandi(request) -> bool:
    beklenen = getattr(settings, "PORTAL_KEY", "") or ""
    gelen = request.META.get("HTTP_X_IT_PORTAL_KEY", "") or ""
    # ⚠️ Sabit zamanlı karşılaştırma; anahtar tanımsızsa portal yolu tümüyle kapalı.
    return bool(beklenen) and hmac.compare_digest(gelen.encode(), beklenen.encode())


def _gecerli_ip(deger: str) -> str | None:
    deger = (deger or "").split(",")[0].strip()
    try:
        return str(ipaddress.ip_address(deger))
    except ValueError:
        return None


def istemci(request, *, imzali_uid: int | None = None) -> Istemci:
    portal = portal_dogrulandi(request)
    bot = portal and request.META.get("HTTP_X_IT_VERIFIED_BOT") == "1"
    kullanici = getattr(request, "user", None)
    if kullanici is not None and kullanici.is_authenticated:
        return Istemci(f"u:{kullanici.pk}", portal, bot, True)
    if imzali_uid:
        return Istemci(f"u:{imzali_uid}", portal, bot, True)
    ip = None
    if portal:
        ip = _gecerli_ip(request.META.get("HTTP_X_IT_CLIENT_IP"))
    ip = ip or _gecerli_ip(request.META.get("HTTP_X_REAL_IP")) or _gecerli_ip(
        request.META.get("REMOTE_ADDR")) or "bilinmiyor"
    return Istemci(f"ip:{ip}", portal, bot, False)


def mod(ist: Istemci) -> str:
    deger = (getattr(settings, "API_THROTTLE_PORTAL_MODU", "uygula") if ist.portal
             else getattr(settings, "API_THROTTLE_MODU", "gozlem"))
    return deger if deger in MODLAR else "gozlem"


def oranlar(kapsam: str, girisli: bool):
    tablo = dict(VARSAYILAN_ORANLAR)
    tablo.update(getattr(settings, "API_THROTTLE_ORANLAR", None) or {})
    kayit = tablo.get(kapsam) or {}
    return [tuple(x) for x in kayit.get("girisli" if girisli else "anonim", [])]


def _say(anahtar: str, ttl: int) -> int:
    cache.add(anahtar, 0, ttl)
    try:
        return cache.incr(anahtar)
    except ValueError:  # add ile incr arasında düştü
        cache.set(anahtar, 1, ttl)
        return 1


def gozlem_kaydet(olay: str, kapsam: str, ist: Istemci) -> None:
    """"Uygula olsaydı reddedilirdi" sayacı (+ tekil kimlik sayacı)."""
    gun = timezone.localdate().isoformat()
    _say(f"thr:gz:{gun}:{olay}:{kapsam}:{ist.tur}", _GOZLEM_TTL)
    if cache.add(f"thr:gzk:{gun}:{olay}:{kapsam}:{ist.kimlik}", 1, 2 * 24 * 3600):
        _say(f"thr:gzt:{gun}:{olay}:{kapsam}:{ist.tur}", _GOZLEM_TTL)
        logger.info("throttle gözlem: olay=%s kapsam=%s tür=%s kimlik=%s",
                    olay, kapsam, ist.tur, ist.kimlik)


def gozlem_ozeti(gun: str) -> dict:
    """`throttle_rapor` komutu için: {(olay, kapsam, tür): (istek, tekil)}."""
    sonuc = {}
    for olay in ("hiz", "derinlik", "belge_anonim"):
        for kapsam in (*VARSAYILAN_ORANLAR, "liste"):
            for tur in ("dogrudan-anonim", "dogrudan-girisli", "portal-anonim",
                        "portal-girisli"):
                n = cache.get(f"thr:gz:{gun}:{olay}:{kapsam}:{tur}")
                if n:
                    sonuc[(olay, kapsam, tur)] = (
                        n, cache.get(f"thr:gzt:{gun}:{olay}:{kapsam}:{tur}") or 0)
    return sonuc


class KapsamliThrottle(BaseThrottle):
    """Kapsam bazlı, çok pencereli (dakika + saat/gün) sabit pencere sayacı."""

    def __init__(self):
        self._bekle = None

    def allow_request(self, request, view):
        kapsam = getattr(view, "throttle_kapsam", None)
        if not kapsam:
            return True
        try:
            return self._karar(request, view, kapsam)
        except Exception as e:  # noqa: BLE001 — koruma hatası API'yi kırmamalı
            logger.warning("throttle hatası (istek geçirildi): %s", str(e)[:200])
            return True

    def _karar(self, request, view, kapsam):
        imzali = getattr(view, "imzali_uid", None)
        ist = istemci(request, imzali_uid=imzali(request) if callable(imzali) else None)
        if ist.bot:
            # ⚠️ Doğrulanmış arama botu sınırlanmaz: 429 Google'ın tarama bütçesini
            # düşürür. Bota farklı İÇERİK verilmez, yalnızca sınır farklıdır.
            return True
        m = mod(ist)
        if m == "kapali":
            return True

        simdi = time.time()
        if m == "uygula":
            bitis = cache.get(f"thr:ban:{ist.kimlik}")
            if bitis and bitis > simdi:
                self._bekle = int(bitis - simdi) + 1
                return False

        bekle = None
        for pencere, sinir in oranlar(kapsam, ist.girisli):
            dilim = int(simdi // pencere)
            n = _say(f"thr:{kapsam}:{ist.kimlik}:{pencere}:{dilim}", int(pencere) + 5)
            if n > sinir:
                kalan = int(pencere - simdi % pencere) + 1
                bekle = max(bekle or 0, kalan)
        if bekle is None:
            return True

        if m == "gozlem":
            gozlem_kaydet("hiz", kapsam, ist)
            return True

        self._bekle = max(bekle, self._ceza(ist, simdi) or 0)
        return False

    @staticmethod
    def _ceza(ist: Istemci, simdi: float) -> int | None:
        """Farklı dakikalarda tekrarlanan aşım → kademeli geçici engel (15 dk/1 sa/24 sa)."""
        if not cache.add(f"thr:asimdk:{ist.kimlik}:{int(simdi // 60)}", 1, 120):
            return None  # bu dakika zaten sayıldı
        sayi = _say(f"thr:ceza:{ist.kimlik}", _CEZA_HAFIZA_SN)
        for esik, sure in _CEZA_KADEMELERI:
            if sayi >= esik:
                cache.set(f"thr:ban:{ist.kimlik}", simdi + sure, sure)
                logger.warning("throttle engeli: kimlik=%s aşım=%s süre=%ss",
                               ist.kimlik, sayi, sure)
                return sure
        return None

    def wait(self):
        return self._bekle


def sayfa_derinligi_asimi(request, page: int, page_size: int, kapsam: str = "liste"):
    """Sayfa derinliği sınırı. Reddedilecekse Türkçe mesaj, değilse `None` döner.

    Sınır **kayıt ofsetidir** (`(page-1) * page_size`): anonim 200, girişli 1000 —
    web'in 20'lik sayfasıyla 10 / 50 sayfa. Mobil 10'luk sayfa kullanıyor, aynı ofset
    orada 20 / 100 sayfa eder.
    ⚠️⚠️ Mod kuralı hız sınırıyla aynı: mobil/doğrudan trafikte vars. `gozlem`. Mobil
    liste sonsuz kaydırmada sınır tanımıyor ve bu uç hata dönerse **zaten yüklenmiş
    sonuçlar dahil** tüm liste hata ekranına düşüyor (ölçüldü: `TenderList/index.js`).
    """
    try:
        ist = istemci(request)
        if ist.bot:
            return None
        sinir = (getattr(settings, "API_SAYFA_DERINLIK_GIRISLI", 1000) if ist.girisli
                 else getattr(settings, "API_SAYFA_DERINLIK_ANONIM", 200))
        if (max(1, page) - 1) * max(1, page_size) < sinir:
            return None
        m = mod(ist)
        if m == "kapali":
            return None
        if m == "gozlem":
            gozlem_kaydet("derinlik", kapsam, ist)
            return None
    except Exception as e:  # noqa: BLE001
        logger.warning("sayfa derinliği kontrolü hatası (istek geçirildi): %s", str(e)[:200])
        return None
    if ist.girisli:
        return "Bu kadar derin sayfaya inilemiyor; lütfen aramanızı daraltın."
    return ("Bu kadar derin sayfaya inilemiyor; aramanızı daraltın ya da daha fazla "
            "sonuç için giriş yapın.")
