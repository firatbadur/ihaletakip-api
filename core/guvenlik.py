"""
Güvenlik katmanı — erişim engelleri (IP/CIDR, hesap, e-posta alanı) + olay günlüğü.

⚠️⚠️ **Neden var (2026-11-05).** Betikle açılmış tek kullanımlık bir hesap web portalında gece
~23 bin sayfa gezip ortak EKAP kullanıcı bütçesini bitirdi; elimizde ne kalıcı engel ne de
"bugün kim ne kadar istek attı" görünürlüğü vardı. Bu modül:
- `ErisimEngeli` üzerinden **IP kara listesi** (middleware), **hesap askıya alma**
  (`is_active=False`) ve **e-posta alanı yasağı** (tek kullanımlık adres servisleri),
- `GuvenlikOlayi` üzerinden **olay günlüğü** (yönetim paneli `/admin/guvenlik/`) sağlar.

Kurallar:
- ⚠️ Engel mantığı TEK yerdedir (admin eylemleri, panel düğmeleri, süre görevi hep buradaki
  fonksiyonları çağırır) — satırı elle silmek/düzenlemek `is_active`'i geri açmaz.
- ⚠️ Hesap engeli `is_active=False` yapar: JWT doğrulaması (`CHECK_USER_IS_ACTIVE`), token
  yenileme ve şifre girişi kendiliğinden kapanır; Google/Apple girişleri açıkça kontrol eder.
  `onceki_aktif` saklanır → kaldırınca yalnızca engelden önce aktif olan hesap açılır.
- ⚠️ IP engeli özel/loopback/Docker ağlarını ve çok geniş blokları REDDEDER: web portalı
  API'ye Docker ağ geçidinden (172.x) geliyor; o adresi engellemek bütün web'i kapatırdı.
- ⚠️ Bu modül API'yi asla düşürmez: Redis/DB hatasında engel kontrolü "engel yok" der.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

_SURUM_ANAHTARI = "guv:engel:surum"
_YEREL = {"surum": None, "zaman": 0.0, "aglar": [], "alanlar": frozenset()}
_YEREL_TTL = 30.0          # süreç içi önbellek (sn); değişiklik sürüm anahtarıyla duyurulur

# v4 /16'dan, v6 /32'den geniş blok engellenemez (yanlışlıkla bir operatörü kapatmamak için).
AZAMI_GENISLIK = {4: 16, 6: 32}

# Yerleşik tek kullanımlık e-posta alanları (admin'den `eposta_alani` engeliyle genişletilir).
TEK_KULLANIMLIK_ALANLAR = frozenset({
    "mailinator.com", "mailinator.net", "mailinator2.com", "notmailinator.com",
    "yopmail.com", "yopmail.fr", "yopmail.net", "guerrillamail.com", "guerrillamail.net",
    "guerrillamail.org", "guerrillamail.biz", "guerrillamailblock.com", "sharklasers.com",
    "grr.la", "pokemail.net", "spam4.me", "10minutemail.com", "10minutemail.net",
    "20minutemail.com", "temp-mail.org", "temp-mail.io", "tempmail.com", "tempmail.net",
    "tempmailo.com", "tempmail.dev", "tempr.email", "tmpmail.org", "tmpmail.net",
    "throwawaymail.com", "trashmail.com", "trashmail.de", "trashmail.net", "getnada.com",
    "nada.email", "dispostable.com", "maildrop.cc", "mailnesia.com", "mintemail.com",
    "mohmal.com", "emailondeck.com", "fakeinbox.com", "fakemail.net", "mailcatch.com",
    "moakt.com", "mytemp.email", "burnermail.io", "inboxkitten.com", "33mail.com",
    "spamgourmet.com", "mailpoof.com", "emailfake.com", "1secmail.com", "1secmail.net",
    "1secmail.org", "byom.de", "discard.email", "dropmail.me", "linshiyouxiang.net",
    "minuteinbox.com", "tempinbox.com", "mail.tm", "mailtemp.net", "luxusmail.org",
})

MESAJ_IP = "Bu ağdan erişim engellendi."
MESAJ_HESAP = ("Hesabınız askıya alındı. Bir hata olduğunu düşünüyorsanız "
               "destek@ihaletakip.com adresine yazın.")
MESAJ_EPOSTA = ("Tek kullanımlık e-posta adresleriyle kayıt olunamıyor. Lütfen kalıcı bir "
                "e-posta adresi kullanın.")


# ── Olay günlüğü ──────────────────────────────────────
def olay_yaz(tur, *, ist=None, request=None, kullanici=None, ip=None, kimlik=None,
             ayrinti=None, her_zaman=False):
    """
    `GuvenlikOlayi` satırı. Kimlik × tür başına dakikada tek satır (`her_zaman` hariç):
    bir kazıyıcı saniyede onlarca istek atarken günlük de bir yazma fırtınası olmasın.
    """
    from .models import GuvenlikOlayi

    try:
        kimlik = kimlik or getattr(ist, "kimlik", "") or ""
        if not her_zaman and not cache.add(
                f"guv:olay:{tur}:{kimlik}:{int(time.time() // 60)}", 1, 120):
            return None
        uid = getattr(ist, "uid", None)
        if kullanici is None and uid:
            kullanici_id = uid
        else:
            kullanici_id = getattr(kullanici, "pk", None)
        ip = ip or getattr(ist, "ip", "") or None
        return GuvenlikOlayi.objects.create(
            tur=tur, kimlik=kimlik[:80], kullanici_id=kullanici_id,
            ip=_gecerli_tek_ip(ip), ua=(getattr(ist, "ua", "") or "")[:300],
            istemci_turu=getattr(ist, "tur", "") if ist is not None else "",
            yol=(request.path[:300] if request is not None else ""),
            ayrinti=ayrinti or {},
        )
    except Exception:                                       # noqa: BLE001
        logger.warning("güvenlik olayı yazılamadı (%s)", tur, exc_info=True)
        return None


def _gecerli_tek_ip(ip):
    try:
        return str(ipaddress.ip_address(str(ip))) if ip else None
    except ValueError:
        return None


# ── IP / ağ doğrulaması ───────────────────────────────
def ag_coz(deger: str):
    """`deger` → `ip_network`. Engellenemeyecek bir ağsa `ValueError` (Türkçe mesaj)."""
    try:
        ag = ipaddress.ip_network((deger or "").strip(), strict=False)
    except ValueError as e:
        raise ValueError("Geçerli bir IP adresi ya da CIDR bloğu girin (ör. 203.0.113.7 "
                         "ya da 203.0.113.0/24).") from e
    if (ag.is_private or ag.is_loopback or ag.is_link_local or ag.is_multicast
            or ag.is_reserved or ag.is_unspecified):
        raise ValueError("Özel/yerel ağlar engellenemez: web portalı API'ye bu ağlardan "
                         "geliyor, engel bütün siteyi kapatırdı.")
    if ag.prefixlen < AZAMI_GENISLIK[ag.version]:
        raise ValueError(f"Blok çok geniş: IPv{ag.version} için en geniş "
                         f"/{AZAMI_GENISLIK[ag.version]} engellenebilir.")
    return ag


# ── Etkin engel listesi (süreç içi + Redis sürümü) ─────
def _surum():
    try:
        return cache.get(_SURUM_ANAHTARI)
    except Exception:                                       # noqa: BLE001
        return None


def degisti():
    """Engel listesi değişti → tüm süreçler bir sonraki istekte yeniden yükler."""
    try:
        cache.set(_SURUM_ANAHTARI, time.time(), None)
    except Exception:                                       # noqa: BLE001
        pass
    _YEREL["zaman"] = 0.0


def _yukle():
    from .models import ErisimEngeli

    simdi = time.time()
    surum = _surum()
    if _YEREL["zaman"] and simdi - _YEREL["zaman"] < _YEREL_TTL and surum == _YEREL["surum"]:
        return _YEREL
    aglar, alanlar = [], set()
    try:
        qs = (ErisimEngeli.objects.filter(aktif=True)
              .exclude(bitis__lte=timezone.now())
              .filter(tur__in=[ErisimEngeli.TUR_IP, ErisimEngeli.TUR_EPOSTA_ALANI])
              .values_list("pk", "tur", "deger"))
        for pk, tur, deger in qs:
            if tur == ErisimEngeli.TUR_IP:
                try:
                    aglar.append((ipaddress.ip_network(deger, strict=False), pk))
                except ValueError:
                    continue
            else:
                alanlar.add((deger or "").strip().lower())
    except Exception:                                       # noqa: BLE001
        logger.warning("engel listesi yüklenemedi (engel yok sayıldı)", exc_info=True)
        return _YEREL
    _YEREL.update(surum=surum, zaman=simdi, aglar=aglar, alanlar=frozenset(alanlar))
    return _YEREL


def ip_engeli(ip: str):
    """IP etkin bir engelin içindeyse engel id'si, değilse `None`."""
    if not ip:
        return None
    try:
        adres = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for ag, pk in _yukle()["aglar"]:
        if adres.version == ag.version and adres in ag:
            return pk
    return None


def isabet_say(engel_id):
    """Engel isabet sayacı — dakikada bir DB yazımı."""
    from .models import ErisimEngeli

    if not engel_id or not cache.add(f"guv:isabet:{engel_id}:{int(time.time() // 60)}", 1, 120):
        return
    try:
        ErisimEngeli.objects.filter(pk=engel_id).update(
            isabet_sayisi=F("isabet_sayisi") + 1, son_isabet=timezone.now())
    except Exception:                                       # noqa: BLE001
        pass


def eposta_engelli(email: str) -> bool:
    """Tek kullanımlık (yerleşik liste) ya da admin'den yasaklanmış alan mı?"""
    alan = (email or "").rsplit("@", 1)[-1].strip().lower()
    if not alan:
        return False
    if alan in TEK_KULLANIMLIK_ALANLAR or alan in _yukle()["alanlar"]:
        return True
    # Alt alan adı: `x.mailinator.com`
    return any(alan.endswith("." + a) for a in TEK_KULLANIMLIK_ALANLAR)


# ── Engel ekleme / kaldırma ───────────────────────────
def _bitis(sure):
    if sure is None:
        return None
    if isinstance(sure, timedelta):
        return timezone.now() + sure
    return sure


def ip_engelle(deger, *, sebep="", sure=None, olusturan=None):
    from .models import ErisimEngeli

    ag = ag_coz(deger)
    deger = str(ag) if ag.num_addresses > 1 else str(ag.network_address)
    engel = ErisimEngeli.objects.create(
        tur=ErisimEngeli.TUR_IP, deger=deger, sebep=sebep, bitis=_bitis(sure),
        olusturan=olusturan)
    degisti()
    olay_yaz("engel_eklendi", kimlik=f"ip:{deger}", ip=str(ag.network_address),
             kullanici=None, ayrinti={"engel": engel.pk, "sebep": sebep}, her_zaman=True)
    return engel


def eposta_alani_engelle(alan, *, sebep="", olusturan=None):
    from .models import ErisimEngeli

    alan = (alan or "").strip().lower().lstrip("@")
    if "." not in alan or "@" in alan:
        raise ValueError("Geçerli bir alan adı girin (ör. ornek.com).")
    engel = ErisimEngeli.objects.create(tur=ErisimEngeli.TUR_EPOSTA_ALANI, deger=alan,
                                        sebep=sebep, olusturan=olusturan)
    degisti()
    return engel


def hesap_engelle(kullanici, *, sebep="", sure=None, olusturan=None):
    """Hesabı askıya alır (`is_active=False`). Aynı hesapta etkin engel varsa onu döner."""
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken, OutstandingToken,
    )

    from .models import ErisimEngeli

    if kullanici.is_superuser:
        raise ValueError("Süper kullanıcı hesabı askıya alınamaz.")
    with transaction.atomic():
        mevcut = ErisimEngeli.objects.filter(
            tur=ErisimEngeli.TUR_KULLANICI, kullanici=kullanici, aktif=True).first()
        if mevcut:
            mevcut.bitis = _bitis(sure)
            mevcut.sebep = sebep or mevcut.sebep
            mevcut.save(update_fields=["bitis", "sebep", "updated_at"])
            return mevcut
        onceki = bool(kullanici.is_active)
        engel = ErisimEngeli.objects.create(
            tur=ErisimEngeli.TUR_KULLANICI, kullanici=kullanici,
            deger=(kullanici.email or kullanici.username or "")[:255], sebep=sebep,
            bitis=_bitis(sure), onceki_aktif=onceki, olusturan=olusturan)
        type(kullanici).objects.filter(pk=kullanici.pk).update(is_active=False)
        kullanici.is_active = False
    # Yenileme token'ları da kapanır (access token `is_active` kontrolüyle zaten düşer).
    try:
        for ot in OutstandingToken.objects.filter(user=kullanici):
            BlacklistedToken.objects.get_or_create(token=ot)
    except Exception:                                       # noqa: BLE001
        logger.warning("askıya alınan hesabın token'ları kara listeye alınamadı",
                       exc_info=True)
    olay_yaz("engel_eklendi", kimlik=f"u:{kullanici.pk}", kullanici=kullanici,
             ayrinti={"engel": engel.pk, "sebep": sebep}, her_zaman=True)
    return engel


def engeli_kaldir(engel, *, kaldiran=None, sebep="elle"):
    """Engeli pasifleştirir; hesap engelinde engelden önce aktif olan hesabı açar."""
    from .models import ErisimEngeli

    if not engel.aktif:
        return engel
    with transaction.atomic():
        engel.aktif = False
        engel.kaldirilma = timezone.now()
        engel.save(update_fields=["aktif", "kaldirilma", "updated_at"])
        if engel.tur == ErisimEngeli.TUR_KULLANICI and engel.kullanici_id:
            baska = ErisimEngeli.objects.filter(
                tur=ErisimEngeli.TUR_KULLANICI, kullanici_id=engel.kullanici_id,
                aktif=True).exclude(pk=engel.pk).exists()
            if engel.onceki_aktif and not baska:
                type(engel.kullanici).objects.filter(pk=engel.kullanici_id).update(
                    is_active=True)
    degisti()
    olay_yaz("engel_kaldirildi", kimlik=(f"u:{engel.kullanici_id}" if engel.kullanici_id
                                         else f"{engel.tur}:{engel.deger}"),
             kullanici=engel.kullanici if engel.kullanici_id else None,
             ayrinti={"engel": engel.pk, "sebep": sebep,
                      "kaldiran": getattr(kaldiran, "pk", None)}, her_zaman=True)
    return engel


def suresi_dolanlari_kaldir() -> int:
    from .models import ErisimEngeli

    n = 0
    for engel in ErisimEngeli.objects.filter(aktif=True, bitis__lte=timezone.now()):
        engeli_kaldir(engel, sebep="süre doldu")
        n += 1
    return n


def hesap_engeli(kullanici):
    """Hesabın etkin engeli (giriş uçlarının Türkçe mesajı için)."""
    from .models import ErisimEngeli

    if kullanici is None or not getattr(kullanici, "pk", None):
        return None
    return ErisimEngeli.objects.filter(
        tur=ErisimEngeli.TUR_KULLANICI, kullanici=kullanici, aktif=True).first()


# ── Şüphe puanı + otomatik askı ───────────────────────
OTOMASYON_UA = re.compile(
    r"headless|curl/|wget|python-requests|python-urllib|aiohttp|httpx|scrapy|go-http|"
    r"java/|okhttp/[0-9.]+$|phantom|selenium|playwright|puppeteer|node-fetch|axios/",
    re.I)


def supheli_puani(satir, user, kota_g):
    """
    Şüphe puanı + Türkçe gerekçeler. Panel (`/admin/guvenlik/`) ve otomatik askı
    (`otomatik_aski`) AYNI fonksiyonu kullanır — ekranda görülen puan, kararı veren puandır.

    `satir`: `guvenlik_izleme.en_yogun` satırı ({istek, red, ua, …}); `kota_g`: bugünkü
    kişi başı EKAP isteği.
    """
    puan, neden = 0, []
    if user is not None:
        yas = timezone.now() - user.date_joined
        if yas < timedelta(hours=24):
            puan += 2
            neden.append("hesap 24 saatten yeni")
        if eposta_engelli(user.email or ""):
            puan += 3
            neden.append("tek kullanımlık e-posta")
    ua = satir.get("ua") or ""
    if ua and OTOMASYON_UA.search(ua):
        puan += 3
        neden.append("otomasyon tarayıcısı")
    if satir["istek"] >= 2000:
        puan += 2
        neden.append("günde 2.000+ istek")
    elif satir["istek"] >= 800:
        puan += 1
        neden.append("günde 800+ istek")
    if satir.get("red", 0) >= 50:
        puan += 2
        neden.append("çok sayıda 429")
    if kota_g:
        puan += 1 if kota_g < 20 else 2
        neden.append(f"EKAP isteği {kota_g}")
    return puan, neden


def otomatik_aski(kuru: bool = False) -> list[int]:
    """
    Açık sinyallerde hesabı **24 saat** otomatik askıya alır (kullanıcı kararı, 2026-11-05).

    Koşulların HEPSİ (ayarlar `GUVENLIK_OTOMATIK_ASKI_*`):
    1. Hesap 24 saatten yeni (`YENI_SAAT`),
    2. şüphe puanı ≥ `PUAN` (5) — panelde kırmızı satır,
    3. bugün ≥ `ISTEK` (500) API isteği **ya da** kişi başı EKAP kotası dolu.
    ⚠️ Pro, staff ve superuser ASLA otomatik askıya alınmaz: kazıyıcı ödeme yapmaz,
    ödeyen bir müşteriyi yanlışlıkla kesmek kazıyıcıyı birkaç dakika geç durdurmaktan pahalıdır.
    ⚠️ IP otomatik ENGELLENMEZ — mobil operatör IP'leri paylaşılıyor (CGNAT).
    ⚠️ Kaldırmak: panel → Etkin engeller → Kaldır (ya da 24 sa sonra kendiliğinden).
    Dönen: askıya alınan kullanıcı id'leri. `kuru=True`: hiçbir şey yazmaz, yalnız adayları
    döner (canlıya almadan önce üretim verisiyle deneme için).
    """
    from django.conf import settings
    from django.contrib.auth import get_user_model

    from ekap.mobil import kota as kota_mod

    from . import guvenlik_izleme
    from .models import ErisimEngeli

    if not getattr(settings, "GUVENLIK_OTOMATIK_ASKI", True):
        return []
    esik_puan = getattr(settings, "GUVENLIK_OTOMATIK_ASKI_PUAN", 5)
    esik_istek = getattr(settings, "GUVENLIK_OTOMATIK_ASKI_ISTEK", 500)
    yeni_saat = getattr(settings, "GUVENLIK_OTOMATIK_ASKI_YENI_SAAT", 24)
    sure = timedelta(hours=getattr(settings, "GUVENLIK_OTOMATIK_ASKI_SURE_SAAT", 24))

    satirlar = [s for s in guvenlik_izleme.en_yogun(adet=200)
                if s["kimlik"].startswith("u:") and s["kimlik"][2:].isdigit()]
    if not satirlar:
        return []
    uidler = [int(s["kimlik"][2:]) for s in satirlar]
    sinir = timezone.now() - timedelta(hours=yeni_saat)
    kullanicilar = {u.pk: u for u in get_user_model().objects.filter(
        pk__in=uidler, is_active=True, is_staff=False, is_superuser=False,
        date_joined__gte=sinir)}
    askida = set(ErisimEngeli.objects.filter(
        tur=ErisimEngeli.TUR_KULLANICI, aktif=True, kullanici_id__in=list(kullanicilar))
        .values_list("kullanici_id", flat=True))
    gunluk = kota_mod.tablo()
    alinan = []
    for s in satirlar:
        u = kullanicilar.get(int(s["kimlik"][2:]))
        if u is None or u.pk in askida or getattr(u, "is_premium", False):
            continue
        kota_g, _ = kota_mod.ozet(s["kimlik"])
        seviye = kota_mod.seviye_bul(u) or "free"
        kota_dolu = kota_g >= max(1, gunluk.get(seviye, {}).get("gun", 0))
        puan, nedenler = supheli_puani(s, u, kota_g)
        if puan < esik_puan or not (s["istek"] >= esik_istek or kota_dolu):
            continue
        if kuru:
            logger.info("otomatik askı (kuru): uid=%s puan=%s %s", u.pk, puan, nedenler)
            alinan.append(u.pk)
            continue
        sebep = (f"Otomatik askı: puan {puan} ({', '.join(nedenler)}); "
                 f"bugün {s['istek']} istek, EKAP {kota_g}")
        try:
            engel = hesap_engelle(u, sebep=sebep, sure=sure)
        except ValueError:
            continue
        olay_yaz("otomatik_aski", kimlik=s["kimlik"], kullanici=u, ip=s.get("ip"),
                 ayrinti={"engel": engel.pk, "puan": puan, "nedenler": nedenler,
                          "istek": s["istek"], "ekap": kota_g, "ua": (s.get("ua") or "")[:120]},
                 her_zaman=True)
        logger.warning("otomatik askı: uid=%s puan=%s istek=%s", u.pk, puan, s["istek"])
        alinan.append(u.pk)
    return alinan
