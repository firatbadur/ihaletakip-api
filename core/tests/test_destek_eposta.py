"""
Destek e-postaları: kullanıcı yazınca EKİBE, destek yanıtlayınca KULLANICIYA.

⚠️ Arıza (2026-11-10): ekip e-postası kodda vardı ama üretimde alıcı hiç tanımlanmamıştı
→ hiçbir talep yöneticiye ulaşmadı; ayrıca `fail_silently=True` SMTP hatasını iz
bırakmadan yutuyordu. Kullanıcıya ise destek yanıtı hiç e-postalanmıyordu.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.core.management import call_command
from django.test import TestCase, override_settings

from core import destek_eposta, support
from core.models import AppSetting, SupportMessage
from core.tasks import destek_ekibine_eposta

User = get_user_model()
IZOLE = override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SUPPORT_NOTIFY_EMAILS=[],
)


@IZOLE
class AlicilarTest(TestCase):
    def test_ayar_yoksa_env(self):
        with self.settings(SUPPORT_NOTIFY_EMAILS=["ekip@ornek.com"]):
            self.assertEqual(destek_eposta.alicilar(), ["ekip@ornek.com"])

    def test_admin_ayari_env_i_ezer_gecersiz_ve_tekrar_atilir(self):
        AppSetting.objects.create(key=destek_eposta.AYAR_ANAHTARI,
                                  value="a@ornek.com, bozuk-adres;\nA@ornek.com b@ornek.com")
        with self.settings(SUPPORT_NOTIFY_EMAILS=["env@ornek.com"]):
            self.assertEqual(destek_eposta.alicilar(), ["a@ornek.com", "b@ornek.com"])

    def test_bos_admin_ayari_kapali_demek(self):
        AppSetting.objects.create(key=destek_eposta.AYAR_ANAHTARI, value="")
        with self.settings(SUPPORT_NOTIFY_EMAILS=["env@ornek.com"]):
            self.assertEqual(destek_eposta.alicilar(), [])


@IZOLE
class EkipEpostasiTest(TestCase):
    def setUp(self):
        cache.clear()
        self.pro = User.objects.create_user(
            username="pro", email="pro@test.local", password="x", first_name="Ayşe",
            subscription_tier=User.Tier.PRO)
        self.yonetici = User.objects.create_superuser(
            username="admin", email="admin@test.local", password="x")
        AppSetting.objects.create(key=destek_eposta.AYAR_ANAHTARI, value="yonetici@ornek.com")

    def _ac(self, **ek):
        with patch("core.tasks.destek_ekibine_eposta.delay") as gecikmeli, \
                self.captureOnCommitCallbacks(execute=True):
            t = support.talep_ac(self.pro, message="Bildirim <b>gelmiyor</b>, yardım?",
                                 category="bildirim", meta={"platform": "ios"}, **ek)
        return t, gecikmeli

    def test_yeni_talep_kuyruga_girer(self):
        t, gecikmeli = self._ac()
        gecikmeli.assert_called_once_with(t.pk, t.message, True)

    def test_alici_yoksa_kuyruga_hic_girmez(self):
        AppSetting.objects.filter(key=destek_eposta.AYAR_ANAHTARI).update(value="")
        _, gecikmeli = self._ac()
        gecikmeli.assert_not_called()

    def test_kullanici_yaniti_kuyruga_girer_destek_yaniti_girmez(self):
        t, _ = self._ac()
        with patch("core.tasks.destek_ekibine_eposta.delay") as gecikmeli, \
                patch("core.tasks.destek_yaniti_eposta.delay"), \
                patch("tenders.services.notify.push_to_user", return_value=False), \
                self.captureOnCommitCallbacks(execute=True):
            support.destek_yaniti(t, "Ayarları kontrol edin.", author=self.yonetici)
            support.kullanici_yaniti(t, "Hâlâ gelmiyor.")
        gecikmeli.assert_called_once_with(t.pk, "Hâlâ gelmiyor.", False)

    def test_eposta_icerigi(self):
        t, _ = self._ac()
        self.assertEqual(destek_eposta.ekibe_gonder(t.pk, t.message, True), "gonderildi")
        self.assertEqual(len(mail.outbox), 1)
        e = mail.outbox[0]
        self.assertEqual(e.to, ["yonetici@ornek.com"])
        self.assertEqual(e.reply_to, [])   # yanıt panelden yazılmalı
        self.assertIn(f"#{t.pk}", e.subject)
        self.assertIn("Bildirimler", e.subject)
        html = e.alternatives[0][0]
        self.assertIn(f"/admin/core/supportticket/{t.pk}/change/", html)
        self.assertIn("Pro", html)
        self.assertIn("ios", html)
        self.assertIn("&lt;b&gt;gelmiyor&lt;/b&gt;", html)   # kullanıcı metni kaçırılır
        self.assertNotIn("<b>gelmiyor</b>", html)

    def test_yanit_epostasi_onceki_mesajlari_tasir(self):
        t, _ = self._ac()
        with patch("core.tasks.destek_yaniti_eposta.delay"), \
                self.captureOnCommitCallbacks(execute=False):
            support.destek_yaniti(t, "Filtre bildirimini açın.", author=self.yonetici)
            support.kullanici_yaniti(t, "Açtım ama yine yok.")
        destek_eposta.ekibe_gonder(t.pk, "Açtım ama yine yok.", False)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("Filtre bildirimini açın.", html)
        self.assertIn("Önceki mesajlar", html)

    def test_smtp_hatasi_yutulmaz(self):
        t, _ = self._ac()
        with patch.object(EmailMultiAlternatives, "send", side_effect=OSError("smtp kapalı")):
            with self.assertRaises(OSError):
                destek_ekibine_eposta(t.pk, t.message, True)

    def test_deneme_komutu_kuru(self):
        call_command("destek_eposta_test", "--kuru", stdout=open("/dev/null", "w"))
        self.assertEqual(len(mail.outbox), 0)
        call_command("destek_eposta_test", stdout=open("/dev/null", "w"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith("[TEST]"))


@IZOLE
class KullaniciYanitEpostasiTest(TestCase):
    def setUp(self):
        cache.clear()
        self.pro = User.objects.create_user(
            username="pro", email="pro@test.local", password="x",
            subscription_tier=User.Tier.PRO, email_notifications=False)
        self.yonetici = User.objects.create_superuser(
            username="admin", email="admin@test.local", password="x")
        self.t = support.talep_ac(self.pro, message="Faturamı göremiyorum, nereden bakarım?")

    def _yanitla(self, metin="Ayarlar > Abonelik ekranından görebilirsiniz."):
        with patch("core.tasks.destek_yaniti_eposta.delay") as gecikmeli, \
                patch("tenders.services.notify.push_to_user", return_value=False), \
                self.captureOnCommitCallbacks(execute=True):
            m = support.destek_yaniti(self.t, metin, author=self.yonetici)
        return m, gecikmeli

    def test_destek_yaniti_eposta_kuyruguna_girer(self):
        m, gecikmeli = self._yanitla()
        gecikmeli.assert_called_once_with(m.pk)

    def test_yanit_kullaniciya_bir_kez_gider(self):
        m, _ = self._yanitla()
        # email_notifications=False → işlemsel e-posta yine gider (bülten tercihi değil)
        self.assertEqual(destek_eposta.kullaniciya_gonder(m.pk), "gonderildi")
        self.assertEqual(destek_eposta.kullaniciya_gonder(m.pk), "zaten_gonderildi")
        self.assertEqual(len(mail.outbox), 1)
        e = mail.outbox[0]
        self.assertEqual(e.to, ["pro@test.local"])
        self.assertIn(f"#{self.t.pk}", e.subject)
        self.assertIn("Ayarlar &gt; Abonelik", e.alternatives[0][0])
        self.assertIn("Ayarlar > Abonelik", e.body)

    def test_gonderim_duserse_kilit_birakilir(self):
        m, _ = self._yanitla()
        with patch.object(EmailMultiAlternatives, "send", side_effect=OSError("smtp")):
            with self.assertRaises(OSError):
                destek_eposta.kullaniciya_gonder(m.pk)
        self.assertEqual(destek_eposta.kullaniciya_gonder(m.pk), "gonderildi")
        self.assertEqual(len(mail.outbox), 1)

    def test_kullanici_mesaji_icin_gonderilmez(self):
        m = SupportMessage.objects.filter(ticket=self.t, sender="user").first()
        self.assertEqual(destek_eposta.kullaniciya_gonder(m.pk), "destek_yaniti_degil")
        self.assertEqual(len(mail.outbox), 0)

    def test_talepteki_iletisim_adresi_once_gelir(self):
        self.t.email = "iletisim@firma.com"
        self.t.save(update_fields=["email"])
        m, _ = self._yanitla()
        destek_eposta.kullaniciya_gonder(m.pk)
        self.assertEqual(mail.outbox[0].to, ["iletisim@firma.com"])
