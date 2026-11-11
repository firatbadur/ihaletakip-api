"""
Asistan ekleri — kapsam, sınırlar, kota, API şekilleri ve sohbete bağlama.

Dönüştürme ve Files API `assistant/services/attachments.py`'dedir; bu modül HTTP
uçlarının ve sohbet görevinin ortak iş kurallarını tutar (docs/plan-asistan-ekler.md
1.3, 1.4, 1.6, 1.8). Hata yanıtları `EkIstekHatasi` ile verilir: kod `errors.code`'da,
ek alanlar (`sifirlanma`, `limit`) `errors` içinde (PremiumRequired deseni).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max, Sum
from django.utils import timezone
from rest_framework.exceptions import APIException

from .attachments import hata_mesaji

logger = logging.getLogger("ihaletakip")

# Mobilin dosya seçicide kullandığı kabul listesi. Asıl karar sunucuda İMZAYLA verilir
# (uzantı yalnızca ipucu); bu liste istemcinin baştan eleyeceği türleri söyler.
TURLER = [
    *({"uzanti": u, "tur": "dokuman"} for u in
      ("pdf", "doc", "docx", "xls", "xlsx", "rtf", "txt", "csv", "htm", "html", "mht", "mhtml")),
    *({"uzanti": u, "tur": "fotograf"} for u in ("jpg", "jpeg", "png", "webp", "gif")),
]

VARSAYILAN_SORU = {
    "dokuman": "Bu dokümanı özetler misin?",
    "fotograf": "Bu fotoğrafta ne var, açıklar mısın?",
}


class EkIstekHatasi(APIException):
    """
    Ek uçlarının hata yanıtı. Zarf: `errors = {code, detail, …ek}`.

    ⚠️ Kod `errors.code`'dadır (mobil `http.js:87` oradan okur). `api_response(status=…)`
    ile dönmek kodsuz bir 4xx üretirdi — mevcut "profil yok" yanıtının hatası buydu.
    """

    default_code = "ek_hatasi"

    def __init__(self, kod, status_code, mesaj=None, **ek):
        self.status_code = status_code
        super().__init__({"code": kod, "detail": mesaj or hata_mesaji(kod) or kod})
        self.ek_hata = ek


# ── Kapsam ve sınırlar ──────────────────────────────────────────────────────

def ek_etkin(user) -> bool:
    """`ASSISTANT_EK_KAPSAM`'a göre bu kullanıcı için ekler açık mı? (Pro ayrı kapı.)"""
    if not settings.ASSISTANT_AGENT_ENABLED:
        return False  # eski anahtar kelime yolu ekleri okuyamaz
    kapsam = settings.ASSISTANT_EK_KAPSAM
    if kapsam == "herkes":
        return True
    if kapsam == "staff":
        return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    return False


def gun_baslangici():
    """Kota günü: İstanbul gece yarısı (TIME_ZONE)."""
    return timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)


def sifirlanma():
    return (gun_baslangici() + timedelta(days=1)).isoformat()


def gunluk_limit(tur) -> int:
    return (settings.ASSISTANT_EK_GUNLUK_FOTO if tur == "fotograf"
            else settings.ASSISTANT_EK_GUNLUK_DOKUMAN)


# ⚠️ Günlük hak SATIRLARDAN sayılmaz, ayrı bir sayaçtan sayılır: sohbet silme CASCADE
# ile ek satırlarını siler; satır saymak "20 doküman yükle → sohbeti sil → hak yeniden
# 20" döngüsüne izin verirdi (bağımsız incelemede bulundu, 2026-11-11). Sayaç Redis'te
# (gün anahtarlı, 2 gün TTL); Redis'e ulaşılamazsa DB sayımına düşülür.
_KOTA_TTL = 2 * 86400


def _kota_anahtari(user_id, tur, gun) -> str:
    return f"asistan:ek_kota:{user_id}:{tur}:{gun.isoformat()}"


def _db_sayimi(user_id, tur) -> int:
    from assistant.models import AssistantAttachment

    return AssistantAttachment.objects.filter(
        user_id=user_id, tur=tur, kota_sayildi=True, created_at__gte=gun_baslangici()
    ).count()


def gunluk_kullanilan(user, tur) -> int:
    try:
        deger = cache.get(_kota_anahtari(user.id, tur, timezone.localdate()))
    except Exception:
        deger = None
    return _db_sayimi(user.id, tur) if deger is None else int(deger)


def kota_harca(user, tur) -> None:
    """
    Kabul edilen yükleme hakkı düşer. Satır YARATILDIKTAN SONRA çağrılır.

    Sayaç yoksa bugünkü DB sayımıyla başlar — o sayım az önce yaratılan satırı zaten
    içerdiği için ayrıca artırılmaz (artırılsaydı ilk yüklemede hak iki kez düşerdi).
    """
    anahtar = _kota_anahtari(user.id, tur, timezone.localdate())
    try:
        if not cache.add(anahtar, _db_sayimi(user.id, tur), timeout=_KOTA_TTL):
            cache.incr(anahtar)
    except Exception:
        logger.warning("ek kota sayacı yazılamadı; DB sayımı kullanılacak")


def kota_iade(user_id, tur, olusma) -> None:
    """Hakkı geri verir (bizden kaynaklı hata, bağlanmamış ekin silinmesi). Sıfırın altına inmez."""
    anahtar = _kota_anahtari(user_id, tur, timezone.localdate(olusma))
    try:
        if (cache.get(anahtar) or 0) > 0:
            cache.decr(anahtar)
    except Exception:
        logger.warning("ek kota iadesi yazılamadı")


def gunluk_kalan(user) -> dict:
    """Bugün kalan hak. Superuser muaftır (sınırın kendisi döner)."""
    sonuc = {}
    for tur in ("dokuman", "fotograf"):
        limit = gunluk_limit(tur)
        sonuc[tur] = limit if getattr(user, "is_superuser", False) else max(
            0, limit - gunluk_kullanilan(user, tur))
    return sonuc


def kota_denetle(user, tur) -> None:
    """Günlük hak dolduysa 429 `ek_kotasi` (+ `sifirlanma`, `limit`)."""
    if getattr(user, "is_superuser", False):
        return
    limit = gunluk_limit(tur)
    if gunluk_kullanilan(user, tur) >= limit:
        ad = "fotoğraf" if tur == "fotograf" else "doküman"
        raise EkIstekHatasi(
            "ek_kotasi", 429,
            f"Bugünkü {ad} hakkınız doldu ({limit}). Yarın yenilenir.",
            sifirlanma=sifirlanma(), limit=limit,
        )


def limitler(user) -> dict:
    """`GET /assistant/attachments/limits/` gövdesi."""
    return {
        "etkin": ek_etkin(user),
        "azami_bayt": settings.ASSISTANT_EK_AZAMI_BAYT,
        "foto_azami_bayt": settings.ASSISTANT_EK_FOTO_AZAMI_BAYT,
        "turler": TURLER,
        "mesaj_azami": {"dokuman": settings.ASSISTANT_EK_MESAJ_DOKUMAN,
                        "fotograf": settings.ASSISTANT_EK_MESAJ_FOTO},
        "sohbet_azami": {"dokuman": settings.ASSISTANT_EK_SOHBET_DOKUMAN,
                         "fotograf": settings.ASSISTANT_EK_SOHBET_FOTO},
        "gunluk_kalan": gunluk_kalan(user),
        "saklama_gun": settings.ASSISTANT_EK_SAKLAMA_GUN,
        "yoklama_aralik_ms": settings.ASSISTANT_EK_YOKLAMA_ARALIK_MS,
        # ⚠️ Görev süresinden UZUN: eşit olsaydı görev 90. saniyede biterken istemci vazgeçerdi.
        "yoklama_azami_sn": settings.ASSISTANT_EK_SURE_SN + 30,
    }


# ── API şekilleri ───────────────────────────────────────────────────────────

def _iso(dt):
    return dt.isoformat() if dt else None


def ek_sozlugu(ek) -> dict:
    """`ek` nesnesi. ⚠️ `metin` ve `anthropic_file_id` ASLA burada değildir."""
    return {
        "id": str(ek.id), "tur": ek.tur, "kaynak": ek.kaynak, "ad": ek.ad,
        "uzanti": ek.uzanti, "boyut": ek.boyut, "durum": ek.durum,
        "hata_kodu": ek.hata_kodu or None,
        "hata_mesaji": hata_mesaji(ek.hata_kodu, ek.sayfa_sayisi) or None,
        "mod": ek.mod or None, "sayfa_sayisi": ek.sayfa_sayisi, "sira": ek.sira,
        "ikn": ek.ikn or None, "ekap_id": ek.ekap_id or None, "dtn": ek.dtn or None,
        "parca": ek.parca or None, "kucuk_resim": ek.kucuk_resim or None,
        "created_at": _iso(ek.created_at), "expires_at": _iso(ek.expires_at),
    }


def ek_ozeti(ek) -> dict:
    """Kullanıcı mesajının `payload.ekler` anlık özeti (durum yok: `data.ekler`'den birleşir)."""
    return {"id": str(ek.id), "tur": ek.tur, "ad": ek.ad, "uzanti": ek.uzanti,
            "sira": ek.sira, "parca": ek.parca or None, "kucuk_resim": ek.kucuk_resim or None}


# ── Sohbet ──────────────────────────────────────────────────────────────────

def sohbet_ekleri(conversation):
    """Sohbete bağlı TÜM ekler (süresi dolanlar dahil), `sira`ya göre."""
    from assistant.models import AssistantAttachment

    if conversation is None:
        return AssistantAttachment.objects.none()
    return AssistantAttachment.objects.filter(conversation=conversation).order_by("sira")


def kullanilabilir_mi(ek, simdi=None) -> bool:
    """Modele gönderilebilir mi? Bitişine 1 saatten az kalan ek gönderilmez."""
    simdi = simdi or timezone.now()
    return (ek.durum == "hazir" and ek.expires_at is not None
            and ek.expires_at > simdi + timedelta(hours=1))


def ek_turu(conversation, simdi=None) -> list:
    """Bu turda modele gidecek ekler: hazır, süresi dolmamış, `sira`ya göre."""
    return [e for e in sohbet_ekleri(conversation) if kullanilabilir_mi(e, simdi)]


def sohbet_kalan(conversation) -> dict:
    """Sohbette eklenebilecek kalan doküman / fotoğraf sayısı."""
    ekler = list(sohbet_ekleri(conversation)) if conversation else []
    return {
        "dokuman": max(0, settings.ASSISTANT_EK_SOHBET_DOKUMAN
                       - sum(1 for e in ekler if e.tur == "dokuman")),
        "fotograf": max(0, settings.ASSISTANT_EK_SOHBET_FOTO
                        - sum(1 for e in ekler if e.tur == "fotograf")),
    }


def kopya_olustur(asil, **ust_veri):
    """
    Hazır bir ekin içeriğini yeni bir satıra kopyalar (yeniden yükleme yok, hak yok).

    ⚠️ `expires_at` asıl ekten TAŞINIR: Files API süresi yüklemede bir kez verilir,
    uzatılamaz. Kopya, dosyanın gerçek ömründen uzun yaşıyor gibi görünmemeli.
    """
    from assistant.models import AssistantAttachment

    alanlar = {f: getattr(asil, f) for f in (
        "user_id", "tur", "kaynak", "ikn", "ekap_id", "dtn", "parca", "ad", "uzanti",
        "mime", "boyut", "sha256", "mod", "metin", "anthropic_file_id", "sayfa_sayisi",
        "token_sayisi", "kucuk_resim", "hazir_at", "expires_at")}
    alanlar.update({k: v for k, v in ust_veri.items() if v is not None})
    return AssistantAttachment.objects.create(
        **alanlar, durum="hazir", kota_sayildi=False)


def kopyalanabilir_mi(ek, simdi=None) -> bool:
    simdi = simdi or timezone.now()
    return (ek.durum == "hazir" and ek.expires_at is not None and ek.expires_at
            - simdi >= timedelta(days=settings.ASSISTANT_EK_KOPYA_MIN_GUN))


def sohbete_bagla(user, conversation, message, ek_idleri) -> list:
    """
    Ekleri denetleyip sohbete ve mesaja bağlar; bağlanan ekleri `sira`ya göre döner.

    Çağıran `transaction.atomic` içinde ve sohbet satırını `select_for_update` ile
    kilitlemiş olmalı (`sira` ataması yarışmasın). Hata: `EkIstekHatasi`.
    """
    from assistant.models import AssistantAttachment

    simdi = timezone.now()
    idler = list(dict.fromkeys(str(i) for i in ek_idleri))
    ekler = {str(e.id): e for e in AssistantAttachment.objects.filter(user=user, id__in=idler)}
    if len(ekler) != len(idler):
        raise EkIstekHatasi("ek_bulunamadi", 404, "Eklediğiniz dosya bulunamadı. Yeniden ekleyin.")

    secilen = []
    for i in idler:
        ek = ekler[i]
        if ek.durum == "hazirlaniyor" or ek.durum == "hata":
            raise EkIstekHatasi("ek_hazir_degil", 409, "Dosya henüz hazır değil.")
        if not kullanilabilir_mi(ek, simdi):
            raise EkIstekHatasi("ek_suresi_doldu", 410,
                                "Dosyanın saklama süresi doldu. Yeniden ekleyin.")
        secilen.append(ek)

    yeni = [e for e in secilen if e.conversation_id != conversation.id]
    dok = sum(1 for e in yeni if e.tur == "dokuman")
    foto = len(yeni) - dok
    if dok > settings.ASSISTANT_EK_MESAJ_DOKUMAN or foto > settings.ASSISTANT_EK_MESAJ_FOTO:
        raise EkIstekHatasi(
            "mesaj_ek_siniri", 400,
            f"Bir mesajla en fazla {settings.ASSISTANT_EK_MESAJ_DOKUMAN} doküman ve "
            f"{settings.ASSISTANT_EK_MESAJ_FOTO} fotoğraf gönderebilirsiniz.",
            limit={"dokuman": settings.ASSISTANT_EK_MESAJ_DOKUMAN,
                   "fotograf": settings.ASSISTANT_EK_MESAJ_FOTO})

    mevcut = list(AssistantAttachment.objects.filter(conversation=conversation))
    m_dok = sum(1 for e in mevcut if e.tur == "dokuman")
    m_foto = len(mevcut) - m_dok
    token = (AssistantAttachment.objects.filter(conversation=conversation, durum="hazir")
             .aggregate(t=Sum("token_sayisi"))["t"] or 0) + sum(e.token_sayisi or 0 for e in yeni)
    if (m_dok + dok > settings.ASSISTANT_EK_SOHBET_DOKUMAN
            or m_foto + foto > settings.ASSISTANT_EK_SOHBET_FOTO
            or token > settings.ASSISTANT_EK_SOHBET_TOKEN):
        raise EkIstekHatasi("sohbet_ek_siniri", 409,
                            "Bu sohbette çok fazla dosya var. Yeni bir sohbet açın.")

    sira = (AssistantAttachment.objects.filter(conversation=conversation)
            .aggregate(m=Max("sira"))["m"] or 0)
    sonuc = []
    for ek in secilen:
        if ek.conversation_id == conversation.id:
            sonuc.append(ek)          # zaten bu sohbette; yeniden numaralanmaz
            continue
        sira += 1
        alanlar = {"conversation": conversation, "message": message, "sira": sira}
        if ek.conversation_id is None:
            # ⚠️ Koşullu UPDATE: aynı bağsız ek iki sohbete eşzamanlı gönderilirse (çift
            # dokunuş) yalnızca biri bağlar; öteki kopya alır. Düz `save()` son yazanı
            # kazandırır ve ilk sohbetin mesajı modelin hiç görmediği eki gösterirdi.
            if AssistantAttachment.objects.filter(
                    id=ek.id, conversation__isnull=True).update(**alanlar):
                for k, v in alanlar.items():
                    setattr(ek, k, v)
                sonuc.append(ek)
                continue
            ek.refresh_from_db()
        # Başka sohbete bağlı: içeriği kopyalanır (hak harcanmaz, yeniden yükleme yok).
        kopya = kopya_olustur(ek)
        for k, v in alanlar.items():
            setattr(kopya, k, v)
        kopya.save(update_fields=list(alanlar))
        sonuc.append(kopya)
    return sorted(sonuc, key=lambda e: e.sira or 0)
