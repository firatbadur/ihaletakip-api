"""
0002 göçünün `admin` saydığı Pro'ları RevenueCat'e sorar (web 6. not #15.4).

Göç RC'ye gidemediği için "RC izi yok → elle verilmiş" varsaymıştı. Bu komut o
satırların her biri için RC'de aktif `pro` entitlement'ı var mı bakar; varsa satırı
`store`'a çevirir (kullanıcının mağaza satırı yoksa) ve etkin katmanı yeniden hesaplar.
Varsayılan KURU çalışır; `--uygula` ile yazar.

    python manage.py pro_hak_gocu_dogrula [--uygula]
"""
from django.core.management.base import BaseCommand



class Command(BaseCommand):
    help = "0002 göçünde admin sayılan Pro'ları RevenueCat ile doğrular."

    def add_arguments(self, parser):
        parser.add_argument("--uygula", action="store_true", help="Değişikliği yaz.")

    def handle(self, *args, **opts):
        import importlib

        from subscription.models import ProGrant
        from subscription.services.pro_hak import recompute_pro
        from subscription.services.revenuecat import RevenueCatError, resolve_pro_status

        goc = importlib.import_module("subscription.migrations.0002_pro_hak_gocu")
        satirlar = ProGrant.objects.filter(
            source=ProGrant.Source.ADMIN, note=goc.GOC_NOTU, revoked_at__isnull=True
        ).select_related("user")
        sayac = {"admin_kaldi": 0, "store_yapildi": 0, "hata": 0}
        for g in satirlar:
            try:
                pro, bitis = resolve_pro_status(str(g.user_id))
            except RevenueCatError as e:
                sayac["hata"] += 1
                self.stderr.write(f"uid={g.user_id}: RC hatası {e}")
                continue
            if not pro:
                sayac["admin_kaldi"] += 1
                continue
            self.stdout.write(f"uid={g.user_id} {g.user.email}: RC'de aktif (bitiş {bitis})")
            sayac["store_yapildi"] += 1
            if opts["uygula"] and not ProGrant.objects.filter(
                user_id=g.user_id, source=ProGrant.Source.STORE
            ).exists():
                g.source = ProGrant.Source.STORE
                g.ends_at = bitis
                g.note = ""
                g.save(update_fields=["source", "ends_at", "note", "updated_at"])
                recompute_pro(g.user)
        kip = "UYGULANDI" if opts["uygula"] else "KURU (yazılmadı)"
        self.stdout.write(f"{kip}: {sayac}")
