"""
EKAP Mobil toplama görevlerini **senkron** tetikler (Celery/beat beklemeden).

    manage.py run_mobil --is tik
    manage.py run_mobil --is kesif [--max-istek N]
    manage.py run_mobil --is detay --ikn 2026/1690784
    manage.py run_mobil --is sonuc --ikn 2024/1362677
    manage.py run_mobil --is durum

⚠️ `EKAP_MOBIL_ENABLED=False` iken `tik` bedavaya çıkar; `detay`/`sonuc`/`kesif`
doğrudan çalışır (kill switch yalnızca otomatik döngüyü kapatır, elle teşhisi değil).
"""
from django.core.management.base import BaseCommand, CommandError

from ekap.mobil import captcha as captcha_mod
from ekap.mobil import tasks as mobil_tasks
from ekap.mobil import throttle


class Command(BaseCommand):
    help = "EKAP mobil toplama görevlerini senkron çalıştırır"

    def add_arguments(self, parser):
        parser.add_argument("--is", dest="is_", required=True,
                            choices=["tik", "kesif", "detay", "sonuc", "durum"])
        parser.add_argument("--ikn")
        parser.add_argument("--max-istek", type=int, default=200)

    def handle(self, *args, **o):
        if o["is_"] == "durum":
            return self._durum()
        if o["is_"] in ("detay", "sonuc") and not o.get("ikn"):
            raise CommandError("--ikn zorunlu")

        if o["is_"] == "tik":
            sonuc = mobil_tasks.tik()
        elif o["is_"] == "kesif":
            sonuc = mobil_tasks.kesif_turu(max_istek=o["max_istek"])
        elif o["is_"] == "detay":
            sonuc = mobil_tasks.detay(o["ikn"])
        else:
            sonuc = mobil_tasks.sonuc(o["ikn"])
        self.stdout.write(str(sonuc))

    def _durum(self):
        from ekap.models import SyncCheckpoint

        self.stdout.write(self.style.MIGRATE_HEADING("Bugünkü iş sayıları"))
        for ad, n in mobil_tasks.sayaclar().items():
            self.stdout.write(f"  {ad:10} {n}")

        self.stdout.write(self.style.MIGRATE_HEADING("Bütçe"))
        for ad, v in throttle.butce_ozet().items():
            self.stdout.write(f"  {ad:10} {v['kullanilan']}/{v['tavan']}")

        cp = SyncCheckpoint.objects.filter(name=mobil_tasks.CHECKPOINT).first()
        yigin = (cp.extra or {}).get("yigin") if cp else None
        self.stdout.write(self.style.MIGRATE_HEADING("Keşif"))
        self.stdout.write(f"  bekleyen dilim : {len(yigin or [])}")
        self.stdout.write(f"  son tur        : {(cp.extra or {}).get('son_tur') if cp else '—'}")

        self.stdout.write(self.style.MIGRATE_HEADING("CAPTCHA"))
        self.stdout.write(f"  geri çekilme   : {'EVET' if captcha_mod.bekliyor_mu() else 'hayır'}")
        self.stdout.write(f"  bekleyen       : {'VAR' if captcha_mod.bekleyen_oku() else 'yok'}")
        self.stdout.write(f"  durum          : {captcha_mod.bekleyen_durum() or '—'}")
