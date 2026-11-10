"""
E-posta engelleri — kalıcı bounce / spam şikâyeti gelen adrese bir daha gönderilmez.

Tek doğruluk kaynağı `accounts.EpostaEngeli` (`aktif=True`). Engel SNS geri bildiriminde
(`ses_sns._kaydet`) kendiliğinden konur, admin'den elle konur/kaldırılır.

**Nerede uygulanır**: gönderim katmanında (`core.eposta.SaglayiciBackend.send_messages`)
— engelli alıcı mesajdan çıkarılır, alıcısı kalmayan mesaj hiç gönderilmez. Böylece
bülten, toplu gönderim, doğrulama kodu ve gelecekte yazılacak her yol aynı kapıdan
geçer. Bülten ve toplu gönderim ayrıca adayları baştan eler (sayılar doğru görünsün).

⚠️ DB hatasında engel kontrolü **gönderimi durdurmaz** (log + gönder): doğrulama kodu
gibi kullanıcının beklediği bir e-postayı bir kontrol hatası yüzünden yutmak, nadir bir
bounce'tan kötüdür.
"""
from __future__ import annotations

import logging
from email.utils import parseaddr

from django.db.models import F
from django.utils import timezone

logger = logging.getLogger("ihaletakip")


def normalize(adres: str) -> str:
    """`"Ad <x@y.com>"` ya da `" X@Y.com "` → `"x@y.com"`."""
    return (parseaddr(adres or "")[1] or "").strip().lower()


def engelli_kume(adresler) -> set[str]:
    """Verilen adreslerden ENGELLİ olanların normalize kümesi (tek sorgu)."""
    from accounts.models import EpostaEngeli

    temiz = {normalize(a) for a in adresler} - {""}
    if not temiz:
        return set()
    return set(EpostaEngeli.objects.filter(aktif=True, email__in=temiz)
               .values_list("email", flat=True))


def engelli_mi(adres: str) -> bool:
    return bool(engelli_kume([adres]))


def tum_engelliler() -> set[str]:
    """Toplu işlerde aday elemek için: tüm etkin engellerin kümesi (tablo küçük)."""
    from accounts.models import EpostaEngeli

    return set(EpostaEngeli.objects.filter(aktif=True).values_list("email", flat=True))


def engelle(adres: str, sebep: str, aciklama: str = "") -> bool:
    """Adresi engeller (varsa yeniden etkinleştirir). Yeni engel konduysa True."""
    from accounts.models import EpostaEngeli

    email = normalize(adres)
    if not email:
        return False
    satir, yeni = EpostaEngeli.objects.get_or_create(
        email=email, defaults={"sebep": sebep, "aciklama": (aciklama or "")[:500]})
    if yeni:
        logger.warning("e-posta engellendi: %s (%s)", email, sebep)
        return True
    EpostaEngeli.objects.filter(pk=satir.pk).update(
        sayac=F("sayac") + 1, aktif=True, sebep=sebep, kaldirildi_at=None,
        aciklama=(aciklama or satir.aciklama)[:500], updated_at=timezone.now())
    if not satir.aktif:
        logger.warning("e-posta engeli yeniden etkin: %s (%s)", email, sebep)
        return True
    return False


def engeli_kaldir(satir, kaldiran=None) -> None:
    satir.aktif = False
    satir.kaldirildi_at = timezone.now()
    satir.kaldiran = kaldiran
    satir.save(update_fields=["aktif", "kaldirildi_at", "kaldiran", "updated_at"])
    logger.info("e-posta engeli kaldırıldı: %s (kaldıran=%s)", satir.email,
                getattr(kaldiran, "pk", None))
