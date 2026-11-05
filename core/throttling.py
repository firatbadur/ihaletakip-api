"""
Hız sınırı + sayfa derinliği — veri madenciliğine (scraping) karşı backend koruması.

Uçlar `throttle_kapsam = "<kapsam>"` özniteliğiyle katılır; özniteliği olmayan uç hiç
sayılmaz. Sınıf `DEFAULT_THROTTLE_CLASSES`'ta global durur.

**Kimlik** (`istemci()`):
- Girişli istek → `u:<kullanıcı id>` (mobil + web aynı kova).
- Belge proxy'sinde geçerli `imza` → `u:<imzadaki id>` (mobil indirmesi başlıksız).
- Web portalı (`X-IT-Portal-Key` doğru) → `ip:<X-IT-Client-IP>`.
- Web portalının **önbellek dolumu** (anahtar doğru + `X-IT-Cache-Fill: 1`) →
  `portal:dolum` — tek, AYRI kova (bkz. `istemci()`).
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
# İKN araması (`ihale_arama` ucu + `ikn_yil` & `ikn_sayi`): kişi kovası aramayla aynı;
# ayrı kapsam olmasının tek sebebi dolum bütçesinin ayrılması (web 8. not #27).
VARSAYILAN_ORANLAR["ikn_arama"] = VARSAYILAN_ORANLAR["ihale_arama"]
# Portal önbellek dolumu kovası (`dolum`): TÜM web anonim kullanıcılarının paylaştığı
# önbellek ıskaları buraya düşer → tek kişinin değil, sitenin bütçesidir.
# ⚠️ Belge dolum olamaz (kişiye özel); yanlışlıkla gelirse anonim belge sınırı uygulanır.
VARSAYILAN_DOLUM_ORANLARI = {
    "ihale_arama": [(60, 200)],
    "firma_liste": [(60, 200)],
    "detay": [(60, 300)],
    "belge": [(60, 20)],
    # ⚠️ Yalnızca DB emniyet tavanı. Asıl İKN bütçesi SONUCA göredir
    # (`ikn_dolum_sonucu`): boş İKN araması ile var olan İKN'yi açan istek ayrı sayılır.
    "ikn_arama": [(60, 1500)],
}
# Dolum İKN araması, sorgudan SONRA sonuca göre (dakikada): İKN sayan bir betik
# çoğunlukla olmayan İKN'leri dener; gerçek kullanıcı ve arama motoru neredeyse hep var
# olanı açar → yalnız boş aramalar yavaşlar. `API_THROTTLE_DOLUM_IKN` ile ezilir.
VARSAYILAN_DOLUM_IKN = {"bulunan": 300, "bos": 60}
# Panelin dakikalık dolum grafiği için kapsam → tür (İKN türleri görünümde yazılır).
_DOLUM_TURU = {"ihale_arama": "liste", "firma_liste": "firma_liste", "detay": "detay"}
DOLUM_KIMLIGI = "portal:dolum"

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
    dolum: bool = False
    # Gerçek istemci IP'si / tarayıcı kimliği (izleme + IP engeli). Girişli istekte de
    # dolar; dolumda boştur (kullanıcısız istek kimin için yapıldığını taşımaz).
    ip: str = ""
    ua: str = ""
    uid: int | None = None

    @property
    def tur(self) -> str:
        if self.dolum:
            return "portal-dolum"
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


def istemci_ip(request, portal: bool | None = None) -> str:
    """Gerçek istemci IP'si: portalda `X-IT-Client-IP`, aksi hâlde `X-Real-IP`."""
    if portal is None:
        portal = portal_dogrulandi(request)
    ip = _gecerli_ip(request.META.get("HTTP_X_IT_CLIENT_IP")) if portal else None
    return (ip or _gecerli_ip(request.META.get("HTTP_X_REAL_IP"))
            or _gecerli_ip(request.META.get("REMOTE_ADDR")) or "")


def _istemci_ua(request, portal: bool) -> str:
    # ⚠️ Portalda User-Agent web konteynerinindir ("node") → gerçek tarayıcı
    # `X-IT-Client-UA` ile gelir (web notu #25); yalnızca anahtar doğruysa kabul edilir.
    ua = request.META.get("HTTP_X_IT_CLIENT_UA", "") if portal else ""
    return (ua or request.META.get("HTTP_USER_AGENT", "") or "")[:300]


def istemci(request, *, imzali_uid: int | None = None) -> Istemci:
    portal = portal_dogrulandi(request)
    bot = portal and request.META.get("HTTP_X_IT_VERIFIED_BOT") == "1"
    kullanici = getattr(request, "user", None)
    girisli = kullanici is not None and kullanici.is_authenticated
    # ⚠️⚠️ GİRİŞLİ KULLANICI DOLUMDAN ÖNCE GELİR (2026-11-05). Web token taşıyan
    # isteklere de `X-IT-Cache-Fill: 1` koyuyordu (`documents/`, benchmark, profile);
    # dolum önce değerlendirildiği için bu istekler kullanıcının değil sitenin ORTAK
    # kovasına düşüyor, tek bir kazıyıcı bütün web kullanıcılarını 429'a sokuyordu
    # (o gece 1.584 adet) ve kişi başı EKAP kotası kimliği göremiyordu.
    # Önbellek dolumu (portal 2. not #8): web'in paylaşılan önbelleğini dolduran
    # KULLANICISIZ istek `X-IT-Client-IP` taşıyamaz → ayrı kova. IP'ye düşseydi web
    # konteynerinin TEK IP'si olurdu. Anahtar yoksa başlık YOK SAYILIR (taklit edilemez).
    if not girisli and portal and request.META.get("HTTP_X_IT_CACHE_FILL") == "1":
        return Istemci(DOLUM_KIMLIGI, True, bot, False, dolum=True)
    ip = istemci_ip(request, portal)
    ua = _istemci_ua(request, portal)
    if girisli:
        return Istemci(f"u:{kullanici.pk}", portal, bot, True, ip=ip, ua=ua, uid=kullanici.pk)
    if imzali_uid:
        return Istemci(f"u:{imzali_uid}", portal, bot, True, ip=ip, ua=ua, uid=imzali_uid)
    return Istemci(f"ip:{ip or 'bilinmiyor'}", portal, bot, False, ip=ip, ua=ua)


def mod(ist: Istemci) -> str:
    deger = (getattr(settings, "API_THROTTLE_PORTAL_MODU", "uygula") if ist.portal
             else getattr(settings, "API_THROTTLE_MODU", "gozlem"))
    return deger if deger in MODLAR else "gozlem"


def oranlar(kapsam: str, ist: Istemci):
    tablo = dict(VARSAYILAN_ORANLAR)
    tablo.update(getattr(settings, "API_THROTTLE_ORANLAR", None) or {})
    kayit = tablo.get(kapsam) or {}
    if ist.dolum:
        dolum = dict(VARSAYILAN_DOLUM_ORANLARI)
        dolum.update(getattr(settings, "API_THROTTLE_DOLUM_ORANLARI", None) or {})
        if kapsam in dolum:
            return [tuple(x) for x in dolum[kapsam]]
    return [tuple(x) for x in kayit.get("girisli" if ist.girisli else "anonim", [])]


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
    for olay in ("hiz", "derinlik", "belge_anonim", "sayfa_pro", "dogrulama"):
        for kapsam in (*VARSAYILAN_ORANLAR, "liste", "ikn_bulunan", "ikn_bos"):
            for tur in ("dogrudan-anonim", "dogrudan-girisli", "portal-anonim",
                        "portal-girisli", "portal-dolum"):
                n = cache.get(f"thr:gz:{gun}:{olay}:{kapsam}:{tur}")
                if n:
                    sonuc[(olay, kapsam, tur)] = (
                        n, cache.get(f"thr:gzt:{gun}:{olay}:{kapsam}:{tur}") or 0)
    return sonuc


def kapsam_bul(view, request) -> str | None:
    """View `throttle_kapsam_coz(request)` tanımlıyorsa istek başına kapsam, yoksa sabit."""
    coz = getattr(view, "throttle_kapsam_coz", None)
    if callable(coz):
        return coz(request)
    return getattr(view, "throttle_kapsam", None)


def ikn_dolum_sonucu(request, bulundu: bool) -> None:
    """
    Dolum İKN aramasının SONUCA göre bütçesi (web 8. not #27). Sorgudan sonra çağrılır;
    sınır aşıldıysa `Throttled` (429 + Retry-After) fırlatır.

    ⚠️ Sorgu bu noktada zaten koşmuştur: İKN araması indeksli ve ucuz, DB'yi korumak
    `VARSAYILAN_DOLUM_ORANLARI["ikn_arama"]` tavanının işi. Burada korunan şey bütçe
    paylaşımıdır — boş aramalarla dolan sayaç var olan İKN'yi açan kullanıcıyı kesmez.
    ⚠️ Koruma hatası API'yi kırmaz (throttle ile aynı kural).
    """
    from rest_framework.exceptions import Throttled

    try:
        ist = istemci(request)
        if not ist.dolum or ist.bot:
            return
        m = mod(ist)
        if m == "kapali":
            return
        tur = "bulunan" if bulundu else "bos"
        from .guvenlik_izleme import dolum_say

        dolum_say(f"ikn_{tur}")
        tablo = dict(VARSAYILAN_DOLUM_IKN)
        tablo.update(getattr(settings, "API_THROTTLE_DOLUM_IKN", None) or {})
        simdi = time.time()
        n = _say(f"thr:dolum:ikn:{tur}:{int(simdi // 60)}", 65)
        if n <= int(tablo.get(tur, 0) or 0):
            return
        if m == "gozlem":
            gozlem_kaydet("hiz", f"ikn_{tur}", ist)
            return
        dolum_say("red")
        KapsamliThrottle._dolum_logla(request, f"ikn_{tur}", simdi)
        bekle = int(60 - simdi % 60) + 1
    except Exception as e:  # noqa: BLE001
        logger.warning("İKN dolum bütçesi hatası (istek geçirildi): %s", str(e)[:200])
        return
    raise Throttled(wait=bekle)


class KapsamliThrottle(BaseThrottle):
    """Kapsam bazlı, çok pencereli (dakika + saat/gün) sabit pencere sayacı."""

    def __init__(self):
        self._bekle = None

    def allow_request(self, request, view):
        from .insan_dogrulama import DogrulamaGerekli

        ist = None
        try:
            kapsam = kapsam_bul(view, request)
        except Exception:  # noqa: BLE001
            kapsam = getattr(view, "throttle_kapsam", None)
        try:
            imzali = getattr(view, "imzali_uid", None)
            ist = istemci(request, imzali_uid=imzali(request) if callable(imzali) else None)
            from .guvenlik_izleme import kaydet

            # İzleme TÜM API isteklerinde (yönetim paneli), kapsamsız uçlar dahil.
            kaydet(ist, request)
        except Exception as e:  # noqa: BLE001
            logger.debug("istek izleme hatası: %s", str(e)[:200])
        if not kapsam:
            return True
        try:
            izin = self._karar(request, view, kapsam, ist)
        except DogrulamaGerekli:
            # ⚠️ Yutulmamalı: aşağıdaki genel `except` 428'i "koruma hatası" sanıp isteği
            # geçirirdi ve insan doğrulaması hiç çalışmazdı.
            raise
        except Exception as e:  # noqa: BLE001 — koruma hatası API'yi kırmamalı
            logger.warning("throttle hatası (istek geçirildi): %s", str(e)[:200])
            return True
        if not izin:
            try:
                from .guvenlik_izleme import kaydet

                kaydet(ist, request, reddedildi=True)
            except Exception:  # noqa: BLE001
                pass
        return izin

    def _karar(self, request, view, kapsam, ist=None):
        if ist is None:
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

        # İnsan doğrulaması (yalnızca portal; kendi modu var — core/insan_dogrulama.py).
        from .insan_dogrulama import kontrol as dogrulama_kontrol

        dogrulama_kontrol(ist, kapsam, request)

        if ist.dolum and kapsam in _DOLUM_TURU:
            from .guvenlik_izleme import dolum_say

            dolum_say(_DOLUM_TURU[kapsam])

        bekle = None
        for pencere, sinir in oranlar(kapsam, ist):
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

        if ist.dolum:
            # ⚠️ Dolum kovasına kademeli engel UYGULANMAZ: kimlik bütün sitedir, 24 sa
            # engel web'in tüm önbellek ıskalarını bir gün kapatırdı. Sık dolması bir
            # tarayıcının önbellek dışı anahtar gezdiği demek → uç + sorgu loglanır.
            self._dolum_logla(request, kapsam, simdi)
            from .guvenlik_izleme import dolum_say

            dolum_say("red")
            self._bekle = bekle
            return False
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
                from .guvenlik import olay_yaz

                olay_yaz("hiz_engeli", ist=ist, ayrinti={"asim": sayi, "sure_sn": sure},
                         her_zaman=True)
                return sure
        return None

    @staticmethod
    def _dolum_logla(request, kapsam, simdi):
        # Dakikada en çok 20 satır: tarayıcı saniyede onlarca istek atarken log da
        # bir saldırı yüzeyi olmasın. ⚠️ Sır loglanmaz (yalnızca yol + sorgu).
        if _say(f"thr:dolumlog:{int(simdi // 60)}", 120) > 20:
            return
        logger.warning("throttle portal-dolum kovası doldu: kapsam=%s yol=%s sorgu=%s",
                       kapsam, request.path, request.META.get("QUERY_STRING", "")[:300])

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
        # ⚠️ Dolum isteği kimin için yapıldığını bilmez: girişli bir kullanıcının açtığı
        # 30. sayfa da paylaşılan önbelleğe dolum olarak gelebilir. Web anonim sınırı
        # (10 sayfa) kendisi uyguluyor → burada geniş (girişli) sınır.
        sinir = (getattr(settings, "API_SAYFA_DERINLIK_GIRISLI", 1000)
                 if ist.girisli or ist.dolum
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
