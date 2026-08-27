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
from django.utils import timezone
from datetime import timedelta

from ekap.models import Tender
from ekap.sync import _publish_date_from_ilanlar, detay_govdesi


class Command(BaseCommand):
    help = "ilan_tarihi NULL olan ihaleleri detail_raw'dan onarır."

    def add_arguments(self, parser):
        # ⚠️ Seçim `created_at` (DB'ye ne zaman girdi) üzerindendir, `detail_synced_at`
        # DEĞİL: backfill/refresh arşivin detayını sürekli tazelediği için
        # `detail_synced_at` neredeyse tüm tabloda "yakın" görünür ve filtre hiçbir
        # şey elemez (ölçüldü: `--gun 60` → 507.640 aday, yani tablonun tamamı).
        # Bildirimler için önemli olan **yeni eklenen** ihalelerdir.
        parser.add_argument("--gun", type=int, default=7,
                            help="son N günde DB'ye eklenmiş kayıtlar (vars. 7)")
        parser.add_argument("--tumu", action="store_true", help="tüm arşivi tara")
        parser.add_argument("--sayim", action="store_true",
                            help="önce toplam adayı say (BÜYÜK tabloda pahalı)")
        parser.add_argument("--limit", type=int, default=0, help="en fazla N satır")
        parser.add_argument("--saniye", type=int, default=600, help="süre bütçesi")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        basla = time.monotonic()
        # ⚠️ **`detail_raw={}` ile karşılaştırma YAPMAYIN.** JSONB eşitliği indekssizdir
        # ve `detail_raw` TOAST'tadır (~40 KB/satır) → planlayıcı 500k satırın tamamını
        # diskten açar. Ölçüldü: tek `.count()` **621 sn** sürüp süre bütçesini yedi ve
        # komut hiçbir satır işleyemeden bitti (`bakılan=0`). Detayın gelip gelmediğini
        # **indeksli** `detail_synced_at` söyler; gövde boşluğu satır satır kontrol edilir.
        qs = Tender.objects.filter(
            ilan_tarihi__isnull=True, detail_synced_at__isnull=False
        )
        if not o["tumu"]:
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=o["gun"]))
        # ⚠️ PK imleci: `ORDER BY <tarih> DESC LIMIT N` + seyrek filtre planlayıcıyı
        # tarih indeksini geriye taratmaya iter (bkz. CLAUDE.md plan tuzağı).
        qs = qs.only("pk", "detail_raw").order_by("pk")

        # ⚠️ `count()` bu tabloda pahalıdır ve süre bütçesinden yer. Varsayılan olarak
        # yapılmaz — komut zaten imleçle ilerleyip ne kadar işlediğini raporluyor.
        if o["sayim"]:
            self.stdout.write(f"onarılacak aday: {qs.count()}")
        self.stdout.write("onarım başlıyor" + ("  [DRY-RUN]" if o["dry_run"] else ""))

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
                # ⚠️ Sarmalı AÇ — `detail_raw` ham EKAP yanıtıdır (`{"item": {...}}`).
                # Açmadan `ilanList` aranınca komut her satırı işleyip hiçbirini
                # onaramıyordu (ölçüldü: bakılan=465, onarılan=0).
                ham = detay_govdesi(tender.detail_raw)
                if not ham:
                    continue
                try:
                    pub = _publish_date_from_ilanlar(ham, None)
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
