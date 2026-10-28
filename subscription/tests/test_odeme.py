"""
Web'den Pro satışı — iyzico Checkout Form (web 6. not #16-#19). iyzico mock'lanır.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from subscription.models import BillingProfile, Payment, ProGrant
from subscription.serializers import tckn_gecerli
from subscription.services import iyzico, odeme
from subscription.tasks import odeme_eposta_task, web_sure_hatirlat

PORTAL = "portal-sir"
AYAR = dict(WEB_SATIS_ENABLED=True, IYZICO_API_KEY="k", IYZICO_SECRET_KEY="s",
            PRO_YEARLY_PRICE="2400.00", PRO_VAT_RATE=20, PORTAL_KEY=PORTAL)
ONAY = {"consents": {"on_bilgilendirme": True, "mesafeli_satis": True,
                     "cayma_istisnasi": True}, "consent_version": "2026-10-28"}
GECERLI_TCKN = "10000000146"


def detay(odeme_obj, **kw):
    v = {"status": "success", "paymentStatus": "SUCCESS", "conversationId": str(odeme_obj.pk),
         "paidPrice": 2400.0, "price": 2400.0, "currency": "TRY", "token": odeme_obj.iyzico_token,
         "paymentId": "pay-1", "fraudStatus": 1, "lastFourDigits": "4242",
         "cardAssociation": "VISA", "itemTransactions": [{"paymentTransactionId": "tx-1"}]}
    v.update(kw)
    return v


@override_settings(**AYAR)
class OdemeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create(email="o@test.local", username="o-test")
        self.c = APIClient()
        self.c.force_authenticate(self.u)
        self.anon = APIClient()

    def _profil(self, **kw):
        v = dict(user=self.u, type="bireysel", full_name="Ayşe Yılmaz",
                 identity_number=GECERLI_TCKN, address="Moda Cad. 1", city="İstanbul",
                 phone="+905321234567")
        v.update(kw)
        return BillingProfile.objects.create(**v)

    def _checkout(self, token="tok-1"):
        if not BillingProfile.objects.filter(user=self.u).exists():
            self._profil()
        yanit = {"status": "success", "token": token, "paymentPageUrl": "https://iyzi/odeme"}
        with patch.object(iyzico, "checkout_baslat", return_value=yanit) as m:
            r = self.c.post("/api/v1/payments/checkout/", ONAY, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return Payment.objects.get(iyzico_token=token), m

    def _complete(self, odeme_obj, veri=None, token=None):
        with patch.object(iyzico, "checkout_detay",
                          return_value=veri if veri is not None else detay(odeme_obj)) as m:
            r = self.anon.post("/api/v1/payments/checkout/complete/",
                               {"token": token or odeme_obj.iyzico_token}, format="json",
                               HTTP_X_IT_PORTAL_KEY=PORTAL)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["data"], m

    # ── #16 fatura profili ────────────────────────────
    def test_tckn_algoritmasi(self):
        self.assertTrue(tckn_gecerli(GECERLI_TCKN))
        self.assertFalse(tckn_gecerli("10000000147"))
        self.assertFalse(tckn_gecerli("01234567890"))

    def test_profil_get_null_put_dogrulama(self):
        self.assertIsNone(self.c.get("/api/v1/auth/billing-profile/").json()["data"])
        r = self.c.put("/api/v1/auth/billing-profile/", {
            "type": "kurumsal", "full_name": "Ayşe Yılmaz", "address": "x", "city": "y",
            "phone": "0532 123 45 67"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("company_name", r.json()["errors"])
        r = self.c.put("/api/v1/auth/billing-profile/", {
            "type": "kurumsal", "full_name": "Ayşe Yılmaz", "company_name": "Örnek Ltd",
            "tax_office": "Kadıköy", "tax_number": "1234567890", "identity_number": GECERLI_TCKN,
            "address": "x", "city": "y", "phone": "0532 123 45 67"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        p = BillingProfile.objects.get(user=self.u)
        self.assertEqual(p.phone, "+905321234567")
        self.assertEqual(p.identity_number, "")  # kurumsalda TCKN tutulmaz

    # ── #17 checkout ──────────────────────────────────
    def test_plan_herkese_acik(self):
        d = self.anon.get("/api/v1/payments/plan/").json()["data"]
        self.assertEqual((d["amount"], d["months"], d["available"]), ("2400.00", 12, True))

    def test_onay_ve_profil_zorunlu(self):
        r = self.c.post("/api/v1/payments/checkout/",
                        {**ONAY, "consents": {**ONAY["consents"], "cayma_istisnasi": False}},
                        format="json")
        self.assertEqual(r.json()["errors"]["code"], "consents_required")
        r = self.c.post("/api/v1/payments/checkout/", ONAY, format="json")
        self.assertEqual(r.json()["errors"]["code"], "billing_profile_required")
        self.assertFalse(Payment.objects.exists())

    @override_settings(WEB_SATIS_ENABLED=False)
    def test_satis_kapali(self):
        self._profil()
        r = self.c.post("/api/v1/payments/checkout/", ONAY, format="json")
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (503, "sales_closed"))

    def test_checkout_govdesi(self):
        o, m = self._checkout()
        govde = m.call_args.args[0]
        self.assertEqual((govde["price"], govde["paidPrice"]), ("2400.00", "2400.00"))
        self.assertEqual(govde["enabledInstallments"], [1])
        self.assertEqual(govde["conversationId"], str(o.pk))
        self.assertEqual(govde["basketItems"][0]["itemType"], "VIRTUAL")
        self.assertEqual(o.consents["version"], "2026-10-28")
        self.assertEqual(o.amount, Decimal("2400.00"))

    # ── #17 complete ──────────────────────────────────
    def test_portal_anahtari_yoksa_403(self):
        r = self.anon.post("/api/v1/payments/checkout/complete/", {"token": "x"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_basarili_odeme_hak_acar_ve_idempotent(self):
        o, _ = self._checkout()
        d, _ = self._complete(o)
        self.assertEqual(d["status"], "success")
        self.u.refresh_from_db()
        self.assertTrue(self.u.is_premium)
        g = ProGrant.objects.get(payment=o)
        self.assertAlmostEqual((g.ends_at - g.starts_at).days, 365, delta=1)
        o.refresh_from_db()
        self.assertEqual((o.card_last4, o.iyzico_transaction_id), ("4242", "tx-1"))
        d2, m2 = self._complete(o)
        self.assertEqual(d2, d)
        m2.assert_not_called()  # sonuçlanmış ödeme iyzico'ya tekrar sorulmaz
        self.assertEqual(ProGrant.objects.filter(user=self.u).count(), 1)

    def test_tutar_uyusmazsa_hak_ACILMAZ(self):
        o, _ = self._checkout()
        d, _ = self._complete(o, detay(o, paidPrice=1.0))
        self.assertEqual(d["status"], "failure")
        self.assertFalse(ProGrant.objects.exists())

    def test_ikinci_yil_ustune_eklenir(self):
        o1, _ = self._checkout("tok-1")
        self._complete(o1)
        o2, _ = self._checkout("tok-2")
        self._complete(o2)
        g1, g2 = ProGrant.objects.get(payment=o1), ProGrant.objects.get(payment=o2)
        self.assertEqual(g2.starts_at, g1.ends_at)
        self.u.refresh_from_db()
        self.assertEqual(self.u.subscription_expires_at, g2.ends_at)

    def test_bilinmeyen_token_failure(self):
        r = self.anon.post("/api/v1/payments/checkout/complete/", {"token": "yok"},
                           format="json", HTTP_X_IT_PORTAL_KEY=PORTAL)
        self.assertEqual(r.json()["data"]["status"], "failure")

    def test_karar_yoksa_pending_sonra_SUCCESS_canlanir(self):
        o, _ = self._checkout()
        d, _ = self._complete(o, {"status": "success", "paymentStatus": "INIT_THREEDS",
                                  "conversationId": str(o.pk)})
        self.assertEqual(d["status"], "pending")
        d, _ = self._complete(o, detay(o, paymentStatus="FAILURE", errorMessage="Yetersiz bakiye"))
        self.assertEqual((d["status"], d["message"]), ("failure", "Yetersiz bakiye"))
        # iyzico sonradan SUCCESS derse (para çekilmiş) failure terminal değildir.
        d, _ = self._complete(o)
        self.assertEqual(d["status"], "success")

    # ── #18 mutabakat ─────────────────────────────────
    def test_mutabakat_genc_kaydi_bozmaz_eskiyi_kapatir(self):
        o, _ = self._checkout()
        Payment.objects.filter(pk=o.pk).update(created_at=timezone.now() - timedelta(minutes=20))
        with patch.object(iyzico, "checkout_detay", return_value={"status": "failure"}):
            self.assertEqual(odeme.mutabakat()["pending"], 1)
        Payment.objects.filter(pk=o.pk).update(created_at=timezone.now() - timedelta(hours=2))
        with patch.object(iyzico, "checkout_detay", return_value={"status": "failure"}):
            self.assertEqual(odeme.mutabakat()["failure"], 1)

    def test_mutabakat_donusu_gelmeyen_odemeyi_acar(self):
        o, _ = self._checkout()
        Payment.objects.filter(pk=o.pk).update(created_at=timezone.now() - timedelta(minutes=20))
        with patch.object(iyzico, "checkout_detay", return_value=detay(o)):
            self.assertEqual(odeme.mutabakat()["success"], 1)
        self.assertTrue(ProGrant.objects.filter(payment=o).exists())

    # ── #19 fesih / iade / e-posta ────────────────────
    def test_fesih_iade_yok_pro_surer(self):
        o, _ = self._checkout()
        self._complete(o)
        r = self.c.post(f"/api/v1/payments/{o.pk}/cancel-request/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["data"]["refund"])
        self.u.refresh_from_db()
        self.assertTrue(self.u.is_premium)
        diger = User.objects.create(email="x@test.local", username="x-test")
        self.c.force_authenticate(diger)
        self.assertEqual(self.c.post(f"/api/v1/payments/{o.pk}/cancel-request/").status_code, 404)

    def test_iade_hakki_kapatir(self):
        o, _ = self._checkout()
        self._complete(o)
        o.refresh_from_db()
        with patch.object(iyzico, "iade", return_value={"status": "success"}) as m:
            odeme.iade_et(o)
        self.assertEqual(m.call_args.args[0], "tx-1")
        o.refresh_from_db()
        self.assertEqual(o.status, Payment.Status.REFUNDED)
        self.u.refresh_from_db()
        self.assertFalse(self.u.is_premium)

    def test_odeme_alindi_epostasi_ekli(self):
        o, _ = self._checkout()
        self._complete(o)
        odeme_eposta_task.apply(args=[o.pk, "alindi"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(mail.outbox[0].attachments), 2)
        self.assertNotIn(GECERLI_TCKN, mail.outbox[0].body)

    def test_odeme_listesi_ve_fatura_hazir_degil(self):
        o, _ = self._checkout()
        self._complete(o)
        liste = self.c.get("/api/v1/payments/").json()["data"]
        self.assertEqual([x["id"] for x in liste], [o.pk])
        self.assertFalse(liste[0]["invoice_available"])
        r = self.c.get(f"/api/v1/payments/{o.pk}/invoice/")
        self.assertEqual((r.status_code, r.json()["errors"]["code"]), (404, "invoice_not_ready"))

    # ── #18.2 süre hatırlatması ───────────────────────
    def _web_hak(self, gun):
        g = ProGrant.objects.create(user=self.u, source="iyzico",
                                    starts_at=timezone.now() - timedelta(days=300),
                                    ends_at=timezone.now() + timedelta(days=gun, hours=1))
        from subscription.services.pro_hak import recompute_pro
        recompute_pro(self.u)
        return g

    def test_hatirlatma_esik_tek_sefer(self):
        g = self._web_hak(7)
        web_sure_hatirlat()
        web_sure_hatirlat()
        self.assertEqual(len(mail.outbox), 1)
        g.refresh_from_db()
        self.assertEqual(sorted(g.hatirlatmalar), ["30", "7"])

    def test_magaza_aktifse_hatirlatma_yok(self):
        self._web_hak(7)
        ProGrant.objects.create(user=self.u, source="store",
                                starts_at=timezone.now() - timedelta(days=1),
                                ends_at=timezone.now() + timedelta(days=3))
        web_sure_hatirlat()
        self.assertEqual(len(mail.outbox), 0)


class IyzicoImzaTests(TestCase):
    def test_fiyat_bicimi(self):
        self.assertEqual(iyzico.fiyat("2400"), "2400.00")
        self.assertEqual(iyzico.fiyat(Decimal("99.5")), "99.50")

    def test_yetkilendirme_basligi(self):
        import base64

        b = iyzico.yetkilendirme("api", "sir", "/yol", "{}", "rnd")
        ham = base64.b64decode(b.removeprefix("IYZWSv2 ")).decode()
        self.assertTrue(ham.startswith("apiKey:api&randomKey:rnd&signature:"))
