"""
Mobil istemcide bütçe ↔ pencere sırası ve bütçe kategorileri.

⚠️ Arıza (bulundu 2026-10-25): bütçe `slot_al`'dan ÖNCE düşülüyordu → pencere dolu
olduğu için `MobilSlotError` alan tur EKAP'a hiç gitmeden günlük hakkı yakıyordu.
Doğrudan temin şeridi aynı pencereyi paylaşınca bu çakışma olağan hâle gelir.
"""
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from ekap.mobil import throttle
from ekap.mobil.client import EkapMobilClient, MobilButceError, MobilSlotError


def _yanit(kod=200, govde=None):
    r = MagicMock()
    r.status_code = kod
    r.cookies = {}
    r.json.return_value = govde if govde is not None else {"ok": True}
    r.text = "" if kod == 200 else "CAPTCHA_REQUIRED"
    return r


@override_settings(EKAP_MOBIL_GUNLUK_TAVAN=10, EKAP_MOBIL_DT_TAVAN=5,
                   EKAP_MOBIL_KULLANICI_REZERV=7)
class ButceSiraTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_slot_alinamazsa_BUTCE_DUSULMEZ(self):
        cli = EkapMobilClient()
        with patch.object(throttle, "slot_al", return_value=False):
            with self.assertRaises(MobilSlotError):
                cli._ham_istek("/x")
        self.assertEqual(throttle.butce_kalan("arka_plan"), 10)

    def test_butce_bittiyse_PENCERE_REZERVE_EDILMEZ(self):
        throttle.butce_harca("arka_plan", 10)
        cli = EkapMobilClient()
        with patch.object(throttle, "slot_al") as slot:
            with self.assertRaises(MobilButceError):
                cli._ham_istek("/x")
        slot.assert_not_called()

    def test_basarili_istek_bir_kez_duser(self):
        cli = EkapMobilClient()
        cli.session.post = MagicMock(return_value=_yanit())
        with patch.object(throttle, "slot_al", return_value=True):
            cli._ham_istek("/x")
        self.assertEqual(throttle.butce_kalan("arka_plan"), 9)

    def test_dt_kendi_butcesini_harcar_ama_ARKA_PLAN_penceresini_kullanir(self):
        cli = EkapMobilClient(butce="dt", slot_bekleme=90)
        cli.session.post = MagicMock(return_value=_yanit())
        with patch.object(throttle, "slot_al", return_value=True) as slot:
            cli._ham_istek("/x")
        self.assertEqual(slot.call_args.kwargs["ad"], "arka_plan")
        self.assertTrue(slot.call_args.kwargs["bekle"])
        self.assertEqual(slot.call_args.kwargs["azami_bekleme"], 90)
        self.assertEqual(throttle.butce_kalan("dt"), 4)
        self.assertEqual(throttle.butce_kalan("arka_plan"), 10)

    def test_kullanici_kendi_penceresini_kullanir(self):
        cli = EkapMobilClient(butce="kullanici")
        cli.session.post = MagicMock(return_value=_yanit())
        with patch.object(throttle, "slot_al", return_value=True) as slot:
            cli._ham_istek("/x")
        self.assertEqual(slot.call_args.kwargs["ad"], "kullanici")
        self.assertEqual(throttle.butce_kalan("kullanici"), 6)

    def test_api_version_kapatilabilir(self):
        cli = EkapMobilClient()
        cli.session.post = MagicMock(return_value=_yanit())
        with patch.object(throttle, "slot_al", return_value=True):
            cli._ham_istek("/x", params={"dtEncId": "abc"}, api_version=False)
        self.assertEqual(cli.session.post.call_args.kwargs["params"], {"dtEncId": "abc"})

    def test_butce_ozeti_dt_kategorisini_gosterir(self):
        throttle.butce_harca("dt", 2)
        self.assertEqual(throttle.butce_ozet()["dt"], {"kullanilan": 2, "tavan": 5})


class CaptchaSayaciTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_captcha_duvari_sayilir(self):
        cli = EkapMobilClient()
        with patch("ekap.mobil.client.captcha_mod.bekliyor_mu", return_value=True):
            with self.assertRaises(Exception):
                cli._captcha_asilirsa_tekrarla(_yanit(300), "/x")
        self.assertEqual(throttle.sayac_oku("captcha"), 1)
