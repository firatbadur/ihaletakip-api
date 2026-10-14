"""
`tenders/0014`: favori idare yeniden Pro'ya özel → Pro OLMAYANLARIN kayıtları silinir.

"Pro" tanımı migration'da `User.is_premium`'un kopyası olduğu için ikisinin aynı
sonucu verdiği burada kilitlenir (superuser · süresiz Pro · süresi geçmiş Pro · Free).
"""
import importlib
from datetime import timedelta

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from tenders.models import FavoriteAuthority

goc = importlib.import_module("tenders.migrations.0014_favori_idare_free_temizle")


class FavoriIdareTemizligiTest(TestCase):
    def test_yalniz_pro_olmayanlarin_kayitlari_silinir(self):
        simdi = timezone.now()
        kullanicilar = {
            "free": User.objects.create(email="f@x.com", username="f"),
            "pro": User.objects.create(email="p@x.com", username="p", subscription_tier="pro"),
            "pro_sureli": User.objects.create(
                email="ps@x.com", username="ps", subscription_tier="pro",
                subscription_expires_at=simdi + timedelta(days=3)),
            "pro_bitmis": User.objects.create(
                email="pb@x.com", username="pb", subscription_tier="pro",
                subscription_expires_at=simdi - timedelta(days=1)),
            "admin": User.objects.create(email="a@x.com", username="a", is_superuser=True),
        }
        for i, u in enumerate(kullanicilar.values()):
            FavoriteAuthority.objects.create(user=u, detsis_no=str(100 + i), alarm=True)

        goc.free_favorileri_sil(apps, None)

        kalan = set(FavoriteAuthority.objects.values_list("user__username", flat=True))
        self.assertEqual(kalan, {"p", "ps", "a"})
        # Migration'daki tanım property ile aynı sonucu vermeli.
        for u in kullanicilar.values():
            self.assertEqual(u.username in kalan, u.is_premium, u.username)
