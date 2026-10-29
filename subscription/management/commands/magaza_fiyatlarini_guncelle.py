"""
Pro paketlerinin mağaza fiyatlarını tazeler (günlük görevin elle tetiklenmesi).

    python manage.py magaza_fiyatlarini_guncelle           # oku + kaydet
    python manage.py magaza_fiyatlarini_guncelle --kuru    # oku + bas, KAYDETME
    python manage.py magaza_fiyatlarini_guncelle --durum   # saklanan kayıt + son deneme
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Pro paketlerinin App Store / Google Play Türkiye fiyatlarını günceller."

    def add_arguments(self, parser):
        parser.add_argument("--kuru", action="store_true", help="Kaydetmeden yalnızca bas.")
        parser.add_argument("--durum", action="store_true", help="Saklanan kaydı bas.")

    def handle(self, *args, **opts):
        from subscription.services import magaza_fiyat as mf

        def bas(veri):
            self.stdout.write(json.dumps(veri, ensure_ascii=False, indent=2))

        if opts["durum"]:
            bas({"kayit": mf.oku(), "son_deneme": mf.oku(mf.DURUM_ANAHTARI)})
            return
        if opts["kuru"]:
            try:
                bas(mf.topla())
            except mf.MagazaFiyatHatasi as e:
                self.stderr.write(self.style.ERROR(f"Hata: {e}"))
            return
        sonuc = mf.guncelle()
        stil = self.style.SUCCESS if sonuc["ok"] else self.style.ERROR
        self.stdout.write(stil(json.dumps(sonuc, ensure_ascii=False)))
