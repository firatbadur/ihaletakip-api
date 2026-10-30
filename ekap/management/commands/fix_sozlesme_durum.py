"""
Sözleşmesi olduğu hâlde durumu boş / 2 / 3 / 4 kalan ihaleleri 5'e (Sözleşme
İmzalanmış) çeker — `sync.sozlesme_durumunu_duzelt`'in geçmiş satırlar için toplu hâli.

    python manage.py fix_sozlesme_durum [--dry-run]

⚠️ Alarm fırtınası koruması (`mobil/durum.py` ile aynı kural): teklif tarihi
`EKAP_MOBIL_DURUM_ALARM_SESSIZ_GUN` (14) günden eski ihalelerin "İhale Sonuçlandı"
alarmı sessizce işaretlenir — aylar önce imzalanmış sözleşme bugün haber değildir.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from ekap.mobil.constants import DURUM_ACIKLAMA
from ekap.models import Contract, Tender
from ekap.sync import DURUM_SOZLESME_IMZALANMIS, SOZLESME_ONCESI_DURUMLAR


class Command(BaseCommand):
    help = "Sözleşmesi olup durumu 2/3/4/boş kalan ihaleleri 'Sözleşme İmzalanmış' yapar."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        qs = Tender.objects.filter(
            Q(ihale_durum__isnull=True) | Q(ihale_durum__in=SOZLESME_ONCESI_DURUMLAR),
            Exists(Contract.objects.filter(tender_id=OuterRef("pk"))),
        )
        satirlar = list(qs.values_list("pk", "ikn", "ekap_id", "ihale_tarihi", "ihale_durum"))
        dagilim = {}
        for s in satirlar:
            dagilim[s[4]] = dagilim.get(s[4], 0) + 1
        self.stdout.write(f"aday: {len(satirlar)}  durum dağılımı: {dagilim}")
        if opts["dry_run"] or not satirlar:
            return

        from tenders.models import TenderAlarm

        esik = timezone.now() - timedelta(
            days=getattr(settings, "EKAP_MOBIL_DURUM_ALARM_SESSIZ_GUN", 14))
        eski = [s for s in satirlar if s[3] and s[3] < esik]
        sessiz = 0
        for i in range(0, len(eski), 1000):
            dilim = eski[i:i + 1000]
            sessiz += TenderAlarm.objects.filter(
                Q(tender_id__in=[s[2] for s in dilim]) | Q(tender_ikn__in=[s[1] for s in dilim]),
                completed_notified=False,
            ).update(completed_notified=True, last_ihale_durum=DURUM_SOZLESME_IMZALANMIS)

        degisen = 0
        pkler = [s[0] for s in satirlar]
        for i in range(0, len(pkler), 1000):
            # Koşul yeniden uygulanır: aradaki sürede başka yol durumu ilerletmiş olabilir.
            degisen += qs.filter(pk__in=pkler[i:i + 1000]).update(
                ihale_durum=DURUM_SOZLESME_IMZALANMIS,
                ihale_durum_aciklama=DURUM_ACIKLAMA[DURUM_SOZLESME_IMZALANMIS],
                updated_at=timezone.now(),
            )
        self.stdout.write(f"değişen: {degisen}  sessize alınan alarm: {sessiz}")
