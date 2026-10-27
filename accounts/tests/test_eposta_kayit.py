"""E-posta ile kayıt (kodla doğrulama) + şifre sıfırlama — accounts/views_eposta.py."""
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import EpostaKodu

User = get_user_model()

KAYIT = "/api/v1/auth/email/register/start/"
KAYIT_DOGRULA = "/api/v1/auth/email/register/verify/"
SIFRE = "/api/v1/auth/password/reset/start/"
SIFRE_DOGRULA = "/api/v1/auth/password/reset/verify/"
SIFRE_KAYDET = "/api/v1/auth/password/reset/confirm/"
GIRIS = "/api/v1/auth/login/"
SIFRE_OK = "Guclu.Sifre42"
GEVSEK = {"kod_gonder": [[600, 1000]], "kod_dogrula": [[600, 1000]], "giris": [[600, 1000]]}


def _son_kod():
    return re.search(r"\b(\d{6})\b", mail.outbox[-1].subject).group(1)


@override_settings(EPOSTA_IP_ORANLARI=GEVSEK, EPOSTA_KOD_TEKRAR_SN=60,
                   EPOSTA_KOD_SAATLIK=5, EPOSTA_KOD_DENEME=5, EPOSTA_GUNLUK_TAVAN=1500)
class _Temel(TestCase):
    def setUp(self):
        cache.clear()

    def post(self, yol, **govde):
        return self.client.post(yol, govde, content_type="application/json")

    def baslat(self, email="Ahmet@Firma.com.tr", password=SIFRE_OK, **ek):
        return self.post(KAYIT, email=email, password=password, **ek)


class KayitTest(_Temel):
    def test_kod_gonderilir_hesap_henuz_acilmaz(self):
        r = self.baslat(display_name="Ahmet Yılmaz")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()["data"]
        self.assertEqual(d, {"email": "ahmet@firma.com.tr", "gecerlilik_sn": 600,
                             "tekrar_gonderim_sn": 60})
        self.assertFalse(User.objects.filter(email__iexact="ahmet@firma.com.tr").exists())
        m = mail.outbox[0]
        self.assertEqual(m.to, ["ahmet@firma.com.tr"])
        kod = _son_kod()
        self.assertIn(kod, m.body)
        self.assertIn(kod, m.alternatives[0][0])
        # Kod düz saklanmaz; bekleyen şifre de yalnızca özet olarak durur.
        satir = EpostaKodu.objects.get()
        self.assertNotIn(kod, satir.kod_hash)
        self.assertNotIn(SIFRE_OK, str(satir.veri))

    def test_dogrulama_hesabi_acar_ve_token_doner(self):
        self.baslat(display_name="Ahmet Yılmaz")
        r = self.post(KAYIT_DOGRULA, email="ahmet@firma.com.tr", code=_son_kod())
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()["data"]
        self.assertTrue(d["created"])
        self.assertTrue(d["access"] and d["refresh"])
        u = User.objects.get(email="ahmet@firma.com.tr")
        self.assertEqual((u.provider, u.display_name), ("email", "Ahmet Yılmaz"))
        self.assertIsNotNone(u.email_verified_at)
        self.assertEqual(EpostaKodu.objects.get().veri, {})
        # Hesap e-posta + şifreyle girebilir (büyük harfli yazımla da).
        r = self.post(GIRIS, email="AHMET@firma.com.tr", password=SIFRE_OK)
        self.assertEqual(r.status_code, 200, r.content)

    def test_kod_tek_kullanimlik(self):
        self.baslat()
        kod = _son_kod()
        self.post(KAYIT_DOGRULA, email="ahmet@firma.com.tr", code=kod)
        r = self.post(KAYIT_DOGRULA, email="ahmet@firma.com.tr", code=kod)
        self.assertEqual(r.json()["errors"]["code"], "code_expired")

    def test_kayitli_eposta(self):
        User.objects.create_user("x", email="ahmet@firma.com.tr", password="a")
        r = self.baslat()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "email_exists")
        self.assertEqual(mail.outbox, [])

    def test_zayif_sifre(self):
        for sifre in ("1234", "12345678", "password"):
            with self.subTest(sifre=sifre):
                r = self.baslat(password=sifre)
                self.assertEqual(r.status_code, 400)
                self.assertIn("password", r.json()["errors"])
        self.assertEqual(mail.outbox, [])

    def test_hatali_kod_deneme_siniri(self):
        self.baslat()
        dogru = _son_kod()
        yanlis = "000000" if dogru != "000000" else "111111"
        for kalan in (4, 3, 2, 1):
            e = self.post(KAYIT_DOGRULA, email="ahmet@firma.com.tr", code=yanlis).json()["errors"]
            self.assertEqual((e["code"], e["kalan_deneme"]), ("code_invalid", kalan))
        e = self.post(KAYIT_DOGRULA, email="ahmet@firma.com.tr", code=yanlis).json()["errors"]
        self.assertEqual(e["code"], "code_attempts")
        # Kilitlendikten sonra doğru kod da kabul edilmez → yeni kod gerekir.
        r = self.post(KAYIT_DOGRULA, email="ahmet@firma.com.tr", code=dogru)
        self.assertEqual(r.json()["errors"]["code"], "code_attempts")
        self.assertFalse(User.objects.exists())

    def test_suresi_dolan_kod(self):
        self.baslat()
        EpostaKodu.objects.update(gecerlilik=timezone.now() - timedelta(seconds=1))
        r = self.post(KAYIT_DOGRULA, email="ahmet@firma.com.tr", code=_son_kod())
        self.assertEqual(r.json()["errors"]["code"], "code_expired")

    def test_yeniden_gonderim_beklemesi(self):
        self.baslat()
        r = self.baslat()
        self.assertEqual(r.status_code, 429)
        e = r.json()["errors"]
        self.assertEqual(e["code"], "code_cooldown")
        self.assertIsInstance(e["retry_after"], int)
        self.assertTrue(r.has_header("Retry-After"))
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EPOSTA_KOD_TEKRAR_SN=0, EPOSTA_KOD_SAATLIK=2)
    def test_saatlik_tavan_ve_yeni_kod_eskiyi_gecersiz_kilar(self):
        self.baslat()
        ilk = _son_kod()
        self.baslat()
        r = self.baslat()
        self.assertEqual(r.json()["errors"]["code"], "code_limit")
        if ilk != _son_kod():
            e = self.post(KAYIT_DOGRULA, email="ahmet@firma.com.tr", code=ilk).json()["errors"]
            self.assertEqual(e["code"], "code_invalid")

    def test_gonderim_hatasi_503_ve_beklemeyi_yemez(self):
        with mock.patch("accounts.services.eposta_kod._postala", side_effect=OSError("smtp")):
            r = self.baslat()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["errors"]["code"], "send_failed")
        self.assertFalse(EpostaKodu.objects.exists())
        self.assertEqual(self.baslat().status_code, 200)

    @override_settings(EPOSTA_GUNLUK_TAVAN=1)
    def test_gunluk_tavan(self):
        self.assertEqual(self.baslat().status_code, 200)
        r = self.baslat(email="baska@firma.com.tr")
        self.assertEqual(r.json()["errors"]["code"], "send_failed")

    @override_settings(EPOSTA_IP_ORANLARI={"kod_gonder": [[600, 2]]})
    def test_ip_siniri_her_zaman_uygulanir(self):
        kodlar = [self.baslat(email=f"k{i}@firma.com.tr").status_code for i in range(3)]
        self.assertEqual(kodlar, [200, 200, 429])

    def test_eski_dogrulamasiz_kayit_kapali(self):
        r = self.post("/api/v1/auth/register/", email="a@b.co", password=SIFRE_OK)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"]["code"], "verification_required")
        with override_settings(EPOSTA_DOGRULAMASIZ_KAYIT=True):
            r = self.post("/api/v1/auth/register/", email="a@b.co", password=SIFRE_OK)
            self.assertEqual(r.status_code, 201)


class SifreSifirlamaTest(_Temel):
    def setUp(self):
        super().setUp()
        self.u = User.objects.create_user("ahmet", email="Ahmet@Firma.com.tr",
                                          password="Eski.Sifre99")

    def _token(self):
        self.post(SIFRE, email="ahmet@firma.com.tr")
        r = self.post(SIFRE_DOGRULA, email="ahmet@firma.com.tr", code=_son_kod())
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["data"]["reset_token"]

    def test_kayitli_olmayan_icin_ayni_yanit_mail_yok(self):
        a = self.post(SIFRE, email="yok@firma.com.tr").json()
        b = self.post(SIFRE, email="ahmet@firma.com.tr").json()
        self.assertEqual(a["message"], b["message"])
        self.assertEqual(set(a["data"]), set(b["data"]))
        self.assertEqual([m.to for m in mail.outbox], [["ahmet@firma.com.tr"]])

    def test_bekleme_sessiz(self):
        self.post(SIFRE, email="ahmet@firma.com.tr")
        r = self.post(SIFRE, email="ahmet@firma.com.tr")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_tam_akis_sifre_degisir_oturumlar_kapanir(self):
        eski = RefreshToken.for_user(self.u)
        r = self.post(SIFRE_KAYDET, reset_token=self._token(), new_password="Yeni.Sifre2027")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["data"]["created"])
        self.u.refresh_from_db()
        self.assertTrue(self.u.check_password("Yeni.Sifre2027"))
        self.assertIsNotNone(self.u.email_verified_at)
        r = self.post("/api/v1/auth/token/refresh/", refresh=str(eski))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.post(GIRIS, email="ahmet@firma.com.tr",
                                   password="Yeni.Sifre2027").status_code, 200)

    def test_token_tek_kullanimlik(self):
        t = self._token()
        self.post(SIFRE_KAYDET, reset_token=t, new_password="Yeni.Sifre2027")
        r = self.post(SIFRE_KAYDET, reset_token=t, new_password="Baska.Sifre2028")
        self.assertEqual(r.json()["errors"]["code"], "reset_token_invalid")

    def test_bozuk_ve_suresi_dolmus_token(self):
        r = self.post(SIFRE_KAYDET, reset_token="sahte", new_password="Yeni.Sifre2027")
        self.assertEqual(r.json()["errors"]["code"], "reset_token_invalid")
        t = self._token()
        with override_settings(EPOSTA_SIFRE_TOKEN_SURE_SN=-1):
            r = self.post(SIFRE_KAYDET, reset_token=t, new_password="Yeni.Sifre2027")
        self.assertEqual(r.json()["errors"]["code"], "reset_token_invalid")

    def test_zayif_yeni_sifre(self):
        r = self.post(SIFRE_KAYDET, reset_token=self._token(), new_password="123")
        self.assertEqual(r.status_code, 400)
        self.assertIn("new_password", r.json()["errors"])

    def test_sosyal_hesap_sifre_belirleyebilir(self):
        g = User.objects.create(username="g", email="g@gmail.com", provider="google")
        g.set_unusable_password()
        g.save()
        self.post(SIFRE, email="g@gmail.com")
        t = self.post(SIFRE_DOGRULA, email="g@gmail.com", code=_son_kod()).json()["data"]["reset_token"]
        self.assertEqual(self.post(SIFRE_KAYDET, reset_token=t,
                                   new_password="Yeni.Sifre2027").status_code, 200)
        self.assertEqual(self.post(GIRIS, email="g@gmail.com",
                                   password="Yeni.Sifre2027").status_code, 200)

    def test_kayit_kodu_sifre_icin_gecmez(self):
        self.baslat(email="yeni@firma.com.tr")
        r = self.post(SIFRE_DOGRULA, email="yeni@firma.com.tr", code=_son_kod())
        self.assertEqual(r.json()["errors"]["code"], "code_expired")


@override_settings(EPOSTA_IP_ORANLARI={"giris": [[600, 2]]})
class GirisSiniriTest(_Temel):
    def test_giris_ip_siniri(self):
        kodlar = [self.post(GIRIS, email="a@b.co", password="x").status_code for _ in range(3)]
        self.assertEqual(kodlar, [401, 401, 429])
