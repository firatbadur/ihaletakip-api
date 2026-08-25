"""
Geçmiş abonelik iptallerini RevenueCat'ten geriye dönük doldurur.

Neden gerekli: iptal izi (`User.subscription_cancelled_at` ...) yalnızca RC webhook'u
geldiğinde yazılır ve bu özellik eklenmeden ÖNCE gelen webhook'lar hiçbir yere
kaydedilmedi. Yani "şimdiye kadar iptal edenler" DB'de YOK — ama RC'de duruyor.
Bu komut onları RC'den çekip aynı alanlara yazar (`subscription_last_event="BACKFILL"`).

Akış (kullanıcı başına, `app_user_id = str(user.id)`):
  1. v2 `customers/{id}/subscriptions` → 404 ise müşteri yok, ATLA (ucuz).
     ⚠️ Önce v2 sorulur çünkü v1 `subscribers/{id}` olmayan müşteriyi **yaratır**.
  2. v1 `subscribers/{id}` → `unsubscribe_detected_at` (iptalin ZAMANI),
     `period_type` (trial/normal/intro), `expires_date`, `billing_issues_detected_at`.
  3. En ileri `expires_date`'e sahip abonelik esas alınır → iptal izi yazılır.

Kullanım:
    python manage.py backfill_subscription_cancels --dry-run
    python manage.py backfill_subscription_cancels --limit 500
    python manage.py backfill_subscription_cancels --force   # webhook izini de ezer
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User


def _en_guncel(subs: dict):
    """v1 `subscriptions` sözlüğünden en ileri `expires_date`'li aboneliği seç."""
    from subscription.services.revenuecat import _parse_rc_ts

    en_iyi, en_iyi_bitis = None, None
    for urun, sub in (subs or {}).items():
        bitis = _parse_rc_ts(sub.get("expires_date"))
        if en_iyi is None or (bitis and (en_iyi_bitis is None or bitis > en_iyi_bitis)):
            en_iyi, en_iyi_bitis = dict(sub, product_id=urun), bitis
    return en_iyi, en_iyi_bitis


class Command(BaseCommand):
    help = "Geçmiş abonelik iptallerini RevenueCat'ten doldurur (tek seferlik)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Yazma, yalnızca raporla.")
        parser.add_argument("--limit", type=int, default=0, help="En çok N kullanıcı işle.")
        parser.add_argument("--from-pk", type=int, default=0, help="Bu pk'dan itibaren başla.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Webhook'tan gelmiş mevcut izi de ez (varsayılan: yalnızca boş olanı doldur).",
        )
        parser.add_argument(
            "--sleep", type=float, default=0.1, help="İstekler arası bekleme (sn), vars. 0.1."
        )

    def handle(self, *args, **opts):
        import time

        from subscription.services.revenuecat import (
            RevenueCatError,
            _parse_rc_ts,
            customer_subscriptions,
            v1_subscriber,
        )

        kuru = opts["dry_run"]
        qs = User.objects.filter(pk__gte=opts["from_pk"]).order_by("pk")
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        toplam = musteri = iptal = yazildi = hata = 0
        simdi = timezone.now()

        for user in qs.iterator(chunk_size=200):
            toplam += 1
            try:
                subs = customer_subscriptions(str(user.id))
            except RevenueCatError as e:
                hata += 1
                self.stderr.write(f"uid={user.pk} RC hatası: {e}")
                continue
            if opts["sleep"]:
                time.sleep(opts["sleep"])
            if not subs:
                continue  # müşteri yok ya da hiç aboneliği olmamış
            musteri += 1

            try:
                subscriber = v1_subscriber(str(user.id)) or {}
            except RevenueCatError as e:
                hata += 1
                self.stderr.write(f"uid={user.pk} v1 hatası: {e}")
                continue
            if opts["sleep"]:
                time.sleep(opts["sleep"])

            sub, bitis = _en_guncel(subscriber.get("subscriptions") or {})
            if not sub:
                continue

            unsub = _parse_rc_ts(sub.get("unsubscribe_detected_at"))
            fatura = _parse_rc_ts(sub.get("billing_issues_detected_at"))
            donem = (sub.get("period_type") or "").upper()[:16]

            if unsub:
                iptal_at, neden = unsub, "UNSUBSCRIBE"
            elif bitis and bitis <= simdi:
                # Yenilenmemiş ve süresi dolmuş: ya fatura sorunu ya sessiz bitiş.
                iptal_at, neden = bitis, ("BILLING_ERROR" if fatura else "")
            else:
                continue  # aktif ve yenilenecek → iptal yok

            iptal += 1
            mevcut = user.subscription_last_event
            if user.subscription_cancelled_at is not None and not opts["force"]:
                continue  # webhook zaten yazmış, ezme
            if mevcut and mevcut != "BACKFILL" and not opts["force"]:
                continue

            self.stdout.write(
                f"uid={user.pk} {user.email} → iptal={iptal_at:%Y-%m-%d} "
                f"neden={neden or '-'} donem={donem or '-'}"
            )
            if kuru:
                continue
            user.subscription_cancelled_at = iptal_at
            user.subscription_cancel_reason = neden
            user.subscription_period_type = donem
            user.subscription_last_event = "BACKFILL"
            user.save(
                update_fields=[
                    "subscription_cancelled_at",
                    "subscription_cancel_reason",
                    "subscription_period_type",
                    "subscription_last_event",
                ]
            )
            yazildi += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"bitti: taranan={toplam} rc_musterisi={musteri} iptal_bulunan={iptal} "
                f"yazilan={yazildi} hata={hata}" + (" (DRY RUN)" if kuru else "")
            )
        )
