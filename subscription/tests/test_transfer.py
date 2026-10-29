"""RevenueCat TRANSFER olayı — kimlikler yalnızca `transferred_from/to`'da gelir.

Senaryo: web'den gelen kullanıcı ödeme ekranında girişten ÖNCE satın alırsa satın alma
anonim RC kimliğindedir; giriş (`Purchases.logIn`) onu hesaba aktarır. Olay
`app_user_id` taşımadığı için eskiden "kullanıcı eşleşmedi" diye düşüyordu.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from accounts.models import User
from subscription.services.revenuecat import apply_event_fallback, resolve_user_from_event

WEBHOOK = "/api/v1/subscription/revenuecat-webhook/"


@override_settings(REVENUECAT_WEBHOOK_AUTH="rc-sir")
class TransferTests(TestCase):
    def setUp(self):
        self.yeni = User.objects.create(email="y@test.local", username="y")
        self.eski = User.objects.create(email="e@test.local", username="e",
                                        subscription_tier=User.Tier.PRO)

    def _olay(self, kaynak, hedef):
        return {"type": "TRANSFER", "transferred_from": kaynak, "transferred_to": hedef,
                "entitlement_ids": ["pro"]}

    def _gonder(self, olay):
        with patch("subscription.tasks.sync_subscription_task.delay") as gorev:
            r = self.client.post(WEBHOOK, {"event": olay}, content_type="application/json",
                                 HTTP_AUTHORIZATION="rc-sir")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["data"], gorev

    def test_anonimden_hesaba_aktarim_eslesir(self):
        olay = self._olay(["$RCAnonymousID:abc"], [str(self.yeni.pk)])
        self.assertEqual(resolve_user_from_event(olay), self.yeni)
        veri, gorev = self._gonder(olay)
        self.assertTrue(veri["handled"])
        gorev.assert_called_once()
        self.assertEqual(gorev.call_args.args, (self.yeni.pk,))

    def test_kaybeden_hesap_da_senkronlanir_yedeksiz(self):
        _, gorev = self._gonder(self._olay([str(self.eski.pk)], [str(self.yeni.pk)]))
        cagrilar = {c.args[0]: c.kwargs for c in gorev.call_args_list}
        self.assertEqual(set(cagrilar), {self.yeni.pk, self.eski.pk})
        # Kaybedene event yedeği verilmez: RC'ye ulaşılamazsa ona Pro yazılmasın.
        self.assertNotIn("event", cagrilar[self.eski.pk])

    def test_kazanan_anonimse_kaybeden_yine_senkronlanir(self):
        veri, gorev = self._gonder(self._olay([str(self.eski.pk)], ["$RCAnonymousID:x"]))
        self.assertFalse(veri["handled"])
        self.assertEqual([c.args for c in gorev.call_args_list], [(self.eski.pk,)])

    def test_tarihsiz_transfer_yedegi_suresiz_pro_yazmaz(self):
        apply_event_fallback(self.yeni, self._olay([], [str(self.yeni.pk)]))
        self.yeni.refresh_from_db()
        self.assertEqual(self.yeni.subscription_tier, User.Tier.FREE)
