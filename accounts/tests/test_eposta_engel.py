"""E-posta engelleri: kalıcı bounce / şikâyet gelen adrese HİÇBİR e-posta gitmez.

Kapı gönderim katmanındadır (`core.eposta.SaglayiciBackend`) — testler o sınıfı SMTP'siz
çalıştırır (Django test koşucusu `EMAIL_BACKEND`'i locmem yaptığı için ayrıca kurulur).
"""
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import EpostaEngeli, EpostaKodu, User
from accounts.services import eposta_engel
from accounts.services.eposta_kod import EpostaHatasi, kod_gonder
from core.eposta import SaglayiciBackend


class GonderimKatmaniTest(TestCase):
    def setUp(self):
        eposta_engel.engelle("olu@ornek.com", EpostaEngeli.Sebep.BOUNCE)
        yama = mock.patch("django.core.mail.backends.smtp.EmailBackend.send_messages",
                          side_effect=lambda mesajlar: len(mesajlar))
        self.smtp = yama.start()
        self.addCleanup(yama.stop)

    def _gonder(self, *mesajlar):
        return SaglayiciBackend().send_messages(list(mesajlar))

    def test_engelli_alici_cikarilir_digerleri_kalir(self):
        m = EmailMessage("k", "g", to=["Ölü <OLU@ornek.com>", "canli@ornek.com"],
                         bcc=["olu@ornek.com"])
        self.assertEqual(self._gonder(m), 1)
        gonderilen = self.smtp.call_args[0][0]
        self.assertEqual(gonderilen[0].to, ["canli@ornek.com"])
        self.assertEqual(gonderilen[0].bcc, [])

    def test_yalniz_engelli_aliciyi_tasiyan_mesaj_hic_gonderilmez(self):
        self.assertEqual(self._gonder(EmailMessage("k", "g", to=["olu@ornek.com"]),
                                      EmailMessage("k", "g", to=["canli@ornek.com"])), 1)
        self.assertEqual([m.to for m in self.smtp.call_args[0][0]], [["canli@ornek.com"]])

    def test_kaldirilan_engel_gonderime_izin_verir(self):
        eposta_engel.engeli_kaldir(EpostaEngeli.objects.get())
        self.assertEqual(self._gonder(EmailMessage("k", "g", to=["olu@ornek.com"])), 1)

    def test_kontrol_hatasinda_gonderim_durmaz(self):
        with mock.patch("accounts.services.eposta_engel.engelli_kume",
                        side_effect=RuntimeError("db yok")):
            self.assertEqual(self._gonder(EmailMessage("k", "g", to=["olu@ornek.com"])), 1)

    def test_yeni_geri_donus_kaldirilmis_engeli_yeniden_etkinlestirir(self):
        e = EpostaEngeli.objects.get()
        eposta_engel.engeli_kaldir(e)
        self.assertTrue(eposta_engel.engelle("OLU@ornek.com", EpostaEngeli.Sebep.SIKAYET))
        e.refresh_from_db()
        self.assertEqual((e.aktif, e.sebep, e.sayac), (True, "sikayet", 2))


@override_settings(EPOSTA_GUNLUK_TAVAN=100)
class KayitKoduTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_engelli_adrese_kayit_kodu_acik_hatayla_reddedilir(self):
        eposta_engel.engelle("olu@ornek.com", EpostaEngeli.Sebep.BOUNCE)
        with self.assertRaises(EpostaHatasi) as h:
            kod_gonder("Olu@Ornek.com", EpostaKodu.Amac.KAYIT, {"sifre": "x"})
        self.assertEqual(h.exception.detail["code"], "email_undeliverable")
        self.assertEqual(len(mail.outbox), 0)

    def test_kod_epostasi_markali_duzenle_gider(self):
        kod_gonder("yeni@ornek.com", EpostaKodu.Amac.KAYIT, {"sifre": "x"})
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("/static/ihaletakip/eposta/logo", html)
        self.assertIn("Doğrulama kodunuz", html)


@override_settings(EPOSTA_BULTEN_ENABLED=True, EPOSTA_BULTEN_GUNLUK_TAVAN=100)
class BultenEngelTest(TestCase):
    def setUp(self):
        from tenders.models import Notification
        from tenders.services import eposta_bulten as eb

        cache.clear()
        self.dun = timezone.localdate() - timedelta(days=1)
        _, tavan = eb.pencere(self.dun)
        for ad in ("olu", "canli"):
            u = User.objects.create_user(ad, email=f"{ad}@ornek.com", password="x")
            n = Notification.objects.create(user=u, type="info", title="Duyuru", body="metin")
            Notification.objects.filter(pk=n.pk).update(created_at=tavan - timedelta(hours=1))
        eposta_engel.engelle("olu@ornek.com", EpostaEngeli.Sebep.BOUNCE)

    def test_engelli_adres_bultende_atlanir(self):
        from tenders.tasks import send_email_digest

        sonuc = send_email_digest(gun=self.dun.isoformat())
        self.assertEqual((sonuc["gonderilen"], sonuc["atlanan"]), (1, 1))
        self.assertEqual([m.to for m in mail.outbox], [["canli@ornek.com"]])
