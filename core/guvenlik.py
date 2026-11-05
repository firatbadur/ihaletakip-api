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
