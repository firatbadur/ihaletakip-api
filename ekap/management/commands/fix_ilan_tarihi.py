"""
`ilan_tarihi` onarımı — liste upsert'inin sildiği yayın tarihlerini `detail_raw`'dan
geri doldurur.

⚠️ **Neden gerekli**: `upsert_tender_from_list` uzun süre `ilan_tarihi`'yi koşulsuz
yazıyordu; EKAP liste yanıtında `ilanTarihi` **%100 boş** geldiği için her liste turu
(`sync_recent` + `backfill`) detaydan gelmiş değeri **NULL'lıyordu**. Ölçüm
(2026-08-27): tek bir `sync_recent` turundan sonra 25 ve 26 Ağustos'un dolu kayıt
sayısı 157/213 → 0. Kök neden `sync._LISTE_EZMEZ` ile giderildi; bu komut **geçmiş
satırları** onarır.

⚠️ Sonuç bildirimlere kadar uzanır: filtre/idare/OKAS görevlerinin tamamı
`ilan_tarihi` üzerinden çalışır, alan NULL'ken hiçbiri eşleşme bulamaz. Onarım
yapılmazsa "dün yayımlanan" ihaleler bildirim penceresine hiç girmez.

Saf DB işidir (EKAP'a gitmez) ama `detail_raw` TOAST'ını okur → varsayılan olarak
**yakın tarihli** kayıtlarla sınırlıdır (`--gun`); tüm arşiv için `--tumu`.
"""
import time

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta

from ekap.models import Tender
from ekap.sync import _publish_date_from_ilanlar


class Command(BaseCommand):
    help = "ilan_tarihi NULL olan ihaleleri detail_raw'dan onarır."

    def add_arguments(self, parser):
        parser.add_argument("--gun", type=int, default=30,
                            help="son N günde detayı senkronlanmış kayıtlar (vars. 30)")
        parser.add_argument("--tumu", action="store_true", help="tüm arşivi tara")
        parser.add_argument("--limit", type=int, default=0, help="en fazla N satır")
        parser.add_argument("--saniye", type=int, default=600, help="süre bütçesi")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        basla = time.monotonic()
        qs = Tender.objects.filter(ilan_tarihi__isnull=True).exclude(
            Q(detail_raw__isnull=True) | Q(detail_raw={})
        )
        if not o["tumu"]:
            qs = qs.filter(detail_synced_at__gte=timezone.now() - timedelta(days=o["gun"]))
        # ⚠️ PK imleci: `ORDER BY <tarih> DESC LIMIT N` + seyrek filtre planlayıcıyı
        # tarih indeksini geriye taratmaya iter (bkz. CLAUDE.md plan tuzağı).
        qs = qs.only("pk", "detail_raw").order_by("pk")

        toplam = qs.count()
        self.stdout.write(f"onarılacak aday: {toplam}"
                          + ("  [DRY-RUN]" if o["dry_run"] else ""))

        son_pk = 0
        onarilan = bakilan = 0
        while True:
            if o["saniye"] and time.monotonic() - basla > o["saniye"]:
                self.stdout.write(self.style.WARNING("süre bütçesi doldu"))
                break
            parti = list(qs.filter(pk__gt=son_pk)[:500])
            if not parti:
                break
            guncel = []
            for tender in parti:
                son_pk = tender.pk  # imleç try'dan ÖNCE ilerler (bozuk satır kilitlemesin)
                bakilan += 1
                try:
                    pub = _publish_date_from_ilanlar(tender.detail_raw or {}, None)
                except Exception:
                    continue
                if pub:
                    tender.ilan_tarihi = pub
                    guncel.append(tender)
            if guncel and not o["dry_run"]:
                Tender.objects.bulk_update(guncel, ["ilan_tarihi"])
            onarilan += len(guncel)
            if o["limit"] and bakilan >= o["limit"]:
                break

        self.stdout.write(self.style.SUCCESS(
            f"✅ bakılan={bakilan} onarılan={onarilan} "
            f"({int(time.monotonic() - basla)} sn)"
        ))
