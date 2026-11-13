"""Sunucu dönüşü token eşlemesi (`KimlikEsleme`, 2026-11-13).

Ana sunucu ile acil yedek aynı ID'leri farklı kişilere verdi; dönüşte boşluk kullanıcıları
yeni ID'ye taşındı. Ana sunucunun verdiği token (eski uid + eski iat) HEDEF hesaba
çevrilmeli, aynı uid'li yeni token'lar ise kendi sahibinde kalmalı.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.models import KimlikEsleme

User = get_user_model()
PROFIL = "/api/v1/auth/profile/"
YENILE = "/api/v1/auth/token/refresh/"
# Sınırlar GEÇMİŞTE: PyJWT geleceğe tarihli iat'i reddeder, yeni token'lar sınırdan sonra.
COKME = timezone.now() - timedelta(hours=30)
KORUMA = timezone.now() - timedelta(hours=2)
ESKI = int((COKME - timedelta(hours=3)).timestamp())        # ana sunucunun verdiği
ARADA = int((COKME + timedelta(hours=5)).timestamp())       # çökme ile koruma arası (belirsiz)


def _veri(r):
    j = r.json()
    return j.get("data") or j


class KimlikEslemeTest(TestCase):
    def setUp(self):
        cache.clear()
        # `cakisan`: yedekte bu ID'yi alan kişi; `tasinan`: ana sunucudaki asıl sahibi (yeni ID).
        self.cakisan = User.objects.create_user(username="yedek", email="yedek@x.com", password="x.Guclu42")
        self.tasinan = User.objects.create_user(username="ana", email="ana@x.com", password="x.Guclu42")
        self.koruma = dict(ESKI_TOKEN_UID_MIN=self.cakisan.id, ESKI_TOKEN_UID_MAX=self.cakisan.id,
                           ESKI_TOKEN_IAT_SINIRI=KORUMA)

    def _kural(self, hedef, bas=None, bit=None):
        return KimlikEsleme.objects.create(eski_uid=self.cakisan.id, iat_bas=bas, iat_bit=bit, hedef=hedef)

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

    def test_kural_yokken_davranis_degismez(self):
        r = self.client.get(PROFIL, **self._erisim(self.cakisan, ESKI))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(_veri(r)["email"], "yedek@x.com")

    def test_eski_erisim_tokeni_hedef_hesabi_acar(self):
        self._kural(self.tasinan, bit=COKME)
        r = self.client.get(PROFIL, **self._erisim(self.cakisan, ESKI))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(_veri(r)["email"], "ana@x.com")

    def test_eski_yenileme_tokeni_hedef_icin_yeni_cift_uretir(self):
        self._kural(self.tasinan, bit=COKME)
        eski = self._yenileme(self.cakisan, ESKI)
        r = self._yenile(eski)
        self.assertEqual(r.status_code, 200, r.content)
        yeni = _veri(r)
        self.assertEqual(AccessToken(yeni["access"])["user_id"], self.tasinan.id)
        self.assertEqual(RefreshToken(yeni["refresh"])["user_id"], self.tasinan.id)
        # Yeni çift kuralın dışında: doğal yoldan hedefin kendisi.
        p = self.client.get(PROFIL, HTTP_AUTHORIZATION=f"Bearer {yeni['access']}")
        self.assertEqual(_veri(p)["email"], "ana@x.com")
        # Eski yenileme token'ı tek kullanımlık: kara listeye alındı.
        self.assertEqual(self._yenile(eski).status_code, 401)

    def test_aralik_disindaki_token_kendi_sahibinde_kalir(self):
        self._kural(self.tasinan, bit=COKME)
        r = self.client.get(PROFIL, **self._erisim(self.cakisan))   # şimdi üretilmiş
        self.assertEqual(_veri(r)["email"], "yedek@x.com")
        y = self._yenile(self._yenileme(self.cakisan))
        self.assertEqual(AccessToken(_veri(y)["access"])["user_id"], self.cakisan.id)

    def test_hedefsiz_kural_reddeder(self):
        self._kural(None, bas=COKME, bit=KORUMA)
        self.assertEqual(self.client.get(PROFIL, **self._erisim(self.cakisan, ARADA)).status_code, 401)
        r = self._yenile(self._yenileme(self.cakisan, ARADA))
        self.assertEqual(r.status_code, 401, r.content)
        self.assertNotIn("access", _veri(r))

    def test_kural_ortam_korumasinin_onundedir(self):
        # Koruma bu uid'nin eski token'larını reddeder; kural eski aralığı hedefe bağlar,
        # arada kalan aralık korumaya düşer (yine RED), yeni token doğal geçer.
        self._kural(self.tasinan, bit=COKME)
        with override_settings(**self.koruma):
            r = self.client.get(PROFIL, **self._erisim(self.cakisan, ESKI))
            self.assertEqual(_veri(r)["email"], "ana@x.com")
            self.assertEqual(self.client.get(PROFIL, **self._erisim(self.cakisan, ARADA)).status_code, 401)
            self.assertEqual(_veri(self.client.get(PROFIL, **self._erisim(self.cakisan)))["email"], "yedek@x.com")

    def test_kendine_esleme_korumayi_acar(self):
        # 718 durumu: asıl sahip ID'sini geri aldı → eski token'ı kendine eşlenir.
        self._kural(self.cakisan, bit=COKME)
        with override_settings(**self.koruma):
            r = self.client.get(PROFIL, **self._erisim(self.cakisan, ESKI))
        self.assertEqual(r.status_code, 200, r.content)

    def test_pasif_hedef_reddedilir(self):
        self._kural(self.tasinan, bit=COKME)
        self.tasinan.is_active = False
        self.tasinan.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(PROFIL, **self._erisim(self.cakisan, ESKI)).status_code, 401)
        self.assertEqual(self._yenile(self._yenileme(self.cakisan, ESKI)).status_code, 401)

    def test_kural_eklenince_onbellek_tazelenir(self):
        # Önce kuralsız istek önbelleği (boş liste) doldurur; ORM kaydı önbelleği silmeli.
        self.client.get(PROFIL, **self._erisim(self.cakisan, ESKI))
        self._kural(self.tasinan, bit=COKME)
        r = self.client.get(PROFIL, **self._erisim(self.cakisan, ESKI))
        self.assertEqual(_veri(r)["email"], "ana@x.com")
