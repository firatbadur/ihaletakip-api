"""Web → mobil geçiş kodu — accounts/views_handoff.py + services/web_handoff.py."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.models import WebHandoff

User = get_user_model()

OLUSTUR = "/api/v1/auth/handoff/"
OKUTULDU = "/api/v1/auth/handoff/okutuldu/"
BOZDUR = "/api/v1/auth/handoff/redeem/"
GEVSEK = {"handoff_olustur": [[600, 1000]], "handoff_bozdur": [[600, 1000]]}


@override_settings(WEB_HANDOFF_IP_ORANLARI=GEVSEK, WEB_HANDOFF_SURE_DK=15,
                   WEB_HANDOFF_AKTIF_AZAMI=5, WEB_HANDOFF_KISA_KURESEL_HATA=300,
                   PORTAL_KEY="portal-sir")
class _Temel(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="ahmet", email="ahmet@firma.com.tr",
                                             password="x.Guclu42")
        self.baska = User.objects.create_user(username="mehmet", email="m@firma.com.tr",
                                              password="x.Guclu42")

    def _yetki(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}

    def post(self, yol, govde=None, **ek):
        return self.client.post(yol, govde or {}, content_type="application/json", **ek)

    def olustur(self, user=None, baglam="market"):
        r = self.post(OLUSTUR, {"baglam": baglam}, **self._yetki(user or self.user))
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["data"]

    def bozdur(self, **govde):
        return self.post(BOZDUR, govde)

    def durum(self, hid, user=None):
        return self.client.get(f"{OLUSTUR}{hid}/", **self._yetki(user or self.user))


class OlusturTest(_Temel):
    def test_giris_zorunlu(self):
        self.assertEqual(self.post(OLUSTUR, {"baglam": "market"}).status_code, 401)

    def test_kod_doner_yalnizca_ozet_saklanir(self):
        d = self.olustur()
        self.assertEqual(len(d["kisa_kod"]), 7)
        self.assertGreaterEqual(len(d["kod"]), 40)
        self.assertEqual((d["baglam"], d["gecerlilik_sn"]), ("market", 900))
        satir = WebHandoff.objects.get(pk=d["id"])
        self.assertEqual(satir.durum, "bekliyor")
        self.assertNotIn(d["kod"], satir.kod_hash)
        self.assertNotIn(d["kisa_kod"], satir.kisa_hash)

    def test_gecersiz_baglam_bos_sayilir(self):
        self.assertEqual(self.olustur(baglam="<script>")["baglam"], "")

    def test_aktif_kod_tavani_en_eskiyi_iptal_eder(self):
        idler = [self.olustur()["id"] for _ in range(6)]
        durumlar = dict(WebHandoff.objects.values_list("pk", "durum"))
        self.assertEqual(durumlar[idler[0]], "iptal")
        self.assertTrue(all(durumlar[i] == "bekliyor" for i in idler[1:]))


class DurumTest(_Temel):
    def test_sahibi_gorur_baskasi_404(self):
        d = self.olustur()
        r = self.durum(d["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"]["durum"], "bekliyor")
        self.assertFalse(r.json()["data"]["is_premium"])
        self.assertEqual(self.durum(d["id"], self.baska).status_code, 404)

    def test_suresi_dolan_hesaplanir(self):
        d = self.olustur()
        WebHandoff.objects.filter(pk=d["id"]).update(
            gecerlilik=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.durum(d["id"]).json()["data"]["durum"], "suresi_doldu")

    def test_pro_olunca_is_premium(self):
        d = self.olustur()
        User.objects.filter(pk=self.user.pk).update(subscription_tier=User.Tier.PRO)
        self.assertTrue(self.durum(d["id"]).json()["data"]["is_premium"])

    def test_iptal_idempotent_ve_bozdurulamaz(self):
        d = self.olustur()
        for _ in range(2):
            r = self.client.delete(f"{OLUSTUR}{d['id']}/", **self._yetki(self.user))
            self.assertEqual(r.status_code, 204)
        self.assertEqual(self.durum(d["id"]).json()["data"]["durum"], "iptal")
        self.assertEqual(self.bozdur(kod=d["kod"]).status_code, 400)


class OkutulduTest(_Temel):
    def test_portal_anahtari_zorunlu(self):
        d = self.olustur()
        r = self.post(OKUTULDU, {"kod": d["kod"]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "portal_required")
        self.assertEqual(WebHandoff.objects.get(pk=d["id"]).durum, "bekliyor")

    def test_isaretler_ve_baglam_doner(self):
        d = self.olustur()
        r = self.post(OKUTULDU, {"kod": d["kod"]}, HTTP_X_IT_PORTAL_KEY="portal-sir")
        self.assertEqual(r.json()["data"], {"gecerli": True, "baglam": "market"})
        self.assertEqual(self.durum(d["id"]).json()["data"]["durum"], "okutuldu")
        # Okutulmuş kod hâlâ bozdurulabilir.
        self.assertEqual(self.bozdur(kod=d["kod"]).status_code, 200)

    def test_bilinmeyen_ve_suresi_dolan(self):
        r = self.post(OKUTULDU, {"kod": "yok"}, HTTP_X_IT_PORTAL_KEY="portal-sir")
        self.assertEqual(r.json()["data"], {"gecerli": False, "baglam": ""})
        d = self.olustur()
        WebHandoff.objects.filter(pk=d["id"]).update(
            gecerlilik=timezone.now() - timedelta(seconds=1))
        r = self.post(OKUTULDU, {"kod": d["kod"]}, HTTP_X_IT_PORTAL_KEY="portal-sir")
        self.assertEqual(r.json()["data"], {"gecerli": False, "baglam": "market"})
        self.assertEqual(WebHandoff.objects.get(pk=d["id"]).durum, "bekliyor")


class BozdurTest(_Temel):
    def test_qr_kodu_ayni_hesabin_oturumunu_acar(self):
        d = self.olustur()
        r = self.bozdur(kod=d["kod"])
        self.assertEqual(r.status_code, 200, r.content)
        v = r.json()["data"]
        self.assertEqual(int(AccessToken(v["access"])["user_id"]), self.user.pk)
        self.assertEqual(v["user"]["id"], self.user.pk)
        self.assertFalse(v["created"])
        self.assertEqual((v["baglam"], v["maskeli_eposta"]), ("market", "a***@firma.com.tr"))
        self.assertEqual(self.durum(d["id"]).json()["data"]["durum"], "kullanildi")

    def test_tek_kullanimlik(self):
        d = self.olustur()
        self.assertEqual(self.bozdur(kod=d["kod"]).status_code, 200)
        r = self.bozdur(kod=d["kod"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "handoff_invalid")
        self.assertEqual(self.bozdur(kisa_kod=d["kisa_kod"]).status_code, 400)

    def test_kisa_kod_tire_bosluk_kucuk_harf_kabul(self):
        d = self.olustur()
        k = d["kisa_kod"].lower()
        r = self.bozdur(kisa_kod=f" {k[:3]}-{k[3:]} ")
        self.assertEqual(r.status_code, 200, r.content)

    def test_suresi_dolan_reddedilir(self):
        d = self.olustur()
        WebHandoff.objects.filter(pk=d["id"]).update(
            gecerlilik=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.bozdur(kod=d["kod"]).status_code, 400)
        self.assertEqual(self.bozdur(kisa_kod=d["kisa_kod"]).status_code, 400)

    def test_pasif_hesap_reddedilir(self):
        d = self.olustur()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.bozdur(kod=d["kod"]).status_code, 400)

    def test_tam_olarak_bir_alan(self):
        d = self.olustur()
        self.assertEqual(self.bozdur().status_code, 400)
        self.assertEqual(self.bozdur(kod=d["kod"], kisa_kod=d["kisa_kod"]).status_code, 400)

    def test_bayat_authorization_basligi_401_dondurmez(self):
        # Mobil eski hesabın süresi dolmuş token'ını taşıyor olabilir; 401 → oturum kapanırdı.
        d = self.olustur()
        r = self.post(BOZDUR, {"kod": d["kod"]}, HTTP_AUTHORIZATION="Bearer bozuk.token.x")
        self.assertEqual(r.status_code, 200, r.content)

    @override_settings(WEB_HANDOFF_KISA_KURESEL_HATA=2)
    def test_kuresel_sigorta_kisa_kodu_kapatir_qr_acik_kalir(self):
        d = self.olustur()
        for _ in range(2):
            self.assertEqual(self.bozdur(kisa_kod="ZZZZZZZ").status_code, 400)
        r = self.bozdur(kisa_kod=d["kisa_kod"])
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["errors"]["code"], "handoff_busy")
        self.assertEqual(self.bozdur(kod=d["kod"]).status_code, 200)

    @override_settings(WEB_HANDOFF_IP_ORANLARI={"handoff_bozdur": [[600, 3]]})
    def test_ip_siniri(self):
        for _ in range(3):
            self.assertEqual(self.bozdur(kisa_kod="ZZZZZZZ").status_code, 400)
        self.assertEqual(self.bozdur(kisa_kod="ZZZZZZZ").status_code, 429)
