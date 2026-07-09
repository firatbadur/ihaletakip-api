"""81 ili (filterData.js CITIES) City tablosuna yükler."""
from django.core.management.base import BaseCommand

from ekap.constants import CITIES
from ekap.models import City


class Command(BaseCommand):
    help = "İl (City) lookup tablosunu seed eder."

    def handle(self, *args, **options):
        count = 0
        for ekap_il_id, plaka, ad, is_big in CITIES:
            City.objects.update_or_create(
                ekap_il_id=ekap_il_id,
                defaults={"plaka": plaka, "ad": ad, "is_big_city": is_big},
            )
            count += 1
        self.stdout.write(self.style.SUCCESS(f"✅ {count} il yüklendi."))
