"""
subscription Celery görevleri.

Webhook hızlıca 200 döner ve senkronu buraya atar (RC API çağrısı istek yolunu
bloklamaz). Görev RC'yi sorgular; ulaşılamazsa event verisine (yedek) düşer.
"""
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("ihaletakip")


@shared_task(name="subscription.tasks.sync_subscription_task")
def sync_subscription_task(user_id, event=None):
    """Kullanıcının aboneliğini RC'den senkronlar; RC hatasında event yedeğine düşer."""
    from accounts.models import User
    from .services.revenuecat import (
        RevenueCatError,
        apply_event_fallback,
        sync_user_subscription,
    )

    user = User.objects.filter(pk=user_id).first()
    if not user:
        logger.warning("sync_subscription_task: kullanıcı yok uid=%s", user_id)
        return {"ok": False, "reason": "user_not_found"}

    try:
        sync_user_subscription(user)
        return {"ok": True, "tier": user.subscription_tier}
    except RevenueCatError:
        logger.exception("sync_subscription_task: RC sorgusu başarısız, event yedeği uid=%s", user_id)
        if event:
            try:
                apply_event_fallback(user, event)
            except Exception:
                logger.exception("sync_subscription_task: event yedeği de başarısız uid=%s", user_id)
        return {"ok": False, "tier": user.subscription_tier}


# ── Deneme bitiş hatırlatması ──────────────────────────────
# ⚠️⚠️ Bu görev bir VAADİN karşılığıdır: mobil paywall deneme çizelgesinde
# "{5}. gün — deneme bitmeden bildirimle hatırlatırız" yazıyor (IhaleTakip
# `src/screens/Paywall/helpers.js` → REMINDER_DAYS_BEFORE = 2). Görev kapatılır ya da
# pencere değiştirilirse mobildeki metin de değişmeli; aksi halde kullanıcıya verilen
# söz tutulmaz ve "habersiz ücret" şikâyeti/iadesi doğar.
#
# Pencere: bitişe 24–52 saat kala. Üst sınır 48 değil 52: görev saatlik koşuyor ve
# push sessiz saatte (22–07) / min. aralıkta atlanır → gönderilemeyen kullanıcı bir
# sonraki turda yeniden denenir. `trial_reminder_sent_at` YALNIZCA push en az bir
# cihaza gittiyse yazılır.
#
# İptal etmiş deneme kullanıcısına GİTMEZ: ücret alınmayacak, hatırlatma gürültüdür.
TRIAL_REMINDER_MIN_HOURS = 24
TRIAL_REMINDER_MAX_HOURS = 52

_AYLAR = [
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]


def _gun_ay(dt):
    yerel = timezone.localtime(dt)
    return f"{yerel.day} {_AYLAR[yerel.month - 1]}"


@shared_task(name="subscription.tasks.send_trial_reminders")
def send_trial_reminders():
    """Denemesi 1–2 gün içinde bitecek (iptal etmemiş) kullanıcıya tek push."""
    from datetime import timedelta

    from accounts.models import User
    from tenders.services.notify import push_to_user

    now = timezone.now()
    adaylar = User.objects.filter(
        subscription_tier=User.Tier.PRO,
        subscription_period_type="TRIAL",
        subscription_cancelled_at__isnull=True,
        trial_reminder_sent_at__isnull=True,
        subscription_expires_at__gte=now + timedelta(hours=TRIAL_REMINDER_MIN_HOURS),
        subscription_expires_at__lte=now + timedelta(hours=TRIAL_REMINDER_MAX_HOURS),
    )
    gonderilen = 0
    for user in adaylar.iterator():
        tarih = _gun_ay(user.subscription_expires_at)
        ok = push_to_user(
            user,
            title="Ücretsiz denemen yakında bitiyor",
            body=(
                f"{tarih} tarihinde Pro aboneliğin başlayacak. Devam etmek istemezsen "
                "mağaza hesabından iptal edebilirsin; o tarihe kadar ücret alınmaz."
            ),
            data={"type": "INFO", "subscription": "trial_reminder"},
            idem_key=f"trial_reminder:{user.pk}:{user.subscription_expires_at:%Y%m%d}",
        )
        if ok:
            User.objects.filter(pk=user.pk).update(trial_reminder_sent_at=now)
            gonderilen += 1
    logger.info("deneme hatırlatması: %s kullanıcıya gönderildi", gonderilen)
    return {"sent": gonderilen}


@shared_task(name="subscription.tasks.magaza_fiyatlarini_guncelle")
def magaza_fiyatlarini_guncelle():
    """Pro paketlerinin mağaza fiyatlarını tazeler (bkz. services/magaza_fiyat.py)."""
    from .services.magaza_fiyat import guncelle

    return guncelle()
