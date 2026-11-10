"""Günlük blog e-postası (12:00): son 24 saatin en yeni yazısı, bir kez, blogdan çıkan hariç."""
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from blog import eposta
from blog.models import BlogPost
from core import toplu
from tenders.services.eposta_bulten import abonelik_coz_tam


def _yazi(slug, saat_once):
    return BlogPost.objects.create(
        title=f"Yazı {slug}", slug=slug, summary="Kısa özet", content="Gövde metni",
        status="published", published_at=timezone.now() - timedelta(hours=saat_once))


@override_settings(TOPLU_EPOSTA_SANIYE=1000, BLOG_EPOSTA_ENABLED=True)
class BlogEpostaTest(TestCase):
    def setUp(self):
        cache.clear()
        yama = mock.patch("core.toplu.kuyruga_al")
        yama.start()
        self.addCleanup(yama.stop)
        self.okur = User.objects.create_user("okur", email="okur@ornek.com", password="x")
        self.blogsuz = User.objects.create_user("blogsuz", email="b@ornek.com", password="x",
                                                blog_eposta=False)

    def test_en_yeni_yazi_bir_kez_gider_blogdan_cikan_almaz(self):
        _yazi("eski", 3)
        _yazi("yeni", 1)
        sonuc = eposta.gunluk()
        self.assertEqual(sonuc["yazi"], "yeni")
        self.assertEqual(eposta.gunluk(), {"atlandi": "bugun_gonderildi"})  # "eski" gitmez
        g = toplu.TopluGonderim.objects.get()
        self.assertEqual(g.kategori, "blog")
        toplu.isle(g.pk)
        self.assertEqual([m.to for m in mail.outbox], [["okur@ornek.com"]])
        m = mail.outbox[0]
        self.assertEqual(m.subject, "Yazı yeni")
        self.assertIn("/blog/yeni?utm_source=eposta", m.alternatives[0][0])
        self.assertIn("Blog e-postalarından çık", m.alternatives[0][0])
        from urllib.parse import parse_qs, urlparse

        url = m.extra_headers["List-Unsubscribe"].strip("<>")
        self.assertEqual(abonelik_coz_tam(parse_qs(urlparse(url).query)["t"][0])[1], "blog")

    def test_ikinci_tetik_ayni_yaziyi_gondermez(self):
        _yazi("yeni", 1)
        eposta.gunluk()
        self.assertEqual(eposta.gunluk(), {"atlandi": "bugun_gonderildi"})
        self.assertEqual(toplu.TopluGonderim.objects.count(), 1)

    def test_arsiv_yazilari_gonderilmez(self):
        _yazi("arsiv", 30)
        self.assertEqual(eposta.gunluk(), {"atlandi": "yeni_yazi_yok"})

    def test_blog_aboneliginden_cikis_yalniz_blogu_kapatir(self):
        from django.urls import reverse
        from tenders.services.eposta_bulten import abonelik_token

        t = abonelik_token(self.okur, "blog")
        self.client.post(reverse("eposta-abonelik"), {"t": t})
        self.okur.refresh_from_db()
        self.assertEqual((self.okur.blog_eposta, self.okur.email_notifications), (False, True))
