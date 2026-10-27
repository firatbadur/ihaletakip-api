"""Günlük e-posta bülteni + e-posta tercihi + abonelikten çıkma.

Bülten yeni eşleştirme yapmaz: penceredeki `Notification` satırlarını anlatır. Filtre
özetinin ihale listesi uygulamanın açtığı listeyle AYNI fonksiyondan kurulur
(`kayitli_filtre_birlesimi`) — `test_liste_yalniz_filtreye_uyanlar` bunu tutar.
"""
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from ekap.models import Tender
from tenders.models import Notification, SavedFilter
from tenders.services import eposta_bulten as eb
from tenders.tasks import check_saved_filter_matches, send_email_digest

DUN = None  # setUp'ta hesaplanır


def _ihale(i, sektor="su_kanalizasyon"):
    t = Tender.objects.create(
        ikn=f"2026/{i}", ekap_id=f"e{i}", ihale_adi=f"Su Hattı İşi {i}", sektor=sektor,
        idare_adi="ÖRNEK BELEDİYESİ", ihale_durum=2, ilan_tarihi=timezone.now(),
        ihale_tarihi=timezone.now() + timedelta(days=20))
    Tender.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(hours=3))
    return t


@override_settings(NOTIF_BIRLESIK_BILDIRIM=True, EPOSTA_BULTEN_ENABLED=True,
                   EPOSTA_LINK_TABANI="https://ihaletakip.com",
                   PUBLIC_BASE_URL="https://api.ornek", EPOSTA_BULTEN_GUNLUK_TAVAN=1200)
class BultenTest(TestCase):
    def setUp(self):
        cache.clear()
        self.dun = timezone.localdate() - timedelta(days=1)
        self.taban, self.tavan = eb.pencere(self.dun)
        self.user = User.objects.create_user(
            "ahmet", email="ahmet@firma.com.tr", password="x", first_name="Ahmet")
        SavedFilter.objects.create(user=self.user, name="Su", alarm=True,
                                   filters={"sektor": ["su_kanalizasyon"]})
        _ihale(1)
        _ihale(2)
        _ihale(3, sektor="yol_altyapi")

    def _filtre_bildirimi(self):
        """Gerçek görevle üretilir, sonra bülten penceresinin içine taşınır."""
        check_saved_filter_matches()
        n = Notification.objects.get(user=self.user)
        Notification.objects.filter(pk=n.pk).update(created_at=self.tavan - timedelta(hours=2))
        return n

    def _gonder(self):
        return send_email_digest(gun=self.dun.isoformat())

    def test_tek_eposta_ihale_listesi_ve_baglantilar(self):
        self._filtre_bildirimi()
        sonuc = self._gonder()
        self.assertEqual(sonuc["gonderilen"], 1)
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["ahmet@firma.com.tr"])
        html = m.alternatives[0][0]
        self.assertIn("Günaydın Ahmet", html)
        self.assertIn("Su Hattı İşi 1", html)
        self.assertIn("https://ihaletakip.com/ihale/2026-1?", html)
        self.assertIn("Su Hattı İşi 1", m.body)
        self.assertIn("https://api.ornek/eposta/abonelik/?t=", m.extra_headers["List-Unsubscribe"])
        self.assertEqual(m.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_liste_yalniz_filtreye_uyanlar(self):
        self._filtre_bildirimi()
        baglam = eb.bulten_kur(self.user, self.taban, self.tavan)
        self.assertEqual({t["ikn"] for t in baglam["bolumler"][0]["ihaleler"]},
                         {"2026/1", "2026/2"})

    def test_ayni_gun_ikinci_tetik_mukerrer_gondermez(self):
        self._filtre_bildirimi()
        self._gonder()
        self.assertEqual(self._gonder()["gonderilen"], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_tercih_kapaliysa_gitmez(self):
        self._filtre_bildirimi()
        User.objects.filter(pk=self.user.pk).update(email_notifications=False)
        self._gonder()
        self.assertEqual(mail.outbox, [])

    def test_pencere_disindaki_bildirim_gitmez(self):
        n = self._filtre_bildirimi()
        Notification.objects.filter(pk=n.pk).update(created_at=self.taban - timedelta(minutes=1))
        self._gonder()
        self.assertEqual(mail.outbox, [])

    def test_birden_cok_bildirim_tek_epostada(self):
        self._filtre_bildirimi()
        a = Notification.objects.create(user=self.user, type=Notification.Type.ALARM,
                                        title="Su Hattı İşi 1", body="İhale günü bugün.",
                                        tender_ikn="2026/1")
        Notification.objects.filter(pk=a.pk).update(created_at=self.tavan - timedelta(hours=1))
        self._gonder()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2 yeni gelişme", mail.outbox[0].subject)
        self.assertIn("İhale alarmı", mail.outbox[0].alternatives[0][0])

    def test_gonderim_hatasi_kilidi_birakir(self):
        self._filtre_bildirimi()
        with mock.patch("django.core.mail.EmailMultiAlternatives.send",
                        side_effect=OSError("smtp")):
            self.assertEqual(self._gonder()["hatali"], 1)
        self.assertEqual(self._gonder()["gonderilen"], 1)

    def test_pencere_kapanmadan_gonderilmez(self):
        self._filtre_bildirimi()
        yarin = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(send_email_digest(gun=yarin), {"atlandi": "pencere_kapanmadi"})

    @override_settings(EPOSTA_BULTEN_ENABLED=False)
    def test_kill_switch(self):
        self._filtre_bildirimi()
        self.assertEqual(self._gonder(), {"atlandi": "kapali"})


class TercihTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ahmet", email="ahmet@firma.com.tr", password="x")
        self.client.force_login(self.user)
        from rest_framework_simplejwt.tokens import RefreshToken

        self.h = {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(self.user).access_token}"}

    def test_varsayilan_acik_ve_profilden_kapatilir(self):
        d = self.client.get("/api/v1/auth/profile/", **self.h).json()["data"]
        self.assertIs(d["email_notifications"], True)
        r = self.client.patch("/api/v1/auth/profile/", {"email_notifications": False},
                              content_type="application/json", **self.h)
        self.assertIs(r.json()["data"]["email_notifications"], False)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_notifications)

    def test_eposta_profilden_degistirilemez(self):
        self.client.patch("/api/v1/auth/profile/", {"email": "baska@x.com"},
                          content_type="application/json", **self.h)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "ahmet@firma.com.tr")


class AbonelikTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ahmet", email="ahmet@firma.com.tr", password="x")
        self.yol = f"/eposta/abonelik/?t={eb.abonelik_token(self.user)}"

    def _acik(self):
        self.user.refresh_from_db()
        return self.user.email_notifications

    def test_get_tercihi_degistirmez(self):
        r = self.client.get(self.yol)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "çıkılsın mı")
        self.assertTrue(self._acik())

    def test_tek_tik_post_cikarir_ve_geri_acilir(self):
        r = self.client.post(self.yol, {"List-Unsubscribe": "One-Click"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self._acik())
        self.client.post(self.yol, {"islem": "geri"})
        self.assertTrue(self._acik())

    def test_gecersiz_token(self):
        r = self.client.post("/eposta/abonelik/?t=sahte")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self._acik())
