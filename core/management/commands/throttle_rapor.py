"""Hız sınırı gözlem raporu — `uygula`ya geçmeden önce bakılacak ölçüm.

    python manage.py throttle_rapor            # bugün + son 6 gün
    python manage.py throttle_rapor --gun 2

Satır = (olay, kapsam, istemci türü): "uygula modunda olsaydı reddedilecek" istek
sayısı ve bunu yapan TEKİL kimlik sayısı. Olaylar: `hiz` (oran aşımı), `derinlik`
(sayfa derinliği), `belge_anonim` (girişsiz belge erişimi — `EKAP_BELGE_GIRIS_ZORUNLU`
açılmadan önce sıfıra inmeli).
⚠️ `dogrudan-*` satırları mobil uygulama + doğrudan API'dir; ikisi ayırt edilemez.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.throttling import gozlem_ozeti


class Command(BaseCommand):
    help = "Hız sınırı gözlem sayaçlarını gösterir."

    def add_arguments(self, parser):
        parser.add_argument("--gun", type=int, default=7)

    def handle(self, *args, gun, **opts):
        bugun = timezone.localdate()
        for i in range(gun):
            g = (bugun - timedelta(days=i)).isoformat()
            ozet = gozlem_ozeti(g)
            self.stdout.write(self.style.MIGRATE_HEADING(f"── {g}"))
            if not ozet:
                self.stdout.write("  (kayıt yok)")
                continue
            for (olay, kapsam, tur), (n, tekil) in sorted(ozet.items()):
                self.stdout.write(f"  {olay:<13} {kapsam:<12} {tur:<17} "
                                  f"istek={n:<7} tekil={tekil}")
