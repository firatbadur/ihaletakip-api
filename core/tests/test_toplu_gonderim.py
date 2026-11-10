"""Toplu e-posta / push gönderimi (admin → Toplu Gönderim, `core/toplu.py`)."""
import json
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import EpostaEngeli, PushDevice, User
from accounts.services.eposta_engel import engelle
from core import toplu
from core.eposta import TEMALAR
from core.models import TopluGonderim, TopluGonderimAlici
from tenders.models import Notification


def _veri(**ek):
    v = {"kanal": "eposta", "konu": "Merhaba {ad}", "icerik": "Yeni özellik\n\nhttps://ihaletakip.com",
         "icerik_turu": "metin", "hedef": {"segment": "hepsi"}}
    v.update(ek)
    return v


@override_settings(TOPLU_EPOSTA_SANIYE=1000, PUBLIC_BASE_URL="https://api.ornek")
class GonderimTest(TestCase):
    def setUp(self):
        cache.clear()
        yama = mock.patch("core.toplu.kuyruga_al")
        self.kuyruk = yama.start()
        self.addCleanup(yama.stop)
        self.admin = User.objects.create_superuser("yonetici", "yonetici@ornek.com", "x")
        self.ali = User.objects.create_user("ali", email="ali@ornek.com", password="x",
                                            first_name="Ali")
        self.kapali = User.objects.create_user("kapali", email="kapali@ornek.com", password="x",
                                               email_notifications=False)
        self.olu = User.objects.create_user("olu", email="olu@ornek.com", password="x")
        engelle("olu@ornek.com", EpostaEngeli.Sebep.BOUNCE)
        User.objects.create_user("pasif", email="pasif@ornek.com", password="x", is_active=False)

    def _hepsini_isle(self, veri):
        g = toplu.olustur(veri, self.admin)
        toplu.isle(g.pk)
        g.refresh_from_db()
        return g

    def test_eposta_atlama_sebepleri_kisisellestirme_ve_abonelik(self):
        g = self._hepsini_isle(_veri())
        self.assertEqual(g.toplam, 4)  # pasif kullanıcı alıcı listesine hiç girmez
        self.assertEqual((g.gonderilen, g.atlanan, g.hatali, g.durum), (2, 2, 0, "tamamlandi"))
        alicilar = {m.to[0]: m for m in mail.outbox}
        self.assertEqual(set(alicilar), {"ali@ornek.com", "yonetici@ornek.com"})
        m = alicilar["ali@ornek.com"]
        self.assertEqual(m.subject, "Merhaba Ali")
        self.assertIn("List-Unsubscribe", m.extra_headers)
        html = m.alternatives[0][0]
        self.assertIn("https://api.ornek/static/ihaletakip/eposta/logo", html)
        self.assertIn('<a href="https://ihaletakip.com"', html)  # düz metin bağlantısı tıklanır
        sebepler = dict(g.alicilar.exclude(durum="gonderildi").values_list("user__username", "sebep"))
        self.assertIn("kapalı", sebepler["kapali"])
        self.assertIn("engelli", sebepler["olu"])

    def test_ayni_kisiye_iki_kez_gitmez(self):
        g = self._hepsini_isle(_veri())
        toplu.isle(g.pk)
        self.assertEqual(len(mail.outbox), 2)

    def test_secili_kullanicilar(self):
        g = self._hepsini_isle(_veri(hedef={"segment": "secili", "user_ids": [self.ali.pk]}))
        self.assertEqual((g.toplam, g.gonderilen), (1, 1))

    @override_settings(TOPLU_TUR_SN=0)
    def test_sure_dolunca_kendini_yeniden_kuyruga_atar(self):
        g = toplu.olustur(_veri(), self.admin)
        self.kuyruk.reset_mock()
        sonuc = toplu.isle(g.pk)
        self.assertTrue(sonuc["devam"])
        self.kuyruk.assert_called_once_with(g.pk, gecikme=2)

    def test_iptal_bekleyenleri_kapatir(self):
        g = toplu.olustur(_veri(), self.admin)
        toplu.iptal_et(g)
        g.refresh_from_db()
        self.assertEqual((g.durum, g.atlanan), ("iptal", 4))
        toplu.isle(g.pk)
        self.assertEqual(len(mail.outbox), 0)

    def test_push_cihazsiz_atlanir_uygulama_ici_satir_yazilir(self):
        PushDevice.objects.create(user=self.ali, token="tok-ali", platform="ios")
        with mock.patch("tenders.services.push.send_fcm", return_value="sent") as fcm:
            g = self._hepsini_isle(_veri(kanal="bildirim", konu="Yeni {ad}", icerik="Kısa metin",
                                         uygulama_ici=True, sessiz_saat_yok=True))
        self.assertEqual((g.gonderilen, g.atlanan), (1, 3))
        self.assertEqual(fcm.call_args[0][1], "Yeni Ali")
        self.assertEqual(Notification.objects.filter(title__startswith="Yeni").count(), 4)


@override_settings(TOPLU_EPOSTA_SANIYE=1000)
class AdminEkraniTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser("yonetici", "yonetici@ornek.com", "x")
        self.staff = User.objects.create_user("staff", email="s@ornek.com", password="x",
                                              is_staff=True)

    def _post(self, ad, govde):
        return self.client.post(reverse(ad), json.dumps(govde), content_type="application/json")

    def test_yalniz_super_kullanici(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("core_toplu")).status_code, 403)
        self.assertEqual(self._post("core_toplu_gonder", {**_veri(), "onay": True}).status_code, 403)
        self.assertFalse(TopluGonderim.objects.exists())

    def test_ekran_onizleme_ve_onaysiz_gonderim(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("core_toplu")).status_code, 200)
        r = self._post("core_toplu_onizle", _veri(icerik_turu="html", icerik="<p>Selam {ad}</p>"))
        self.assertIn("<p>Selam ", r.json()["html"])
        self.assertEqual(self._post("core_toplu_gonder", _veri()).status_code, 400)
        r = self._post("core_toplu_gonder", _veri(dugme_metni="Aç", dugme_url="http://x.com"))
        self.assertIn("dugme_url", r.json()["hatalar"])

    def test_onayli_gonderim_kuyruga_alinir(self):
        self.client.force_login(self.admin)
        with mock.patch("core.toplu.kuyruga_al") as kuyruk, \
                self.captureOnCommitCallbacks(execute=True):
            r = self._post("core_toplu_gonder", {**_veri(), "onay": True})
        self.assertEqual(r.status_code, 200)
        g = TopluGonderim.objects.get()
        kuyruk.assert_called_once_with(g.pk)
        self.assertEqual(TopluGonderimAlici.objects.filter(gonderim=g).count(), 2)

    def test_kullanici_arama(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("core_toplu_ara"), {"q": "s@orn"})
        self.assertEqual([u["id"] for u in r.json()["sonuclar"]], [self.staff.pk])


class TemaTest(TestCase):
    def test_her_tema_bulten_kod_ve_toplu_eposta_cizer(self):
        from accounts.services.eposta_kod import kod_baglami
        from django.template.loader import render_to_string

        bolum = {"baslik": "Su filtresi", "metin": "3 ihale", "etiket": "Kayıtlı filtreler",
                 "url": "https://x", "dugme": "Gör", "ihaleler": [
                     {"ad": "Boru alımı", "idare": "Belediye", "il": "Ankara", "tarih": "1 Ara",
                      "tur": "Mal", "ikn": "2026/1", "url": "https://x/i"}]}
        user = User.objects.create_user("u", email="u@ornek.com", password="x")
        for tema in TEMALAR:
            with self.subTest(tema=tema):
                from tenders.services.eposta_bulten import sablon_baglami

                html = render_to_string("eposta/bulten.html", sablon_baglami({
                    "ad": "Ali", "tarih": "10 Kasım", "kisa_tarih": "10 Kasım",
                    "bolumler": [bolum], "adet": 1, "ozet": "x", "email": "u@ornek.com",
                    "anasayfa": "https://x", "ayarlar": "https://x", "abonelik": "https://x/c"},
                    tema))
                self.assertIn("Boru alımı", html)
                self.assertIn("eposta/logo", html)
                self.assertIn("Günaydın Ali", html)
                self.assertIn("2026", render_to_string("eposta/kod.html", kod_baglami("kayit", "2026", tema)))
                g = TopluGonderim(kanal="eposta", konu="K", icerik="<b>gövde</b>",
                                  icerik_turu="html", tema=tema)
                self.assertIn("<b>gövde</b>", toplu.eposta_olustur(g, user)[1])
