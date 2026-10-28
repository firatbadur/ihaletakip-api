"""
Pro hakkı — `ProGrant` satırlarından etkin katman hesabı.

⚠️⚠️ `User.subscription_tier` / `subscription_expires_at` alanlarının TEK yazarı
`recompute_pro`'dur. Kaynaklar (mağaza / web / admin) yalnızca kendi `ProGrant`
satırlarını yazar; kullanıcı alanına dokunmaz. Eskiden RevenueCat senkronu kullanıcı
alanını doğrudan yazıyordu ve RC'de hakkı olmayan herkesi (web'den ödeyen, admin'den
Pro verilen) mobil uygulama her açıldığında Free'ye çekiyordu.

**Zincir**: başlamış etkin hakların en geç bitişinden başlayıp, o ana kadar BAŞLAMIŞ
olacak hakları da ekleyerek ilerlenir. Pro'yken alınan ikinci yıl (`starts_at` =
mevcut bitiş) böylece kesintisiz sayılır — yalnızca "başlamış satırlara" bakılsaydı
etkin bitiş ilk yılın sonunda kalır ve o an hiçbir görev yeniden hesap yapmadığı için
kullanıcı (parasını ödediği) ikinci yılın başında Free görünürdü.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

logger = logging.getLogger("ihaletakip")


@dataclass(frozen=True)
class EtkinHak:
    pro: bool
    ends_at: datetime | None  # pro + None = bitişsiz
    source: str | None  # etkin bitişi sağlayan kaynak
    grant_id: int | None


_FREE = EtkinHak(False, None, None, None)
# Eşit bitişte hangi kaynak "sağlayan" sayılır: mağaza yenilenir, admin elle yönetilir.
_ONCELIK = {"store": 0, "admin": 1, "iyzico": 2}


def etkin_hak(user, now=None) -> EtkinHak:
    from subscription.models import ProGrant

    now = now or timezone.now()
    satirlar = list(
        ProGrant.objects.filter(user=user, revoked_at__isnull=True)
        .order_by("starts_at", "pk")
        .values("pk", "source", "starts_at", "ends_at")
    )
    basladi = [s for s in satirlar if s["starts_at"] <= now]
    etkin = [s for s in basladi if s["ends_at"] is None or s["ends_at"] > now]
    if not etkin:
        return _FREE
    bitissiz = [s for s in etkin if s["ends_at"] is None]
    if bitissiz:
        s = min(bitissiz, key=lambda x: _ONCELIK.get(x["source"], 9))
        return EtkinHak(True, None, s["source"], s["pk"])

    def sec(adaylar):
        # En geç bitiş; eşitlikte öncelik sırası.
        return max(adaylar, key=lambda x: (x["ends_at"], -_ONCELIK.get(x["source"], 9)))

    en = sec(etkin)
    bitis = en["ends_at"]
    while True:
        devam = [
            s for s in satirlar
            if s["starts_at"] <= bitis and (s["ends_at"] is None or s["ends_at"] > bitis)
        ]
        if not devam:
            break
        bitissiz = [s for s in devam if s["ends_at"] is None]
        if bitissiz:
            s = min(bitissiz, key=lambda x: _ONCELIK.get(x["source"], 9))
            return EtkinHak(True, None, s["source"], s["pk"])
        en = sec(devam)
        bitis = en["ends_at"]
    return EtkinHak(True, bitis, en["source"], en["pk"])


def recompute_pro(user, now=None) -> bool:
    """Etkin hakkı kullanıcı alanlarına yazar (yalnızca değiştiyse). Değişiklik döner."""
    from accounts.models import User

    hak = etkin_hak(user, now)
    tier = User.Tier.PRO if hak.pro else User.Tier.FREE
    expiry = hak.ends_at if hak.pro else None
    if user.subscription_tier == tier and user.subscription_expires_at == expiry:
        return False
    user.subscription_tier = tier
    user.subscription_expires_at = expiry
    user.save(update_fields=["subscription_tier", "subscription_expires_at"])
    logger.info("pro hakkı: uid=%s tier=%s bitiş=%s kaynak=%s",
                user.pk, tier, expiry, hak.source)
    return True


def magaza_hakki_yaz(user, is_pro: bool, expiry, now=None) -> None:
    """RevenueCat sonucunu kullanıcının TEK `store` satırına yazar.

    Aktif değilse satır silinmez, `ends_at = şimdi` yapılır (geçmiş korunur). Hiç
    satırı olmayan Free kullanıcı için satır açılmaz.
    """
    from subscription.models import ProGrant

    now = now or timezone.now()
    satir = ProGrant.objects.filter(user=user, source=ProGrant.Source.STORE).first()
    if is_pro:
        if satir is None:
            ProGrant.objects.create(
                user=user, source=ProGrant.Source.STORE, starts_at=now, ends_at=expiry
            )
            return
        once = (satir.starts_at, satir.ends_at, satir.revoked_at)
        # Bitmiş satır yeniden aktifleşiyorsa (yeni abonelik) başlangıç bugündür.
        if satir.revoked_at is not None or (satir.ends_at is not None and satir.ends_at <= now):
            satir.starts_at = now
        satir.ends_at = expiry
        satir.revoked_at = None
        if (satir.starts_at, satir.ends_at, satir.revoked_at) != once:
            satir.save(update_fields=["starts_at", "ends_at", "revoked_at", "updated_at"])
        return
    if satir is not None and (satir.ends_at is None or satir.ends_at > now):
        satir.ends_at = now
        satir.save(update_fields=["ends_at", "updated_at"])


def magaza_aktif(user, now=None) -> bool:
    from subscription.models import ProGrant

    now = now or timezone.now()
    s = ProGrant.objects.filter(
        user=user, source=ProGrant.Source.STORE, revoked_at__isnull=True,
        starts_at__lte=now,
    ).first()
    return bool(s and (s.ends_at is None or s.ends_at > now))


def web_baslangic(user, now=None):
    """Yeni web hakkının başlangıcı: max(şimdi, iptal edilmemiş en geç web bitişi).

    ⚠️ Yalnızca WEB satırlarına bakılır (istek #17.3): mağaza aboneliği her ay yenilenir
    ve bitişi oynaktır; web yılını onun arkasına zincirlemek kullanıcının ödediği yılı
    mağaza iptal edilene kadar ileri iterdi. İki kaynak üst üste binerse etkin bitiş
    ikisinin geç olanıdır (`etkin_hak`).
    """
    from django.db.models import Max

    from subscription.models import ProGrant

    now = now or timezone.now()
    son = (
        ProGrant.objects.filter(
            user=user, source=ProGrant.Source.IYZICO, revoked_at__isnull=True
        ).aggregate(m=Max("ends_at"))["m"]
    )
    return max(now, son) if son else now
