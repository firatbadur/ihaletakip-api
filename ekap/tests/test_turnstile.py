"""
EKAP v2 Turnstile otomatik çözücüsü (ekap/turnstile.py + ekap_oturum_daemon, 2026-11-13).

Tarayıcı ve EKAP ağı taklit edilir; burada yalnızca karar, kayıt, pano ve müdahale yolları
sınanır. Gerçek çözüm sunucuda ölçüldü (Xvfb görünür 2/2, headless 600010).
"""
import base64
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AppSetting
from ekap import session, turnstile
from ekap.management.commands.ekap_oturum_daemon import Command as Daemon

SIMDI = timezone.now()


def _karar(**kw):
    varsayilan = dict(simdi=SIMDI, durum={}, otomatik=True, istek=None, gerekli=True,
                      dustu=False, cerez_var=True)
    varsayilan.update(kw)
    return turnstile.karar_ver(**varsayilan)


class KararTest(TestCase):
    def test_elle_istek_otomatik_kapali_ve_geri_cekilmeyi_ezer(self):
        durum = {"sonraki_deneme": (SIMDI + timedelta(minutes=10)).isoformat()}
        k = _karar(istek="admin", otomatik=False, durum=durum, gerekli=False)
        self.assertEqual(k.eylem, "coz")

    def test_otomatik_kapaliyken_cozulmez(self):
        self.assertEqual(_karar(otomatik=False, dustu=True).eylem, "bekle")

    def test_EKAP_dogrulama_istemiyorsa_cozulmez(self):
        # Kapalıyken çerez göndermek 428 tetikliyordu (session.py) — boşuna token yok.
        self.assertEqual(_karar(gerekli=False, cerez_var=False).eylem, "bekle")

    def test_EKAP_durumu_bilinmiyorsa_tahminle_cozulmez(self):
        self.assertEqual(_karar(gerekli=None, cerez_var=False).eylem, "bekle")

    def test_geri_cekilme_suresince_bekler(self):
        durum = {"sonraki_deneme": (SIMDI + timedelta(seconds=90)).isoformat()}
        self.assertEqual(_karar(durum=durum, dustu=True).eylem, "bekle")

    def test_geri_cekilme_dolunca_cozer(self):
        durum = {"sonraki_deneme": (SIMDI - timedelta(seconds=1)).isoformat()}
        self.assertEqual(_karar(durum=durum, cerez_var=False).eylem, "coz")

    def test_gorev_dogrulama_hatasi_gorduyse_bitise_bakmadan_cozer(self):
        durum = {"bitis": (SIMDI + timedelta(minutes=15)).isoformat()}
        self.assertEqual(_karar(durum=durum, dustu=True).eylem, "coz")

    def test_cerez_yoksa_cozer(self):
        self.assertEqual(_karar(cerez_var=False).eylem, "coz")

    def test_bitisi_bilinmeyen_cerez_once_sorulur(self):
        self.assertEqual(_karar(durum={}).eylem, "sor")

    def test_bitise_esikten_az_kaldiysa_yeniler(self):
        durum = {"bitis": (SIMDI + timedelta(seconds=turnstile.YENILEME_ESIGI_SN - 10)).isoformat()}
        self.assertEqual(_karar(durum=durum).eylem, "coz")

    def test_gecerli_cerezde_bekler_ve_esige_gore_uyanir(self):
        durum = {"bitis": (SIMDI + timedelta(minutes=10)).isoformat()}
        k = _karar(durum=durum)
        self.assertEqual(k.eylem, "bekle")
        # 10 dk kalan, eşik 4 dk → ≤60 sn varsayılanla sınırlı
        self.assertEqual(turnstile.bekleme_suresi(k, 60), 60)
        durum = {"bitis": (SIMDI + timedelta(seconds=turnstile.YENILEME_ESIGI_SN + 20)).isoformat()}
        self.assertLessEqual(turnstile.bekleme_suresi(_karar(durum=durum), 60), 21)


class IsoTest(TestCase):
    def test_EKAP_yedi_haneli_kesri_ayristirilir(self):
        dt = turnstile._iso("2026-11-13T13:24:23.3304305+00:00")
        self.assertEqual((dt.hour, dt.minute, dt.second, dt.microsecond), (13, 24, 23, 330430))
        self.assertIsNotNone(dt.tzinfo)

    def test_bos_deger_None(self):
        self.assertIsNone(turnstile._iso(None))


class KayitTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_ardisik_hatada_geri_cekilme_buyur_basari_sifirlar(self):
        beklemeler = []
        for _ in range(7):
            turnstile.hata_kaydet(turnstile.TurnstileHata("x", kod="600010"), 1.0, "test")
            d = turnstile.durum_oku()
            sonraki = turnstile._iso(d["sonraki_deneme"])
            beklemeler.append(round((sonraki - turnstile._iso(d["son_hata"])).total_seconds()))
        self.assertEqual(beklemeler, [60, 120, 240, 480, 900, 900, 900])
        self.assertEqual(turnstile.durum_oku()["ardisik_hata"], 7)
        turnstile.basari_kaydet("2026-11-13T13:24:23.33+00:00", 6.2, "test")
        d = turnstile.durum_oku()
        self.assertEqual(d["ardisik_hata"], 0)
        self.assertIsNone(d["sonraki_deneme"])
        self.assertEqual((d["bugun_basari"], d["bugun_hata"]), (1, 7))

    def test_hata_ekran_goruntusu_saklanir(self):
        turnstile.hata_kaydet(turnstile.TurnstileHata("x", kod="token_yok", ekran=b"PNG!"), 1, "t")
        self.assertEqual(base64.b64decode(turnstile.ekran_oku()), b"PNG!")

    def test_olay_gunlugu_sinirli_ve_yeniden_eskiye(self):
        for i in range(40):
            turnstile.olay_ekle("bilgi", f"olay {i}")
        liste = turnstile.olaylar()
        self.assertEqual(len(liste), 30)
        self.assertEqual(liste[0]["metin"], "olay 39")


class PanoOzetiTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_nabiz_yoksa_hata(self):
        self.assertEqual(turnstile.pano_ozeti()["seviye"], "hata")

    def test_ust_uste_hata_esiginde_hata(self):
        turnstile.kalp_at()
        for _ in range(turnstile.UYARI_ARDISIK):
            turnstile.hata_kaydet(turnstile.TurnstileHata("x", kod="k"), 1, "t")
        oz = turnstile.pano_ozeti()
        self.assertEqual(oz["seviye"], "hata")
        self.assertEqual(oz["son_hata_kodu"], "k")

    def test_otomatik_kapaliysa_uyari(self):
        turnstile.kalp_at()
        turnstile.otomatik_ayarla(False)
        self.assertEqual(turnstile.pano_ozeti()["seviye"], "uyari")

    def test_saglikli(self):
        turnstile.kalp_at()
        bitis = (timezone.now() + timedelta(minutes=15)).isoformat()
        turnstile.basari_kaydet(bitis, 6, "t")
        oz = turnstile.pano_ozeti()
        self.assertEqual((oz["seviye"], oz["gecerli"], oz["bugun_basari"]), ("ok", True, 1))


@override_settings(EKAP_TURNSTILE_SITEKEY="0xYEDEK", EKAP_TURNSTILE_ACTION="yedek")
class CozTest(TestCase):
    def setUp(self):
        cache.clear()

    @patch("ekap.turnstile.site_anahtari", return_value=("0xK", "human_check"))
    @patch("ekap.turnstile.dogrula",
           return_value=("TS01=a; ekap.human-verification=b",
                         {"verified": True, "expiresAtUtc": "2026-11-13T13:24:23.3304305+00:00"}))
    @patch("ekap.turnstile.token_al", return_value="TOKEN")
    def test_basarida_cerez_kaydedilir_ve_dustu_bayragi_kalkar(self, token_al, dogrula, _):
        session.dustu("406")
        sonuc = turnstile.coz("test")
        self.assertTrue(sonuc["ok"])
        token_al.assert_called_once()
        self.assertEqual(token_al.call_args.args, ("0xK", "human_check"))
        dogrula.assert_called_once_with("TOKEN")
        self.assertEqual(AppSetting.get(session.ANAHTAR_CEREZ), "TS01=a; ekap.human-verification=b")
        self.assertFalse(cache.get(session._DUSTU_CACHE))
        self.assertTrue(turnstile.durum_oku()["bitis"].startswith("2026-11-13T13:24"))

    @patch("ekap.turnstile.site_anahtari", return_value=("0xK", "a"))
    @patch("ekap.turnstile.token_al",
           side_effect=turnstile.TurnstileHata("token gelmedi", kod="600010", ekran=b"PNG"))
    def test_tokensiz_turda_hata_kaydi_ve_ekran(self, *_):
        sonuc = turnstile.coz("test")
        self.assertFalse(sonuc["ok"])
        d = turnstile.durum_oku()
        self.assertEqual((d["ardisik_hata"], d["son_hata_kodu"]), (1, "600010"))
        self.assertTrue(turnstile.ekran_oku())
        self.assertIsNone(AppSetting.objects.filter(key=session.ANAHTAR_CEREZ).first())

    @patch("ekap.turnstile.site_anahtari", return_value=("0xK", "a"))
    @patch("ekap.turnstile.token_al", return_value="TOKEN")
    @patch("ekap.turnstile.dogrula",
           side_effect=turnstile.TurnstileHata("red", kod="HUMAN_VERIFICATION_INVALID_TOKEN"))
    def test_gecersiz_tokenda_anahtar_onbellegi_silinir(self, *_):
        cache.set(turnstile._ANAHTAR, "0xESKI|a", 3600)
        turnstile.coz("test")
        self.assertIsNone(cache.get(turnstile._ANAHTAR))

    @patch("ekap.turnstile._kesfet", side_effect=RuntimeError("paket değişti"))
    def test_kesif_cokerse_ayar_yedegi(self, _):
        self.assertEqual(turnstile.site_anahtari(yenile=True), ("0xYEDEK", "yedek"))


class SiteAnahtariRegexTest(TestCase):
    def test_canli_paket_parcasi(self):
        # 2026-11-13 EKAP `common` chunk'ından birebir parça.
        js = ('popupCountdownSeconds:60},turnstile:{enabled:!0,siteKey:"0x4AAAAAAEe5dq36tnojDXA6",'
              'action:"human_check",language:"tr",appearance:"interaction-only"},rawKey:new Uint8Array([1])')
        m = turnstile._SITEKEY_RE.search(js)
        self.assertEqual(m.groups(), ("0x4AAAAAAEe5dq36tnojDXA6", "human_check"))


class DaemonTurTest(TestCase):
    def setUp(self):
        cache.clear()

    @patch("ekap.session.dogrulama_gerekli", return_value=True)
    @patch("ekap.turnstile.coz", return_value={"ok": True})
    def test_panodan_istek_turda_cozulur_ve_tuketilir(self, coz, _):
        turnstile.otomatik_ayarla(False)
        turnstile.istek_birak("admin")
        Daemon()._tur(60)
        coz.assert_called_once()
        self.assertIn("elle istek", coz.call_args.args[0])
        self.assertIsNone(cache.get(turnstile._ISTEK))
        self.assertIsNotNone(turnstile.kalp_oku())

    @patch("ekap.session.dogrulama_gerekli", return_value=True)
    @patch("ekap.session.durum", return_value={"http": 200, "enabled": True, "verified": True,
                                               "expiresAtUtc": "2099-01-01T00:00:00+00:00"})
    @patch("ekap.turnstile.coz")
    def test_disaridan_gelen_gecerli_cerezde_cozulmez_bitis_ogrenilir(self, coz, *_):
        AppSetting.objects.create(key=session.ANAHTAR_CEREZ, value="ekap.human-verification=kopru")
        Daemon()._tur(60)
        coz.assert_not_called()
        self.assertTrue(turnstile.durum_oku()["bitis"].startswith("2099"))


class PanoEkraniTest(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = get_user_model().objects.create_superuser(
            "admin", email="admin@example.com", password="x" * 12)
        self.client.force_login(self.admin)
        self.url = reverse("ekap_dogrulama")

    def test_ekran_acilir_ve_EKAPa_istek_atmaz(self):
        with patch("ekap.session.durum") as durum:
            r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        durum.assert_not_called()
        self.assertContains(r, "Şimdi çöz")

    def test_simdi_coz_istegi_birakir(self):
        self.client.post(self.url, {"eylem": "coz"})
        self.assertTrue(cache.get(turnstile._ISTEK))

    def test_otomatik_kapat_ac(self):
        self.client.post(self.url, {"eylem": "otomatik_kapat"})
        self.assertFalse(turnstile.otomatik_acik())
        self.client.post(self.url, {"eylem": "otomatik_ac"})
        self.assertTrue(turnstile.otomatik_acik())

    @patch("ekap.session.durum", return_value={"http": 200, "enabled": True, "verified": True,
                                               "expiresAtUtc": "2099-01-01T00:00:00+00:00"})
    def test_elle_cerez_kaydedilir_ve_bitis_ogrenilir(self, _):
        self.client.post(self.url, {"eylem": "cerez_kaydet",
                                    "cerez": "_ga=iz; TS01=a; ekap.human-verification=b"})
        self.assertEqual(AppSetting.get(session.ANAHTAR_CEREZ), "TS01=a; ekap.human-verification=b")
        self.assertTrue(turnstile.durum_oku()["bitis"].startswith("2099"))

    def test_staff_olmayan_giremez(self):
        self.client.logout()
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)


class AnaPanoSeridiTest(TestCase):
    def setUp(self):
        cache.clear()

    @patch("ekap.session.dogrulama_gerekli", return_value=True)
    def test_cozucu_oluyse_cerez_varken_bile_kirmizi(self, _):
        from core.dashboard import ekap_dogrulama_durumu
        AppSetting.objects.create(key=session.ANAHTAR_CEREZ, value="ekap.human-verification=x")
        d = ekap_dogrulama_durumu()                 # nabız yok
        self.assertEqual(d["seviye"], "hata")
        self.assertIn("otomatik çözücü", d["metin"])

    @patch("ekap.session.dogrulama_gerekli", return_value=True)
    def test_saglikli_cozucu_ve_cerez_yesil(self, _):
        from core.dashboard import ekap_dogrulama_durumu
        AppSetting.objects.create(key=session.ANAHTAR_CEREZ, value="ekap.human-verification=x")
        turnstile.kalp_at()
        self.assertEqual(ekap_dogrulama_durumu()["seviye"], "ok")
