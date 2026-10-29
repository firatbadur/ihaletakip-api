"""E-posta sağlayıcı seçimi (`core.eposta`) — admin'den ses ↔ resend."""
from django.test import TestCase, override_settings

from core.eposta import AYAR_ANAHTARI, SaglayiciBackend, aktif_saglayici
from core.models import AppSetting

AYAR = dict(EMAIL_HOST="email-smtp.eu-north-1.amazonaws.com", EMAIL_PORT=587,
            EMAIL_HOST_USER="AKIA_SES", EMAIL_HOST_PASSWORD="ses-sifre",
            RESEND_API_KEY="re_test", RESEND_SMTP_HOST="smtp.resend.com",
            RESEND_SMTP_PORT=587, EMAIL_SAGLAYICI="ses")


@override_settings(**AYAR)
class SaglayiciTest(TestCase):
    def _sec(self, deger):
        AppSetting.objects.update_or_create(key=AYAR_ANAHTARI, defaults={"value": deger})

    def test_varsayilan_ses(self):
        b = SaglayiciBackend()
        self.assertEqual((b.saglayici, b.host, b.username), ("ses", AYAR["EMAIL_HOST"], "AKIA_SES"))

    def test_admin_resend_secince_resend(self):
        self._sec(" Resend ")
        b = SaglayiciBackend()
        self.assertEqual((b.saglayici, b.host, b.username, b.password),
                         ("resend", "smtp.resend.com", "resend", "re_test"))

    def test_admin_geri_ses(self):
        self._sec("resend")
        self._sec("ses")
        self.assertEqual(SaglayiciBackend().host, AYAR["EMAIL_HOST"])

    def test_gecersiz_deger_ses(self):
        self._sec("mailgun")
        self.assertEqual(aktif_saglayici(), "ses")

    @override_settings(RESEND_API_KEY="")
    def test_kimliksiz_resend_ses_e_duser(self):
        self._sec("resend")
        self.assertEqual(aktif_saglayici(), "ses")

    @override_settings(EMAIL_SAGLAYICI="resend")
    def test_satir_yoksa_env_varsayilani(self):
        self.assertEqual(aktif_saglayici(), "resend")
