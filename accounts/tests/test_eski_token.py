"""Eski sunucu token'ı koruması (acil yedek sunucu, 2026-11-12).

Yedek DB 11.11 dump'ından geldi; ana sunucunun boşluk saatlerinde verdiği ID'ler yedekte
başka kişilere verildi. Aralıktaki kullanıcıların SINIR'dan önce üretilmiş token'ları
reddedilmeli — yoksa eski token başkasının hesabını açar (bkz. config/settings.py).
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

User = get_user_model()
PROFIL = "/api/v1/auth/profile/"
YENILE = "/api/v1/auth/token/refresh/"
# Sınır GEÇMİŞTE olmalı: PyJWT geleceğe tarihli `iat`'i zaten reddeder, yeni üretilen
# token'lar da sınırdan SONRA sayılmalı.
SINIR = timezone.now() - timedelta(hours=1)
ONCE = int((SINIR - timedelta(hours=20)).timestamp())


class EskiTokenKorumasi(TestCase):
    def setUp(self):
        self.aralikta = User.objects.create_user(username="bosluk", email="b@x.com",
                                                 password="x.Guclu42")
        self.disarida = User.objects.create_user(username="eski", email="e@x.com",
                                                 password="x.Guclu42")
        self.ayar = dict(ESKI_TOKEN_UID_MIN=self.aralikta.id, ESKI_TOKEN_UID_MAX=self.aralikta.id,
                         ESKI_TOKEN_IAT_SINIRI=SINIR)

    @staticmethod
    def _erisim(user, iat=None):
        t = AccessToken.for_user(user)
        if iat is not None:
            t["iat"] = iat
        return {"HTTP_AUTHORIZATION": f"Bearer {t}"}

    @staticmethod
    def _yenileme(user, iat=None):
        t = RefreshToken.for_user(user)
        if iat is not None:
            t["iat"] = iat
        return str(t)

    def _yenile(self, refresh):
        return self.client.post(YENILE, {"refresh": refresh}, content_type="application/json")

    def test_kapaliyken_eski_token_gecer(self):
        self.assertEqual(self.client.get(PROFIL, **self._erisim(self.aralikta, ONCE)).status_code, 200)
        self.assertEqual(self._yenile(self._yenileme(self.aralikta, ONCE)).status_code, 200)

    def test_araliktaki_eski_erisim_tokeni_reddedilir(self):
        with override_settings(**self.ayar):
            r = self.client.get(PROFIL, **self._erisim(self.aralikta, ONCE))
        self.assertEqual(r.status_code, 401, r.content)

    def test_sinirdan_sonraki_token_gecer(self):
        sonra = int((SINIR + timedelta(minutes=1)).timestamp())
        with override_settings(**self.ayar):
            r = self.client.get(PROFIL, **self._erisim(self.aralikta, sonra))
        self.assertEqual(r.status_code, 200, r.content)

    def test_aralik_disi_kullanici_etkilenmez(self):
        with override_settings(**self.ayar):
            r = self.client.get(PROFIL, **self._erisim(self.disarida, ONCE))
        self.assertEqual(r.status_code, 200, r.content)

    def test_eski_yenileme_tokeni_yeni_tokena_donusemez(self):
        # Yenileme iat'i sıfırlar: burada durdurulmasa eski token "yeni" görünürdü.
        with override_settings(**self.ayar):
            r = self._yenile(self._yenileme(self.aralikta, ONCE))
        self.assertEqual(r.status_code, 401, r.content)
        self.assertNotIn("access", r.json().get("data") or {})

    def test_yeni_yenileme_tokeni_calisir(self):
        with override_settings(**self.ayar):
            r = self._yenile(self._yenileme(self.aralikta))
            self.assertEqual(r.status_code, 200, r.content)
            yeni = (r.json().get("data") or r.json())["access"]
            p = self.client.get(PROFIL, HTTP_AUTHORIZATION=f"Bearer {yeni}")
        self.assertEqual(p.status_code, 200, p.content)
