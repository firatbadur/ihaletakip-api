"""Hoş geldin e-postası: yeni kullanıcıya bir kez, kayıt yolundan bağımsız (`post_save`)."""
import json
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

from accounts.models import EpostaEngeli, User
from accounts.services import hosgeldin
from accounts.services.eposta_engel import engelle
from core.models import AppSetting

_FIYAT = {"paketler": [
    {"kod": "aylik", "magazalar": {"app_store": {"fiyat": "249.99", "deneme": "P1W"}}},
    {"kod": "yillik", "magazalar": {"app_store": {"fiyat": "2499.99", "deneme": "P1W"}}}]}


class SinyalTest(TestCase):
    def test_yeni_kullanici_gorevi_kuyruga_alir_mevcut_kullanici_almaz(self):
        with mock.patch("accounts.tasks.hosgeldin_eposta.apply_async") as kuyruk, \
                self.captureOnCommitCallbacks(execute=True):
            u = User.objects.create_user("yeni", email="yeni@ornek.com", password="x")
            u.first_name = "Ayşe"
            u.save()  # güncelleme ikinci kez tetiklemez
        kuyruk.assert_called_once_with((u.pk,), countdown=30)

    def test_personel_ve_epostasiz_tetiklemez(self):
        with mock.patch("accounts.tasks.hosgeldin_eposta.apply_async") as kuyruk, \
                self.captureOnCommitCallbacks(execute=True):
            User.objects.create_superuser("admin", "admin@ornek.com", "x")
            User.objects.create_user("bos", email="", password="x")
        kuyruk.assert_not_called()

    @override_settings(HOSGELDIN_EPOSTA_ENABLED=False)
    def test_kill_switch(self):
        with mock.patch("accounts.tasks.hosgeldin_eposta.apply_async") as kuyruk, \
                self.captureOnCommitCallbacks(execute=True):
            User.objects.create_user("yeni", email="yeni@ornek.com", password="x")
        kuyruk.assert_not_called()


class GonderimTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ayse", email="ayse@ornek.com", password="x",
                                             first_name="Ayşe Nur")

    def test_bir_kez_gider_icerik_pro_ve_fiyat(self):
        AppSetting.objects.create(key="magaza_fiyatlari", value=json.dumps(_FIYAT))
        self.assertEqual(hosgeldin.gonder(self.user.pk), "gonderildi")
        self.assertEqual(hosgeldin.gonder(self.user.pk), "zaten_gonderildi")
        self.assertEqual(len(mail.outbox), 1)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("Aramıza hoş geldiniz, Ayşe!", html)
        self.assertIn("İhale Asistanı", html)
        self.assertIn("249,99 TL", html)
        self.assertIn("2.499,99 TL", html)
        self.assertIn("İlk 1 hafta ücretsiz", html)
        self.assertIn("List-Unsubscribe", mail.outbox[0].extra_headers)

    def test_fiyat_kaydi_yoksa_fiyat_uydurulmaz(self):
        hosgeldin.gonder(self.user.pk)
        html = mail.outbox[0].alternatives[0][0]
        self.assertNotIn(" TL", html)
        self.assertIn("Pro ile bir adım önde", html)

    def test_eposta_tercihi_kapaliysa_gitmez(self):
        User.objects.filter(pk=self.user.pk).update(email_notifications=False)
        self.assertEqual(hosgeldin.gonder(self.user.pk), "eposta_yok_ya_da_kapali")
        self.assertEqual(len(mail.outbox), 0)

    def test_gonderim_duserse_damga_geri_alinir(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp")):
            with self.assertRaises(OSError):
                hosgeldin.gonder(self.user.pk)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.hosgeldin_eposta_at)
        self.assertEqual(hosgeldin.gonder(self.user.pk), "gonderildi")

    def test_engelli_adres_damgalanir_ama_gonderilmez_katmanda(self):
        engelle("ayse@ornek.com", EpostaEngeli.Sebep.BOUNCE)
        from core.eposta import engellileri_ayikla

        self.assertEqual(engellileri_ayikla([hosgeldin.eposta_olustur(self.user)]), [])
