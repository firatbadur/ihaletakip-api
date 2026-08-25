"""
Geçmiş abonelik iptallerini RevenueCat'ten geriye dönük doldurur (tek seferlik).

Neden gerekli: iptal izi (`User.subscription_cancelled_at` ...) yalnızca RC webhook'u
geldiğinde yazılır ve bu özellik eklenmeden ÖNCE gelen webhook'lar hiçbir yere
kaydedilmedi. Yani "şimdiye kadar iptal edenler" DB'de YOK — ama RC'de duruyor.

⚠️ **Yalnızca production abonelikler sayılır** (`environment="production"`);
sandbox/test_store kayıtları TEST verisidir ve admin'i sahte iptalle doldururdu
(`--include-sandbox` ile dahil edilebilir).

⚠️ **Yalnızca v2 kullanılır.** RC'nin yeni proje anahtarları (`sk_...`) V1 API'ye
kapalıdır (`403 code 7723`), dolayısıyla iptalin **tam zamanını** veren
`unsubscribe_detected_at` bize ulaşmıyor. İz v2 alanlarından türetilir ve
`subscription_last_event="BACKFILL"` ile işaretlenir → admin bu satırlarda tarihi
**yaklaşık (≈)** gösterir. Webhook'tan gelen izler kesindir, backfill onları ezmez.

Akış (kullanıcı başına, `app_user_id = str(user.id)`):
  1. v2 `customers/{id}/subscriptions` → 404/boş ise müşteri yok, ATLA (ucuz).
  2. En ileri `current_period_ends_at`'e sahip abonelik esas alınır.
  3. `auto_renewal_status` / `status` iptali söylüyorsa iz yazılır.

Kullanım:
    python manage.py backfill_subscription_cancels --probe 3   # ham v2 JSON'u göster
    python manage.py backfill_subscription_cancels --dry-run
    python manage.py backfill_subscription_cancels [--limit N] [--from-pk N] [--force]
"""
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User

# ⚠️ PROD ÖLÇÜMÜ (2026-08-25, --probe): süresi dolmuş HER abonelik
# `auto_renewal_status="will_not_renew"` görünüyor — Apple süresi biten aboneliğe de bunu
# yazıyor. Yani bu bayrak TEK BAŞINA "kullanıcı iptal etti" DEMEK DEĞİLDİR.
# Kesin iptal = yenileme kapalı **VE erişim sürüyor** (`gives_access=True`).
_IPTAL_YENILEME = {"will_not_renew", "will_pause"}
# Fatura sorunu = kullanıcının iradesi değil; nedeni ayrı işaretlenir.
_FATURA_DURUM = {"in_billing_retry", "incomplete"}
# Erişimi bitmiş durumlar (sebep bilinmiyor: iptal mi, ödeme sorunu mu?).
_BITMIS_DURUM = {"expired", "unknown"}


def _uretim(items):
    """Yalnızca `environment="production"` abonelikler. Sandbox/test_store = TEST verisi."""
    return [s for s in items or [] if (s.get("environment") or "").lower() == "production"]


def _en_guncel(items):
    """v2 abonelik listesinden en ileri bitişli olanı seç (kullanıcının GÜNCEL durumu)."""
    from subscription.services.revenuecat import _parse_rc_ts

    en_iyi, en_iyi_bitis = None, None
    for sub in items or []:
        bitis = _parse_rc_ts(sub.get("ends_at") or sub.get("current_period_ends_at"))
        if en_iyi is None or (bitis and (en_iyi_bitis is None or bitis > en_iyi_bitis)):
            en_iyi, en_iyi_bitis = sub, bitis
    return en_iyi, en_iyi_bitis


def iptal_izi(sub, bitis, simdi):
    """
    v2 abonelik nesnesinden (iptal_tarihi, neden, donem_tipi) türetir; iptal yoksa None.

    ⚠️ **Tarih YAKLAŞIKTIR** ve bu kaçınılmazdır: v2 iptalin anını vermiyor
    (`unsubscribe_detected_at` yalnızca erişemediğimiz v1'de). Elimizdeki tek dürüst
    bilgi, iptalin içinde gerçekleştiği **pencere**dir → sınır olarak:
      · erişim bitmişse  → bitiş tarihi (iptal bundan ÖNCE olmuştur)
      · erişim sürüyorsa → dönem başlangıcı (iptal bundan SONRA olmuştur; bitiş
        gelecekte olduğu için onu "iptal tarihi" diye yazmak yanıltıcı olurdu)
    Bu satırlar `BACKFILL` işaretlenir, admin `≈` ile gösterir.

    Neden ayrımı:
      · UNSUBSCRIBE   → yenileme kapalı VE erişim sürüyor = KESİN kullanıcı iptali.
      · BILLING_ERROR → ödeme yeniden deneniyor / eksik.
      · ""            → süresi dolmuş; iptal mi ödeme sorunu mu BİLİNMİYOR (v2 söylemiyor).
        ⚠️ Burada "UNSUBSCRIBE" yazmak yanlış olurdu: prod'da süresi dolan her abonelik
        `will_not_renew` görünüyor.
    """
    from subscription.services.revenuecat import _parse_rc_ts

    yenileme = (sub.get("auto_renewal_status") or "").lower()
    durum = (sub.get("status") or "").lower()
    erisim = bool(sub.get("gives_access"))

    if yenileme in _IPTAL_YENILEME and erisim:
        neden = "UNSUBSCRIBE"
    elif durum in _FATURA_DURUM:
        neden = "BILLING_ERROR"
    elif durum in _BITMIS_DURUM or (bitis and bitis <= simdi and not erisim):
        neden = ""
    else:
        return None  # aktif ve yenilenecek → iptal yok

    if bitis and bitis <= simdi:
        iptal_at = bitis
    else:
        iptal_at = _parse_rc_ts(sub.get("current_period_starts_at")) or bitis
    if iptal_at is None:
        return None  # tarih yoksa uydurma → yazma
    donem = "TRIAL" if durum == "trialing" or sub.get("is_trial") else "NORMAL"
    return iptal_at, neden, donem


class Command(BaseCommand):
    help = "Geçmiş abonelik iptallerini RevenueCat v2'den doldurur (tek seferlik)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Yazma, yalnızca raporla.")
        parser.add_argument("--limit", type=int, default=0, help="En çok N kullanıcı işle.")
        parser.add_argument("--from-pk", type=int, default=0, help="Bu pk'dan itibaren başla.")
        parser.add_argument(
            "--probe",
            type=int,
            default=0,
            help="Aboneliği olan ilk N müşterinin HAM v2 JSON'unu bas ve çık (alan keşfi).",
        )
        parser.add_argument(
            "--include-sandbox",
            action="store_true",
            help="Sandbox/test_store aboneliklerini de say (vars. YALNIZCA production).",
        )
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

        from subscription.services.revenuecat import RevenueCatError, customer_subscriptions

        kuru = opts["dry_run"]
        probe = opts["probe"]
        qs = User.objects.filter(pk__gte=opts["from_pk"]).order_by("pk")
        if opts["limit"]:
            qs = qs[: opts["limit"]]

        toplam = musteri = iptal = yazildi = hata = sandbox = 0
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
            if not opts["include_sandbox"]:
                subs = _uretim(subs)
                if not subs:
                    sandbox += 1
                    continue  # yalnızca test verisi → admin'i sahte iptalle doldurma
            musteri += 1

            if probe:
                self.stdout.write(f"── uid={user.pk} {user.email}")
                self.stdout.write(json.dumps(subs, indent=2, ensure_ascii=False))
                if musteri >= probe:
                    self.stdout.write(self.style.SUCCESS("probe bitti."))
                    return
                continue

            sub, bitis = _en_guncel(subs)
            sonuc = iptal_izi(sub, bitis, simdi) if sub else None
            if not sonuc:
                continue
            iptal_at, neden, donem = sonuc
            iptal += 1

            if user.subscription_cancelled_at is not None and not opts["force"]:
                continue  # webhook zaten yazmış, ezme
            if user.subscription_last_event and user.subscription_last_event != "BACKFILL" and not opts["force"]:
                continue

            self.stdout.write(
                f"uid={user.pk} {user.email} → iptal≈{iptal_at:%Y-%m-%d} "
                f"neden={neden or '-'} donem={donem} "
                f"(yenileme={sub.get('auto_renewal_status')} durum={sub.get('status')})"
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
                f"bitti: taranan={toplam} rc_musterisi={musteri} sadece_sandbox={sandbox} "
                f"iptal_bulunan={iptal} yazilan={yazildi} hata={hata}" + (" (DRY RUN)" if kuru else "")
            )
        )
