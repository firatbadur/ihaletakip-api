"""Güvenlik katmanı (2026-11-05 kazıyıcı arızası) — kota, sayfa sınırı, Turnstile, engeller.

Her korumanın bir "dişi" vardır: koruma geri alınınca en az bir test kırılmalı.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core import guvenlik
from core.models import ErisimEngeli, GuvenlikOlayi
from core.throttling import DOLUM_KIMLIGI, istemci
from ekap.mobil import kota as K
from ekap.mobil.client import EkapMobilClient, MobilKotaError
from ekap.tests.test_dt_uclar import _dt

ANAHTAR = "portal-sir"
PORTAL = {"HTTP_X_IT_PORTAL_KEY": ANAHTAR}
DOLUM = {"HTTP_X_IT_CACHE_FILL": "1"}
LISTE = "/api/v1/ekap/tenders/"
DT = "/api/v1/ekap/dogrudan-temin/"
User = get_user_model()


def _kullanici(ad="u", *, pro=False, yeni=False, **kw):
    u = User.objects.create_user(username=ad, email=f"{ad}@firma.com.tr", password="Sifre.123",
                                 **kw)
    alan = {}
    if not yeni:
        alan["date_joined"] = timezone.now() - timedelta(days=10)
    if pro:
        alan["subscription_tier"] = "pro"
    if alan:
        User.objects.filter(pk=u.pk).update(**alan)
        u.refresh_from_db()
    return u


class KimlikTest(TestCase):
    @override_settings(PORTAL_KEY=ANAHTAR)
    def test_GIRISLI_istekte_dolum_basligi_yok_sayilir(self):
        """Web token'lı isteğe Cache-Fill koyuyordu → ortak kovaya düşüyordu (o gece 1.584 × 429)."""
        rf = RequestFactory()
        r = rf.get(LISTE, HTTP_X_IT_CLIENT_IP="81.214.75.5", **PORTAL, **DOLUM)
        r.user = _kullanici()
        ist = istemci(r)
        self.assertEqual(ist.kimlik, f"u:{r.user.pk}")
        self.assertEqual(ist.ip, "81.214.75.5")
        anonim = rf.get(LISTE, **PORTAL, **DOLUM)
        from django.contrib.auth.models import AnonymousUser

        anonim.user = AnonymousUser()
        self.assertEqual(istemci(anonim).kimlik, DOLUM_KIMLIGI)


class KotaTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_seviyeler(self):
        self.assertEqual(K.seviye_bul(None), "anonim")
        self.assertEqual(K.seviye_bul(_kullanici("a", yeni=True)), "yeni")
        self.assertEqual(K.seviye_bul(_kullanici("b")), "free")
        self.assertEqual(K.seviye_bul(_kullanici("c", pro=True)), "pro")
        self.assertIsNone(K.seviye_bul(User.objects.create_superuser("s", "s@x.co", "p")))

    def test_VARSAYILAN_kullanici_karari(self):
        t = K.tablo()
        self.assertEqual([t[s]["gun"] for s in ("anonim", "yeni", "free", "pro")],
                         [0, 10, 30, 60])

    def test_gunluk_tavan_ve_iade(self):
        k = K.Kota("u:1", "free", gun=2, saat=10)
        self.assertTrue(k.harca())
        self.assertTrue(k.harca())
        self.assertFalse(k.harca())          # tavan aşıldı, sayaç geri alındı
        self.assertEqual(K.ozet("u:1")[0], 2)
        k.iade()
        self.assertTrue(k.on_kontrol())

    def _cli(self, kota):
        cli = EkapMobilClient(butce="kullanici", kota=kota)
        cli.session = mock.MagicMock()
        cli.session.post.return_value = mock.MagicMock(cookies={}, status_code=200)
        return cli

    @mock.patch("ekap.mobil.client.mobil_session")
    @mock.patch("ekap.mobil.client.throttle")
    def test_kota_dolunca_EKAPa_ve_ortak_butceye_DOKUNULMAZ(self, thr, _ses):
        thr.butce_kalan.return_value = 100
        thr.slot_al.return_value = True
        thr.butce_harca.return_value = True
        cli = self._cli(K.Kota("u:9", "free", gun=1, saat=1))
        cli._ham_istek("/x")
        with self.assertRaises(MobilKotaError):
            cli._ham_istek("/x")
        self.assertEqual(cli.session.post.call_count, 1)
        self.assertEqual(thr.butce_harca.call_count, 1)

    @mock.patch("ekap.mobil.client.mobil_session")
    @mock.patch("ekap.mobil.client.throttle")
    def test_ortak_butce_doluysa_kisinin_hakki_IADE_edilir(self, thr, _ses):
        from ekap.mobil.client import MobilButceError

        thr.butce_kalan.return_value = 100
        thr.slot_al.return_value = True
        thr.butce_harca.return_value = False
        cli = self._cli(K.Kota("u:8", "free", gun=5, saat=5))
        with self.assertRaises(MobilButceError):
            cli._ham_istek("/x")
        self.assertEqual(K.ozet("u:8")[0], 0)

    @mock.patch("ekap.mobil.client.mobil_session")
    @mock.patch("ekap.mobil.client.throttle")
    def test_ANONIM_DT_detayi_EKAPa_gitmez_hata_kodu_doner(self, thr, _ses):
        thr.butce_kalan.return_value = 100
        _dt("26DT7")
        with mock.patch("requests.Session.post") as post:
            d = APIClient().get(DT + "26DT7/").json()["data"]
        post.assert_not_called()
        self.assertIsNone(d["detay"])
        self.assertEqual(d["hata_kodu"], "ekap_quota")
        self.assertTrue(GuvenlikOlayi.objects.filter(tur="ekap_kota").exists())

    @mock.patch("ekap.mobil.client.mobil_session")
    @mock.patch("ekap.mobil.client.throttle")
    def test_DT_dokumani_kota_doluysa_429_ve_baslik(self, thr, _ses):
        thr.butce_kalan.return_value = 100
        _dt("26DT8", dokuman_var=True)
        u = _kullanici("dok")
        K.hazirla(mock.Mock(kimlik=f"u:{u.pk}", uid=None, dolum=False), u)
        cache.set(K._anahtar(f"u:{u.pk}", "g"), 30, 3600)
        c = APIClient()
        c.force_authenticate(u)
        r = c.get(DT + "26DT8/dokuman/")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r["X-IT-Hata"], "ekap_quota")
        self.assertEqual(r.json()["errors"]["code"], "ekap_quota")


@override_settings(PORTAL_KEY=ANAHTAR, API_THROTTLE_PORTAL_MODU="kapali",
                   API_THROTTLE_MODU="kapali", LISTE_SAYFA_SINIRI_PORTAL="uygula",
                   LISTE_SAYFA_SINIRI_DOLUM="kapali", LISTE_SAYFA_SINIRI_DIGER="gozlem",
                   LISTE_SAYFA_SINIRI_MOBIL_MIN_BUILD=500)
class SayfaSiniriTest(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()

    def _get(self, url, page, **meta):
        return self.c.get(url, {"page": page, "page_size": 20}, **meta)

    def test_portal_FREE_4_sayfa_403_page_limit(self):
        self.c.force_authenticate(_kullanici())
        self.assertEqual(self._get(LISTE, 3, **PORTAL).status_code, 200)
        r = self._get(LISTE, 4, **PORTAL)
        self.assertEqual(r.status_code, 403)
        e = r.json()["errors"]
        self.assertEqual((e["code"], e["sebep"], e["azami_sayfa"]),
                         ("premium_required", "page_limit", 3))

    def test_dort_liste_de_sinirli(self):
        self.c.force_authenticate(_kullanici())
        for url in (LISTE, DT, "/api/v1/ekap/contractors/", "/api/v1/ekap/kurul-kararlari/"):
            self.assertEqual(self._get(url, 4, **PORTAL).status_code, 403, url)

    def test_PRO_ve_superuser_serbest(self):
        self.c.force_authenticate(_kullanici(pro=True))
        self.assertEqual(self._get(LISTE, 10, **PORTAL).status_code, 200)
        self.c.force_authenticate(User.objects.create_superuser("s", "s@x.co", "p"))
        self.assertEqual(self._get(LISTE, 10, **PORTAL).status_code, 200)

    def test_free_sayfa_boyutu_20ye_kirpilir(self):
        for i in range(25):
            _dt(f"26DT{100 + i}")
        self.c.force_authenticate(_kullanici())
        r = self.c.get(DT, {"page": 1, "page_size": 100}, **PORTAL)
        self.assertEqual(len(r.json()["data"]["list"]), 20)

    def test_MOBIL_eski_surum_gozlem_yeni_surum_uygula(self):
        self.c.force_authenticate(_kullanici())
        self.assertEqual(self._get(LISTE, 4).status_code, 200)
        self.assertEqual(self._get(LISTE, 4, HTTP_X_IT_APP_BUILD="499").status_code, 200)
        self.assertEqual(self._get(LISTE, 4, HTTP_X_IT_APP_BUILD="500").status_code, 403)

    def test_DOLUM_vars_kapali_acilinca_sinirli(self):
        self.assertEqual(self._get(LISTE, 4, **PORTAL, **DOLUM).status_code, 200)
        with override_settings(LISTE_SAYFA_SINIRI_DOLUM="uygula"):
            self.assertEqual(self._get(LISTE, 4, **PORTAL, **DOLUM).status_code, 403)


SITEVERIFY_TAMAM = {"success": True, "hostname": "ihaletakip.com"}


@override_settings(PORTAL_KEY=ANAHTAR, API_THROTTLE_PORTAL_MODU="uygula",
                   API_THROTTLE_MODU="gozlem", LISTE_SAYFA_SINIRI_PORTAL="kapali",
                   API_THROTTLE_ORANLAR={"ihale_arama": {"anonim": [[60, 1000]],
                                                          "girisli": [[60, 1000]]}},
                   GUVENLIK_DOGRULAMA_MODU="uygula", TURNSTILE_SECRET_KEY="sir",
                   GUVENLIK_DOGRULAMA_ESIK={"anonim": 2, "girisli": 2, "ip": 50})
class InsanDogrulamaTest(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()
        self.ip = {"HTTP_X_IT_CLIENT_IP": "88.241.5.4", **PORTAL}

    def _n(self, n, **meta):
        return [self.c.get(LISTE, **meta).status_code for _ in range(n)]

    def test_esik_asilinca_428(self):
        self.assertEqual(self._n(3, **self.ip), [200, 200, 428])
        r = self.c.get(LISTE, **self.ip)
        self.assertEqual(r.json()["errors"]["code"], "human_verification_required")

    def test_MOBIL_ve_dolum_etkilenmez(self):
        self.assertEqual(self._n(4, HTTP_X_REAL_IP="88.241.5.9"), [200] * 4)
        self.assertEqual(self._n(4, **PORTAL, **DOLUM), [200] * 4)

    def test_sir_yoksa_gozleme_duser(self):
        with override_settings(TURNSTILE_SECRET_KEY=""):
            self.assertEqual(self._n(4, **self.ip), [200] * 4)

    @mock.patch("requests.post")
    def test_dogrulama_gecince_sayac_sifirlanir(self, post):
        post.return_value.json.return_value = SITEVERIFY_TAMAM
        self._n(3, **self.ip)
        r = self.c.post("/api/v1/guvenlik/insan-dogrulama/", {"token": "t"}, **self.ip)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(post.call_args.kwargs["data"]["remoteip"], "88.241.5.4")
        self.assertEqual(self._n(1, **self.ip), [200])

    @mock.patch("requests.post")
    def test_hostname_uyusmazsa_reddedilir(self, post):
        post.return_value.json.return_value = {"success": True, "hostname": "kotu.com"}
        r = self.c.post("/api/v1/guvenlik/insan-dogrulama/", {"token": "t"}, **self.ip)
        self.assertEqual(r.status_code, 400)

    def test_portal_disi_403(self):
        r = self.c.post("/api/v1/guvenlik/insan-dogrulama/", {"token": "t"})
        self.assertEqual(r.status_code, 403)


@override_settings(PORTAL_KEY=ANAHTAR, API_THROTTLE_PORTAL_MODU="kapali",
                   API_THROTTLE_MODU="kapali")
class EngelTest(TestCase):
    def setUp(self):
        cache.clear()
        guvenlik.degisti()
        self.c = APIClient()

    def test_ag_dogrulamasi(self):
        for kotu in ("10.0.0.1", "172.19.0.1", "127.0.0.1", "192.168.1.0/24", "8.0.0.0/8",
                     "abc"):
            with self.assertRaises(ValueError, msg=kotu):
                guvenlik.ag_coz(kotu)
        self.assertEqual(str(guvenlik.ag_coz("81.214.75.0/24")), "81.214.75.0/24")

    def test_IP_engeli_403_ve_kaldirinca_acilir(self):
        e = guvenlik.ip_engelle("81.214.75.0/24", sebep="test")
        r = self.c.get(LISTE, HTTP_X_REAL_IP="81.214.75.7")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "ip_blocked")
        self.assertEqual(self.c.get(LISTE, HTTP_X_REAL_IP="88.241.1.1").status_code, 200)
        # Portal: web kullanıcısının gerçek IP'si (`X-IT-Client-IP`) esas alınır.
        self.assertEqual(self.c.get(LISTE, HTTP_X_IT_CLIENT_IP="81.214.75.9",
                                    HTTP_X_REAL_IP="172.19.0.1", **PORTAL).status_code, 403)
        guvenlik.engeli_kaldir(e)
        self.assertEqual(self.c.get(LISTE, HTTP_X_REAL_IP="81.214.75.7").status_code, 200)

    def test_hesap_engeli_is_active_ve_sure_dolumu(self):
        u = _kullanici("ban")
        e = guvenlik.hesap_engelle(u, sebep="kazıyıcı", sure=timedelta(hours=1))
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        ErisimEngeli.objects.filter(pk=e.pk).update(bitis=timezone.now() - timedelta(seconds=1))
        self.assertEqual(guvenlik.suresi_dolanlari_kaldir(), 1)
        u.refresh_from_db()
        self.assertTrue(u.is_active)

    def test_onceden_PASIF_hesap_kaldirinca_acilmaz(self):
        u = _kullanici("pasif", is_active=False)
        e = guvenlik.hesap_engelle(u)
        guvenlik.engeli_kaldir(e)
        u.refresh_from_db()
        self.assertFalse(u.is_active)

    def test_askidaki_hesap_girisi_account_suspended(self):
        u = _kullanici("girisci")
        guvenlik.hesap_engelle(u)
        r = self.c.post("/api/v1/auth/login/", {"username": u.email, "password": "Sifre.123"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "account_suspended")

    @mock.patch("accounts.views.verify_google_id_token")
    def test_GOOGLE_girisi_askidaki_hesaba_token_vermez(self, ver):
        u = _kullanici("goog")
        guvenlik.hesap_engelle(u)
        ver.return_value = {"email": u.email, "sub": "g-1"}
        r = self.c.post("/api/v1/auth/social/google/", {"id_token": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("access", str(r.json().get("data")))

    def test_tek_kullanimlik_eposta_kaydi_reddedilir(self):
        self.assertTrue(guvenlik.eposta_engelli("ihalecin1793822191233@mailinator.com"))
        self.assertTrue(guvenlik.eposta_engelli("x@alt.yopmail.com"))
        self.assertFalse(guvenlik.eposta_engelli("ali@firma.com.tr"))
        r = self.c.post("/api/v1/auth/email/register/start/",
                        {"email": "bot@mailinator.com", "password": "Guclu.Sifre42"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "disposable_email")

    def test_admin_alani_engeli(self):
        guvenlik.eposta_alani_engelle("spamfirma.net")
        self.assertTrue(guvenlik.eposta_engelli("a@spamfirma.net"))

    def test_guvenlik_paneli_acilir_ve_ip_engeller(self):
        admin = User.objects.create_superuser("adm", "adm@x.co", "p")
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/admin/guvenlik/").status_code, 200)
        r = self.client.post("/admin/guvenlik/", {"tur": "ip", "ip": "88.241.192.77",
                                                   "sure": "24s"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(ErisimEngeli.objects.filter(deger="88.241.192.77", aktif=True).exists())
        # Özel ağ panelden de engellenemez.
        self.client.post("/admin/guvenlik/", {"tur": "ip", "ip": "172.19.0.1", "sure": "24s"})
        self.assertFalse(ErisimEngeli.objects.filter(deger="172.19.0.1").exists())


HEADLESS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "HeadlessChrome/156.0.0.0 Safari/537.36")
CHROME = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/156.0.0.0 Safari/537.36"


class OtomatikAskiTest(TestCase):
    """Açık sinyallerde 24 sa otomatik askı (`guvenlik.otomatik_aski`)."""

    def setUp(self):
        cache.clear()

    def _hesap(self, ad, *, email=None, yeni=True, pro=False):
        u = User.objects.create_user(username=ad, email=email or f"{ad}@firma.com.tr",
                                     password="x")
        alan = {}
        if not yeni:
            alan["date_joined"] = timezone.now() - timedelta(days=10)
        if pro:
            alan["subscription_tier"] = "pro"
        if alan:
            User.objects.filter(pk=u.pk).update(**alan)
            u.refresh_from_db()
        return u

    def _calistir(self, *satirlar):
        with mock.patch("core.guvenlik_izleme.en_yogun", return_value=list(satirlar)):
            return guvenlik.otomatik_aski()

    @staticmethod
    def _satir(u, istek, ua):
        return {"kimlik": f"u:{u.pk}", "istek": istek, "red": 0, "ua": ua, "ip": "81.214.75.7"}

    def test_KAZIYICI_askiya_alinir_24_saat(self):
        u = self._hesap("bot", email="ihalecin1@mailinator.com")
        self.assertEqual(self._calistir(self._satir(u, 600, HEADLESS)), [u.pk])
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        e = ErisimEngeli.objects.get(kullanici=u, aktif=True)
        self.assertIsNone(e.olusturan)
        self.assertAlmostEqual((e.bitis - timezone.now()).total_seconds(), 24 * 3600, delta=60)
        self.assertTrue(GuvenlikOlayi.objects.filter(tur="otomatik_aski", kullanici=u).exists())
        # İkinci tur aynı hesabı tekrar askıya almaz.
        self.assertEqual(self._calistir(self._satir(u, 900, HEADLESS)), [])

    def test_ESKI_hesap_askiya_alinmaz(self):
        eski = self._hesap("eski", email="e@mailinator.com", yeni=False)
        self.assertEqual(self._calistir(self._satir(eski, 3000, HEADLESS)), [])

    def test_PRO_ASLA_askiya_alinmaz_isaretlenir_gunde_bir_olay(self):
        pro = self._hesap("pro", email="p@mailinator.com", pro=True)
        self.assertEqual(self._calistir(self._satir(pro, 3000, HEADLESS)), [])
        self._calistir(self._satir(pro, 3500, HEADLESS))
        pro.refresh_from_db()
        self.assertTrue(pro.is_active)
        self.assertFalse(ErisimEngeli.objects.filter(kullanici=pro).exists())
        self.assertEqual(GuvenlikOlayi.objects.filter(tur="pro_supheli", kullanici=pro).count(), 1)

    def test_ESKI_PRO_da_isaretlenir_panelde_kirmizi(self):
        pro = self._hesap("eskipro", yeni=False, pro=True)
        satir = self._satir(pro, 2500, HEADLESS)    # otomasyon 3 + 2000+ istek 2 = 5
        self._calistir(satir)
        self.assertTrue(GuvenlikOlayi.objects.filter(tur="pro_supheli", kullanici=pro).exists())
        admin = User.objects.create_superuser("adm2", "adm2@x.co", "p")
        self.client.force_login(admin)
        with mock.patch("core.guvenlik_izleme.en_yogun", return_value=[satir]):
            r = self.client.get("/admin/guvenlik/")
        self.assertContains(r, "Pro · askı yok, incele")
        self.assertContains(r, "table-danger")

    def test_normal_tarayicili_yogun_yeni_kullanici_alinmaz(self):
        u = self._hesap("yogun")
        # Yeni hesap (2) + 2000+ istek (2) = 4 < 5
        self.assertEqual(self._calistir(self._satir(u, 2500, CHROME)), [])

    def test_puan_yuksek_ama_kullanim_dusukse_alinmaz_kota_dolunca_alinir(self):
        u = self._hesap("az", email="az@yopmail.com")
        self.assertEqual(self._calistir(self._satir(u, 100, HEADLESS)), [])
        cache.set(K._anahtar(f"u:{u.pk}", "g"), 10, 3600)   # "yeni" günlük kota = 10
        self.assertEqual(self._calistir(self._satir(u, 100, HEADLESS)), [u.pk])

    def test_ayar_kapaliysa_hic_calismaz(self):
        u = self._hesap("kapali", email="k@mailinator.com")
        with override_settings(GUVENLIK_OTOMATIK_ASKI=False):
            self.assertEqual(self._calistir(self._satir(u, 5000, HEADLESS)), [])

    def test_KURU_calistirma_hicbir_sey_yazmaz(self):
        u = self._hesap("kuru", email="kuru@mailinator.com")
        with mock.patch("core.guvenlik_izleme.en_yogun",
                        return_value=[self._satir(u, 600, HEADLESS)]):
            self.assertEqual(guvenlik.otomatik_aski(kuru=True), [u.pk])
        u.refresh_from_db()
        self.assertTrue(u.is_active)
        self.assertFalse(ErisimEngeli.objects.filter(kullanici=u).exists())


# ── Web 8. not: İKN dolum bütçesi (#27), hesap paylaşımı işareti (#28) ────────────
def _ihale(ikn):
    from ekap.models import Tender

    return Tender.objects.create(ikn=ikn, ekap_id=f"e-{ikn.replace('/', '-')}", ihale_adi=ikn,
                                 ihale_durum=2, ihale_tarihi=timezone.now() + timedelta(days=5))


@override_settings(PORTAL_KEY=ANAHTAR, API_THROTTLE_PORTAL_MODU="uygula",
                   API_THROTTLE_MODU="gozlem", LISTE_SAYFA_SINIRI_PORTAL="kapali",
                   LISTE_SAYFA_SINIRI_DOLUM="kapali", GUVENLIK_DOGRULAMA_MODU="kapali",
                   API_THROTTLE_DOLUM_ORANLARI={"ihale_arama": [(60, 2)]},
                   API_THROTTLE_DOLUM_IKN={"bulunan": 100, "bos": 2})
class IknDolumTest(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()
        _ihale("2026/123")

    def _ikn(self, sayi, **meta):
        return self.c.get(LISTE, {"ikn_yil": "2026", "ikn_sayi": sayi, "page_size": 1},
                          **PORTAL, **DOLUM, **meta)

    def test_IKN_aramasi_LISTE_dolum_butcesini_tuketmez(self):
        for _ in range(5):
            r = self._ikn("123")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["data"]["totalCount"], 1)
        self.assertEqual(self.c.get(LISTE, **PORTAL, **DOLUM).status_code, 200)

    def test_yalniz_BOS_ikn_aramasi_yavaslar(self):
        for sayi in ("900", "901"):
            self.assertEqual(self._ikn(sayi).status_code, 200)
        r = self._ikn("902")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["errors"]["code"], "throttled")
        self.assertIn("Retry-After", r)
        # Var olan İKN'yi açan istek etkilenmez.
        self.assertEqual(self._ikn("123").status_code, 200)

    def test_dolum_olmayan_ikn_aramasi_bos_butceye_girmez(self):
        with override_settings(API_THROTTLE_DOLUM_IKN={"bulunan": 0, "bos": 0}):
            r = self.c.get(LISTE, {"ikn_yil": "2026", "ikn_sayi": "999"}, **PORTAL,
                           HTTP_X_IT_CLIENT_IP="81.214.75.9")
        self.assertEqual(r.status_code, 200)

    def test_kapsam_cozumu(self):
        from ekap.views import TenderListView

        rf = RequestFactory()
        v = TenderListView()
        self.assertEqual(v.throttle_kapsam_coz(
            v.initialize_request(rf.get(LISTE, {"ikn_yil": "2026", "ikn_sayi": "1"}))),
            "ikn_arama")
        self.assertEqual(v.throttle_kapsam_coz(
            v.initialize_request(rf.get(LISTE, {"ikn_yil": "2026"}))), "ihale_arama")


class PaylasimPuaniTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_cok_IP_puana_eklenir_ama_otomatik_askiya_sayilmaz(self):
        u = User.objects.create_user(username="pay", email="pay@firma.com.tr", password="x")
        satir = {"kimlik": f"u:{u.pk}", "istek": 600, "red": 0, "ua": CHROME,
                 "ip": "81.214.75.7", "ip_1s": 14, "ua_1s": 6}
        puan, nedenler = guvenlik.supheli_puani(satir, u, 0)
        self.assertEqual(puan, 5)                      # yeni 2 + IP 2 + tarayıcı 1
        self.assertIn("1 saatte 14 farklı IP", nedenler)
        uygun, aski_puan, _ = guvenlik.aski_kosulu(satir, u, 0)
        self.assertFalse(uygun)
        self.assertEqual(aski_puan, 2)
        with mock.patch("core.guvenlik_izleme.en_yogun", return_value=[satir]):
            self.assertEqual(guvenlik.otomatik_aski(), [])


class _SahteRedis:
    """guvenlik_izleme'nin kullandığı Redis komutlarının küçük bir taklidi."""

    def __init__(self):
        self.z, self.h, self.s = {}, {}, {}

    def pipeline(self, transaction=False):
        return self

    def execute(self):
        return []

    def expire(self, k, t):
        pass

    def zincrby(self, k, n, m):
        self.z.setdefault(k, {})[m] = self.z.get(k, {}).get(m, 0) + n

    def zadd(self, k, mapping):
        self.z.setdefault(k, {}).update(mapping)

    def zremrangebyscore(self, k, mn, mx):
        self.z[k] = {m: v for m, v in self.z.get(k, {}).items() if not (mn <= v <= mx)}

    def zcount(self, k, mn, mx):
        return sum(1 for v in self.z.get(k, {}).values() if v >= mn)

    def zrevrange(self, k, a, b, withscores=False):
        return sorted(self.z.get(k, {}).items(), key=lambda x: -x[1])[a:b + 1]

    def zscore(self, k, m):
        return self.z.get(k, {}).get(m)

    def hset(self, k, mapping):
        self.h.setdefault(k, {}).update(mapping)

    def hgetall(self, k):
        return self.h.get(k, {})

    def incr(self, k):
        self.s[k] = self.s.get(k, 0) + 1

    def mget(self, keys):
        return [self.s.get(k) for k in keys]


class IzlemeSayaclariTest(TestCase):
    def setUp(self):
        from core import guvenlik_izleme

        self.iz = guvenlik_izleme
        self.r = _SahteRedis()
        p = mock.patch.object(guvenlik_izleme, "_redis", return_value=self.r)
        p.start()
        self.addCleanup(p.stop)

    def test_hesap_basina_son_1_saatte_farkli_IP_ve_tarayici(self):
        from core.throttling import Istemci

        istek = RequestFactory().get(LISTE)
        for i in range(12):
            ist = Istemci("u:7", True, False, True, ip=f"81.214.75.{i}",
                          ua=f"Chrome/{i % 3}", uid=7)
            self.iz.kaydet(ist, istek)
        self.iz.kaydet(Istemci("ip:81.214.75.1", True, False, False, ip="81.214.75.1"), istek)
        satirlar = {s["kimlik"]: s for s in self.iz.en_yogun()}
        self.assertEqual((satirlar["u:7"]["ip_1s"], satirlar["u:7"]["ua_1s"]), (12, 3))
        self.assertNotIn("ip_1s", satirlar["ip:81.214.75.1"])

    def test_dolum_serisi(self):
        self.iz.dolum_say("ikn_bos")
        self.iz.dolum_say("ikn_bos")
        seri = self.iz.dolum_seri(60)
        self.assertEqual(len(seri["ikn_bos"]), 60)
        self.assertEqual(seri["ikn_bos"][-1], 2)
        self.assertEqual(sum(seri["liste"]), 0)

    def test_panel_dolum_karti_render(self):
        self.iz.dolum_say("ikn_bos")
        admin = User.objects.create_superuser("adm3", "adm3@x.co", "p")
        self.client.force_login(admin)
        r = self.client.get("/admin/guvenlik/")
        self.assertContains(r, "Web önbellek dolumu")
        self.assertContains(r, "İKN araması · boş")
