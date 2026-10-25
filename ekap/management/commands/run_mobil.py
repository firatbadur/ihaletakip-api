"""
EKAP Mobil toplama görevlerini **senkron** tetikler (Celery/beat beklemeden).

    manage.py run_mobil --is tik
    manage.py run_mobil --is kesif [--max-istek N]
    manage.py run_mobil --is detay --ikn 2026/1690784
    manage.py run_mobil --is sonuc --ikn 2024/1362677
    manage.py run_mobil --is durum
    manage.py run_mobil --is dt          # DT yeni ilan yoklaması (1-2 istek)
    manage.py run_mobil --is dt_tam      # DT tam açık taraması, bitene kadar

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
                            choices=["tik", "kesif", "detay", "sonuc", "durum", "durum_tara",
                                     "dt", "dt_tam"])
        parser.add_argument("--ikn")
        parser.add_argument("--max-istek", type=int, default=200)

    def handle(self, *args, **o):
        if o["is_"] == "durum":
            return self._durum()
        if o["is_"] in ("detay", "sonuc") and not o.get("ikn"):
            raise CommandError("--ikn zorunlu")

        if o["is_"] == "durum_tara":
            from ekap.mobil import durum as durum_mod
            from ekap.mobil.client import EkapMobilClient, MobilSlotError
            import time

            cli = EkapMobilClient()
            for _ in range(o["max_istek"]):
                try:
                    sonuc = durum_mod.adim(cli)
                except MobilSlotError:
                    time.sleep(30)
                    continue
                self.stdout.write(str(sonuc))
                if sonuc.get("atlandi") or not sonuc.get("kalan_dilim"):
                    break
            return
        if o["is_"] in ("dt", "dt_tam"):
            return self._dt(o["is_"], o["max_istek"])
        if o["is_"] == "tik":
            sonuc = mobil_tasks.tik()
        elif o["is_"] == "kesif":
            sonuc = mobil_tasks.kesif_turu(max_istek=o["max_istek"])
        elif o["is_"] == "detay":
            sonuc = mobil_tasks.detay(o["ikn"])
        else:
            sonuc = mobil_tasks.sonuc(o["ikn"])
        self.stdout.write(str(sonuc))

    def _dt(self, is_, max_istek):
        """
        ⚠️ Kill switch'e bakmaz (elle teşhis). Bütçe `dt` kategorisinden düşer,
        pencere arka planla ortaktır → tik çalışırken de güvenle koşturulabilir.
        """
        from ekap.mobil import dt as dt_mod

        cli = mobil_tasks._dt_cli()
        if is_ == "dt":
            sonuc = dt_mod.yoklama(cli)
            mobil_tasks._say("dt", sonuc["istek"])
            sonuc["yeni"] = len(sonuc["yeni"])
            self.stdout.write(str(sonuc))
            return
        # Yarım tur varsa ondan devam eder; yoksa aralığa bakmadan yenisini başlatır.
        from ekap.models import SyncCheckpoint
        cp, _ = SyncCheckpoint.objects.get_or_create(name=dt_mod.CHECKPOINT)
        if not (cp.extra or {}).get("tam"):
            cp.extra = {**(cp.extra or {}), "son_tam_bit": None}
            cp.save(update_fields=["extra", "updated_at"])
        for _ in range(max_istek):
            sonuc = dt_mod.tam_tarama_adimi(cli)
            if sonuc is None:
                break
            mobil_tasks._say("dt", 1)
            self.stdout.write(
                f"sayfa {sonuc['sayfa']}: {sonuc['satir']} satır, {len(sonuc['yeni'])} yeni"
            )
            if sonuc.get("tamamlandi") or sonuc.get("kesildi"):
                break

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
        dcp = SyncCheckpoint.objects.filter(name="mobil_durum").first()
        dex = (dcp.extra or {}) if dcp else {}
        self.stdout.write(self.style.MIGRATE_HEADING("Durum taraması"))
        self.stdout.write(f"  bekleyen dilim : {len(dex.get('yigin') or [])}")
        self.stdout.write(f"  son yakın/uzak : {dex.get('son_yakin')} / {dex.get('son_uzak')}")
        self.stdout.write(self.style.MIGRATE_HEADING("Keşif"))
        self.stdout.write(f"  bekleyen dilim : {len(yigin or [])}")
        bolumleme = (cp.extra or {}).get("son_bolumleme") if cp else None
        self.stdout.write(f"  son bölümleme  : {len(bolumleme or [])} yaprak")
        self.stdout.write(f"  son tur        : {(cp.extra or {}).get('son_tur') if cp else '—'}")

        from ekap.mobil import dt as dt_mod
        from ekap.models import DogrudanTemin
        dtcp = SyncCheckpoint.objects.filter(name=dt_mod.CHECKPOINT).first()
        dtex = (dtcp.extra or {}) if dtcp else {}
        self.stdout.write(self.style.MIGRATE_HEADING("Doğrudan temin"))
        self.stdout.write(f"  açık / toplam  : "
                          f"{DogrudanTemin.objects.filter(dt_mod.acik_q()).count()} / "
                          f"{DogrudanTemin.objects.count()}")
        self.stdout.write(f"  son yoklama    : {dtex.get('son_yoklama') or '—'}")
        self.stdout.write(f"  son tam tarama : {dtex.get('son_tam_bit') or '—'} "
                          f"({dtex.get('son_tam_sayfa') or '—'} sayfa)"
                          + (f" · sürüyor: sayfa {dtex['tam']['sayfa']}" if dtex.get("tam") else ""))

        self.stdout.write(self.style.MIGRATE_HEADING("CAPTCHA"))
        self.stdout.write(f"  geri çekilme   : {'EVET' if captcha_mod.bekliyor_mu() else 'hayır'}")
        self.stdout.write(f"  bekleyen       : {'VAR' if captcha_mod.bekleyen_oku() else 'yok'}")
        self.stdout.write(f"  durum          : {captcha_mod.bekleyen_durum() or '—'}")
