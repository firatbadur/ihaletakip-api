"""
Kaynağa duyarlı Pro hakkı (web 6. not #15).

Kilitlenen hata: RevenueCat'te hakkı olmayan kullanıcı (web'den ödeyen / admin'den
Pro verilen) mobil uygulama her açılışta `verify` ile Free'ye çekiliyordu.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from accounts.serializers import UserSerializer
from core.dashboard import pro_q
from subscription.models import ProGrant
from subscription.services import revenuecat
from subscription.services.pro_hak import etkin_hak, recompute_pro


class ProHakTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.u = User.objects.create(email="p@test.local", username="p-test")

    def _hak(self, source, bas_gun=-10, bit_gun=30, **kw):
        return ProGrant.objects.create(
            user=self.u, source=source,
            starts_at=self.now + timedelta(days=bas_gun),
            ends_at=None if bit_gun is None else self.now + timedelta(days=bit_gun), **kw)

    def _verify(self, is_pro, expiry=None):
        with patch.object(revenuecat, "resolve_pro_status", return_value=(is_pro, expiry)):
            revenuecat.sync_user_subscription(self.u)
        self.u.refresh_from_db()

    def test_RC_yoksa_web_hakki_SILINMEZ(self):
        self._hak(ProGrant.Source.IYZICO, bit_gun=365)
        recompute_pro(self.u)
        self._verify(False)
        self.assertTrue(self.u.is_premium)
        self.assertEqual(UserSerializer(self.u).data["subscription_source"], "iyzico")

    def test_RC_yoksa_admin_hakki_SILINMEZ(self):
        self._hak(ProGrant.Source.ADMIN, bit_gun=None)
        recompute_pro(self.u)
        self._verify(False)
        self.assertTrue(self.u.is_premium)
        self.assertIsNone(self.u.subscription_expires_at)

    def test_magaza_aktif_sonra_bitince_free(self):
        self._verify(True, self.now + timedelta(days=20))
        self.assertTrue(self.u.is_premium)
        self.assertEqual(ProGrant.objects.filter(user=self.u, source="store").count(), 1)
        self._verify(False)
        self.assertFalse(self.u.is_premium)
        self.assertEqual(self.u.subscription_tier, User.Tier.FREE)
        # Satır silinmez, kapanır; tekrar aktifleşince yine TEK satır.
        self._verify(True, self.now + timedelta(days=30))
        self.assertEqual(ProGrant.objects.filter(user=self.u, source="store").count(), 1)
        self.assertTrue(self.u.is_premium)

    def test_iki_kaynakta_gec_olan_bitis(self):
        self._hak(ProGrant.Source.IYZICO, bit_gun=200)
        self._verify(True, self.now + timedelta(days=20))
        self.assertEqual(self.u.subscription_expires_at.date(),
                         (self.now + timedelta(days=200)).date())
        # Mağaza biter → web sürer.
        self._verify(False)
        self.assertTrue(self.u.is_premium)

    def test_zincir_ust_uste_eklenen_yil(self):
        ilk = self._hak(ProGrant.Source.IYZICO, bas_gun=-300, bit_gun=65)
        ProGrant.objects.create(user=self.u, source="iyzico", starts_at=ilk.ends_at,
                                ends_at=ilk.ends_at + timedelta(days=365))
        recompute_pro(self.u)
        self.assertEqual(self.u.subscription_expires_at, ilk.ends_at + timedelta(days=365))

    def test_iptal_edilen_hak_sayilmaz(self):
        self._hak(ProGrant.Source.IYZICO, revoked_at=self.now)
        recompute_pro(self.u)
        self.assertFalse(self.u.is_premium)
        self.assertIsNone(etkin_hak(self.u).source)

    def test_pro_q_esdegerligi(self):
        self._hak(ProGrant.Source.IYZICO)
        recompute_pro(self.u)
        self.assertEqual(User.objects.filter(pro_q(), pk=self.u.pk).exists(),
                         self.u.is_premium)


class AdminEkranTests(TestCase):
    def test_kullanici_ve_odeme_ekranlari_acilir(self):
        from django.test import Client

        from subscription.models import Payment

        su = User.objects.create_superuser("su", "su@test.local", "x")
        u = User.objects.create(email="a@test.local", username="a-test")
        ProGrant.objects.create(user=u, source="admin", starts_at=timezone.now())
        o = Payment.objects.create(user=u, plan_code="pro_yearly", amount="1.00", vat_rate=20)
        c = Client()
        c.force_login(su)
        for yol in (f"/admin/accounts/user/{u.pk}/change/",
                    f"/admin/subscription/payment/{o.pk}/change/",
                    "/admin/subscription/payment/", "/admin/subscription/progrant/"):
            self.assertEqual(c.get(yol).status_code, 200, yol)
