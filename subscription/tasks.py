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

    from .models import ProGrant

    now = timezone.now()
    # ⚠️ Pencere MAĞAZA hakkının bitişine bakar, kullanıcının etkin bitişine değil
    # (web 6. not #15.6): hatırlatma mağazanın ücret çekeceği anın haberidir. Web'den
    # de Pro'su olan kullanıcının etkin bitişi daha geç olabilir ama mağaza yine çeker.
    magaza = ProGrant.objects.filter(
        source=ProGrant.Source.STORE,
        revoked_at__isnull=True,
        ends_at__gte=now + timedelta(hours=TRIAL_REMINDER_MIN_HOURS),
        ends_at__lte=now + timedelta(hours=TRIAL_REMINDER_MAX_HOURS),
    )
    bitisler = dict(magaza.values_list("user_id", "ends_at"))
    adaylar = User.objects.filter(
        pk__in=list(bitisler),
        subscription_tier=User.Tier.PRO,
        subscription_period_type="TRIAL",
        subscription_cancelled_at__isnull=True,
        trial_reminder_sent_at__isnull=True,
    )
    gonderilen = 0
    for user in adaylar.iterator():
        bitis = bitisler[user.pk]
        tarih = _gun_ay(bitis)
        ok = push_to_user(
            user,
            title="Ücretsiz denemen yakında bitiyor",
            body=(
                f"{tarih} tarihinde Pro aboneliğin başlayacak. Devam etmek istemezsen "
                "mağaza hesabından iptal edebilirsin; o tarihe kadar ücret alınmaz."
            ),
            data={"type": "INFO", "subscription": "trial_reminder"},
            idem_key=f"trial_reminder:{user.pk}:{bitis:%Y%m%d}",
        )
        if ok:
            User.objects.filter(pk=user.pk).update(trial_reminder_sent_at=now)
            gonderilen += 1
    logger.info("deneme hatırlatması: %s kullanıcıya gönderildi", gonderilen)
    return {"sent": gonderilen}


# ── Web'den Pro satışı (iyzico) ────────────────────────
@shared_task(name="subscription.tasks.odeme_mutabakat")
def odeme_mutabakat():
    """15 dk'dan eski `pending` ödemeleri iyzico'dan sorar (dönüş hiç gelmediyse
    — kullanıcı sekmeyi kapattı — hakkı bu görev açar)."""
    from django.conf import settings

    from .services.odeme import mutabakat

    if not (settings.IYZICO_API_KEY and settings.IYZICO_SECRET_KEY):
        return {"atlandi": "anahtar_yok"}
    return mutabakat()


_EPOSTA_TURLERI = ("alindi", "fatura", "fesih", "iade")


@shared_task(name="subscription.tasks.odeme_eposta_task", bind=True, max_retries=3,
             default_retry_delay=300)
def odeme_eposta_task(self, odeme_id, tur):
    """Ödeme işlem e-postası. SMTP düşerse 5 dk arayla 3 kez yeniden denenir."""
    from .models import Payment
    from .services import eposta
    from .services.pro_hak import etkin_hak

    if tur not in _EPOSTA_TURLERI:
        return {"ok": False, "reason": "tur"}
    odeme = Payment.objects.select_related("user", "grant").filter(pk=odeme_id).first()
    if odeme is None or not odeme.user.email:
        return {"ok": False, "reason": "yok"}
    try:
        if tur == "alindi":
            eposta.odeme_alindi(odeme)
        elif tur == "fatura":
            eposta.fatura_hazir(odeme)
        else:
            hak = etkin_hak(odeme.user)
            bitis = hak.ends_at if hak.pro else None
            if tur == "fesih":
                eposta.fesih_alindi(odeme, bitis or getattr(odeme.grant, "ends_at", None))
            else:
                eposta.iade_yapildi(odeme, bitis)
    except Exception as e:  # noqa: BLE001 — SMTP/şablon; yeniden denenir
        logger.warning("ödeme e-postası gönderilemedi odeme=%s tur=%s: %s", odeme_id, tur, e)
        raise self.retry(exc=e)
    return {"ok": True}


@shared_task(name="subscription.tasks.fatura_kes_task")
def fatura_kes_task(odeme_id):
    """e-Arşiv / e-Fatura kesimi — entegratör (`FATURA_SAGLAYICI`) henüz SEÇİLMEDİ.

    ⚠️ Sağlayıcı boşken bilinçli olarak hiçbir şey yapmaz (sahte fatura numarası
    üretmek yasal belge taklidi olurdu): fatura muhasebe tarafından kesilip admin'den
    ödemeye yüklenir, "Fatura hazır" e-postası oradan gider. Entegratör seçilince
    `services/fatura.py` yazılıp buradan çağrılır.
    """
    from django.conf import settings

    if not settings.FATURA_SAGLAYICI:
        logger.info("fatura: sağlayıcı yok, elle kesilecek odeme=%s", odeme_id)
        return {"ok": False, "reason": "saglayici_yok"}
    logger.error("fatura: FATURA_SAGLAYICI=%r tanımlı ama entegrasyon yazılmadı",
                 settings.FATURA_SAGLAYICI)
    return {"ok": False, "reason": "entegrasyon_yok"}


# Web Pro bitişine kalan gün eşikleri. ⚠️ Gün kaçarsa (beat/worker kapalı) eşiğe
# "en yakın küçük eşik" olarak yine gönderilir; büyük eşikler de gönderilmiş sayılır
# → bir kesintiden sonra arka arkaya üç e-posta gitmez.
WEB_HATIRLATMA_ESIKLERI = (30, 7, 1)


@shared_task(name="subscription.tasks.web_sure_hatirlat")
def web_sure_hatirlat():
    """Web (iyzico) kaynaklı etkin bitişe 30/7/1 gün kala + bitince e-posta.

    Etkin bitişi başka kaynak (mağaza/admin) sağlıyorsa ya da mağaza aboneliği aktifse
    gönderilmez. Hatırlatmalar `email_notifications` tercihine uyar (SES bounce'u da
    bu bayrağı kapatıyor → geri dönen adrese gönderim itibarı bozmasın).
    """
    from datetime import timedelta

    from .models import ProGrant
    from .services import eposta
    from .services.pro_hak import etkin_hak, magaza_aktif

    now = timezone.now()
    bugun = timezone.localdate()
    adaylar = (
        ProGrant.objects.filter(
            source=ProGrant.Source.IYZICO, revoked_at__isnull=True,
            ends_at__gte=now - timedelta(days=3),
            ends_at__lte=now + timedelta(days=WEB_HATIRLATMA_ESIKLERI[0] + 1),
            user__email_notifications=True, user__is_active=True,
        ).select_related("user")
    )
    gonderilen = 0
    for grant in adaylar.iterator():
        user = grant.user
        if not user.email:
            continue
        gonderilmis = set(grant.hatirlatmalar or [])
        if grant.ends_at <= now:
            # Bitti: etkin bitiş bu hak olmalı (başka kaynak sürüyorsa haber değil).
            if "bitti" in gonderilmis or etkin_hak(user, now).pro:
                continue
            etiket, gonder = ["bitti"], lambda: eposta.sure_bitti(user, grant.ends_at)
        else:
            hak = etkin_hak(user, now)
            if hak.grant_id != grant.pk or magaza_aktif(user, now):
                continue
            kalan = (timezone.localtime(grant.ends_at).date() - bugun).days
            uygun = [e for e in WEB_HATIRLATMA_ESIKLERI if kalan <= e]
            if not uygun:
                continue
            esik = min(uygun)
            if str(esik) in gonderilmis:
                continue
            etiket = [str(e) for e in uygun]
            gonder = (lambda k=max(kalan, 0): eposta.sure_hatirlat(user, grant.ends_at, k))
        try:
            gonder()
        except Exception as e:  # noqa: BLE001 — sonraki gün yeniden denenir
            logger.warning("web süre hatırlatması gönderilemedi grant=%s: %s", grant.pk, e)
            continue
        grant.hatirlatmalar = sorted(gonderilmis | set(etiket))
        grant.save(update_fields=["hatirlatmalar", "updated_at"])
        gonderilen += 1
    logger.info("web süre hatırlatması: %s e-posta", gonderilen)
    return {"sent": gonderilen}
