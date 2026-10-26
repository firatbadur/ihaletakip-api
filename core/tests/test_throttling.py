"""Hız sınırı + sayfa derinliği (core/throttling.py).

Asıl koruma: **mobil/doğrudan trafik varsayılan `gozlem` modunda hiçbir isteği
reddetmez** (mevcut kullanıcılar etkilenmesin); portal anahtarlı trafik `uygula`.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core.throttling import gozlem_ozeti

LISTE = "/api/v1/ekap/tenders/"
ANAHTAR = "portal-sir"
KUCUK = {"ihale_arama": {"anonim": [[60, 2]], "girisli": [[60, 3]]}}
PORTAL = {"HTTP_X_IT_PORTAL_KEY": ANAHTAR}


@override_settings(PORTAL_KEY=ANAHTAR, API_THROTTLE_ORANLAR=KUCUK,
                   API_THROTTLE_MODU="gozlem", API_THROTTLE_PORTAL_MODU="uygula")
class ThrottleTest(TestCase):
    def setUp(self):
        cache.clear()

    def _n(self, n, **meta):
        return [self.client.get(LISTE, **meta).status_code for _ in range(n)]

    def test_gozlem_modu_hic_reddetmez_ama_sayar(self):
        """Mobil (anahtarsız) trafik: sınır aşılsa da 200 — yalnızca sayaç."""
        self.assertEqual(self._n(5, HTTP_X_REAL_IP="1.1.1.1"), [200] * 5)
        ozet = gozlem_ozeti(timezone.localdate().isoformat())
        self.assertEqual(ozet[("hiz", "ihale_arama", "dogrudan-anonim")], (3, 1))

    def test_portal_uygula_429_turkce_zarf_retry_after(self):
        kodlar = self._n(3, HTTP_X_IT_CLIENT_IP="2.2.2.2", **PORTAL)
        self.assertEqual(kodlar, [200, 200, 429])
        r = self.client.get(LISTE, HTTP_X_IT_CLIENT_IP="2.2.2.2", **PORTAL)
        self.assertEqual(r.status_code, 429)
        g = r.json()
        self.assertFalse(g["success"])
        self.assertIsNone(g["data"])
        self.assertEqual(g["message"],
                         "Çok fazla istek gönderildi. Lütfen biraz bekleyip tekrar deneyin.")
        self.assertTrue(int(r["Retry-After"]) > 0)

    def test_portal_farkli_istemci_ipleri_ayri_kova(self):
        self.assertEqual(self._n(2, HTTP_X_IT_CLIENT_IP="3.3.3.3", **PORTAL), [200, 200])
        self.assertEqual(self._n(2, HTTP_X_IT_CLIENT_IP="4.4.4.4", **PORTAL), [200, 200])

    @override_settings(API_THROTTLE_MODU="uygula")
    def test_yanlis_anahtarla_istemci_ip_yok_sayilir(self):
        """Sahte `X-IT-Client-IP` ile kova değiştirilemez."""
        sahte = {"HTTP_X_IT_PORTAL_KEY": "yanlis", "HTTP_X_REAL_IP": "5.5.5.5"}
        self.client.get(LISTE, HTTP_X_IT_CLIENT_IP="6.6.6.1", **sahte)
        self.client.get(LISTE, HTTP_X_IT_CLIENT_IP="6.6.6.2", **sahte)
        r = self.client.get(LISTE, HTTP_X_IT_CLIENT_IP="6.6.6.3", **sahte)
        self.assertEqual(r.status_code, 429)

    def test_dogrulanmis_bot_sinirlanmaz_anahtarsiz_bot_basligi_etkisiz(self):
        bot = {"HTTP_X_IT_VERIFIED_BOT": "1", "HTTP_X_IT_CLIENT_IP": "7.7.7.7"}
        self.assertEqual(self._n(5, **bot, **PORTAL), [200] * 5)
        with override_settings(API_THROTTLE_MODU="uygula"):
            kodlar = self._n(3, HTTP_X_REAL_IP="8.8.8.8", **bot)
        self.assertEqual(kodlar[-1], 429)

    @override_settings(API_THROTTLE_MODU="uygula")
    def test_girisli_kimlik_kullanici_id(self):
        User = get_user_model()
        a = User.objects.create_user(username="a", email="a@t.local", password="x")
        b = User.objects.create_user(username="b", email="b@t.local", password="x")
        self.client.force_login(a)
        self.assertEqual(self._n(4, HTTP_X_REAL_IP="9.9.9.9"), [200, 200, 200, 429])
        self.client.force_login(b)  # aynı IP, farklı kullanıcı → ayrı kova
        self.assertEqual(self._n(1, HTTP_X_REAL_IP="9.9.9.9"), [200])

    @override_settings(API_THROTTLE_MODU="uygula")
    def test_redis_hatasi_istegi_dusurmez(self):
        with mock.patch("core.throttling.cache.incr", side_effect=RuntimeError("redis")):
            self.assertEqual(self._n(5), [200] * 5)

    @override_settings(API_THROTTLE_MODU="kapali")
    def test_kapali_mod(self):
        self.assertEqual(self._n(5), [200] * 5)

    def test_kapsamsiz_uc_sayilmaz(self):
        with override_settings(API_THROTTLE_MODU="uygula"):
            kodlar = [self.client.get("/api/v1/ekap/cities/").status_code for _ in range(5)]
        self.assertEqual(kodlar, [200] * 5)

    @override_settings(API_THROTTLE_PORTAL_MODU="uygula",
                       API_THROTTLE_ORANLAR={"ihale_arama": {"anonim": [[60, 1000]]}})
    def test_kademeli_engel(self):
        from core.throttling import Istemci, KapsamliThrottle

        ist = Istemci("ip:10.0.0.1", True, False, False)
        t = KapsamliThrottle()
        sureler = [t._ceza(ist, 60.0 * dk) for dk in range(1, 11)]
        self.assertEqual(sureler[:2], [None, None])
        self.assertEqual(sureler[2], 15 * 60)
        self.assertEqual(sureler[5], 3600)
        self.assertEqual(sureler[9], 24 * 3600)


@override_settings(PORTAL_KEY=ANAHTAR, API_THROTTLE_MODU="gozlem",
                   API_THROTTLE_PORTAL_MODU="uygula")
class SayfaDerinligiTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_portal_anonim_11_sayfa_400(self):
        ok = self.client.get(LISTE, {"page": 10, "page_size": 20},
                             HTTP_X_IT_CLIENT_IP="1.2.3.4", **PORTAL)
        self.assertEqual(ok.status_code, 200)
        r = self.client.get(LISTE, {"page": 11, "page_size": 20},
                            HTTP_X_IT_CLIENT_IP="1.2.3.4", **PORTAL)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["success"])

    def test_portal_girisli_50_sayfa(self):
        u = get_user_model().objects.create_user(username="d", email="d@t.local", password="x")
        self.client.force_login(u)
        self.assertEqual(self.client.get(LISTE, {"page": 50, "page_size": 20},
                                         **PORTAL).status_code, 200)
        self.assertEqual(self.client.get(LISTE, {"page": 51, "page_size": 20},
                                         **PORTAL).status_code, 400)

    def test_mobil_derin_sayfa_engellenmez_sayilir(self):
        """Mobil sonsuz kaydırma: hata dönseydi tüm liste hata ekranına düşerdi."""
        r = self.client.get(LISTE, {"page": 40, "page_size": 10})
        self.assertEqual(r.status_code, 200)
        ozet = gozlem_ozeti(timezone.localdate().isoformat())
        self.assertIn(("derinlik", "ihale_arama", "dogrudan-anonim"), ozet)

    def test_firma_listesi_de_sinirli(self):
        r = self.client.get("/api/v1/ekap/contractors/", {"page": 11, "page_size": 20},
                            HTTP_X_IT_CLIENT_IP="1.2.3.5", **PORTAL)
        self.assertEqual(r.status_code, 400)


DOLUM = {"HTTP_X_IT_CACHE_FILL": "1"}


@override_settings(PORTAL_KEY=ANAHTAR, API_THROTTLE_ORANLAR=KUCUK,
                   API_THROTTLE_DOLUM_ORANLARI={"ihale_arama": [[60, 3]]},
                   API_THROTTLE_MODU="uygula", API_THROTTLE_PORTAL_MODU="uygula")
class OnbellekDolumuTest(TestCase):
    """Portal 2. not #8: `X-IT-Cache-Fill` ayrı kova."""

    def setUp(self):
        cache.clear()

    def _n(self, n, **meta):
        return [self.client.get(LISTE, **meta).status_code for _ in range(n)]

    def test_dolum_kendi_kovasinda_ip_kovasi_etkilenmez(self):
        # Anonim IP sınırı 2; dolum sınırı 3 → dolum IP kovasını tüketseydi 3. istek 429 olurdu.
        self.assertEqual(self._n(3, HTTP_X_REAL_IP="10.0.0.9", **DOLUM, **PORTAL),
                         [200, 200, 200])
        self.assertEqual(self._n(1, HTTP_X_REAL_IP="10.0.0.9", **DOLUM, **PORTAL), [429])
        # Aynı web konteyneri IP'sinden gelen kişiye özel istek kendi kovasında.
        self.assertEqual(self._n(2, HTTP_X_IT_CLIENT_IP="11.0.0.1", HTTP_X_REAL_IP="10.0.0.9",
                                 **PORTAL), [200, 200])

    def test_dolum_dolunca_girisli_kullanici_etkilenmez(self):
        self._n(4, **DOLUM, **PORTAL)
        u = get_user_model().objects.create_user(username="g", email="g@t.local",
                                                 password="x")
        self.client.force_login(u)
        self.assertEqual(self._n(1, **PORTAL), [200])

    def test_anahtarsiz_dolum_basligi_etkisiz(self):
        """Taklit: anahtarsız `X-IT-Cache-Fill` normal IP kovasına düşer (sınır 2)."""
        sahte = {"HTTP_X_IT_PORTAL_KEY": "yanlis", "HTTP_X_REAL_IP": "12.0.0.1", **DOLUM}
        self.assertEqual(self._n(3, **sahte), [200, 200, 429])

    def test_dolum_kovasina_kademeli_engel_yok_ve_loglanir(self):
        from core.throttling import DOLUM_KIMLIGI

        with self.assertLogs("ihaletakip", level="WARNING") as log:
            for _ in range(15):
                self.client.get(LISTE, {"q": "asfalt"}, **DOLUM, **PORTAL)
        self.assertIsNone(cache.get(f"thr:ban:{DOLUM_KIMLIGI}"))
        self.assertTrue(any("portal-dolum" in s and "q=asfalt" in s for s in log.output))
        self.assertFalse(any(ANAHTAR in s for s in log.output))

    def test_dolum_derin_sayfada_girisli_siniri(self):
        self.assertEqual(self.client.get(LISTE, {"page": 30, "page_size": 20},
                                         **DOLUM, **PORTAL).status_code, 200)
