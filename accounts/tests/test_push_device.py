"""
Push cihaz kaydı sözleşmesi: bir cihaz token'ı en çok bir hesaba aittir.

⚠️ Bu dosyanın var oluş sebebi üretimde yaşanmış bir arızadır (2026-10-05): tek
telefonun FCM token'ı DÖRT hesapta birden kayıtlıydı (uid 3/4/5/309) ve 08:00 özet
görevi kullanıcı grain'inde çalıştığı için aynı telefona üç push düştü. Mükerrer
bildirimden ağırı hesaplar arası sızıntıydı: alarm push'unun başlığı ihale adıdır.

Diş kontrolü: `PushDevice.token`'dan `unique=True` kaldırılırsa ya da
`FCMTokenView`'daki `update_or_create` düz `create`'e çevrilirse ilk test kırılır.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import PushDevice

FCM_URL = "/api/v1/auth/fcm-token/"
LOGOUT_URL = "/api/v1/auth/logout/"
DEACTIVATE_URL = "/api/v1/auth/deactivate/"
TOKEN = "fMEP0vJqR0m2Xy1s_ayni_telefon_token_abc123"


class PushCihazKaydiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        U = get_user_model()
        cls.a = U.objects.create(email="a@test.local", username="a-test")
        cls.b = U.objects.create(email="b@test.local", username="b-test")

    def _kaydet(self, user, token=TOKEN, **ek):
        self.client.force_login(user)
        return self.client.post(FCM_URL, {"fcm_token": token, **ek}, content_type="application/json")

    # ── asıl arıza ─────────────────────────────────────

    def test_ayni_token_ikinci_hesaba_yazilinca_SAHIP_DEGISIR_satir_cogalmaz(self):
        """2026-10-05 arızası: aynı token iki hesapta birden duruyordu."""
        self.assertEqual(self._kaydet(self.a).status_code, 200)
        self.assertEqual(self._kaydet(self.b).status_code, 200)

        self.assertEqual(PushDevice.objects.filter(token=TOKEN).count(), 1)
        self.assertEqual(PushDevice.objects.get(token=TOKEN).user, self.b)
        self.assertFalse(self.a.push_devices.exists())

    def test_db_kisiti_mukerrer_tokeni_engeller(self):
        """Uç atlansa bile (shell, admin, veri aktarımı) DB izin vermemeli."""
        PushDevice.objects.create(user=self.a, token=TOKEN)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PushDevice.objects.create(user=self.b, token=TOKEN)

    # ── normal akışlar ─────────────────────────────────

    def test_ayni_kullanici_tekrar_yazabilir(self):
        """Mobil her açılışta kaydediyor → kendi kaydını silmemeli."""
        self._kaydet(self.a)
        self.assertEqual(self._kaydet(self.a).status_code, 200)
        self.assertEqual(self.a.push_devices.count(), 1)

    def test_bir_kullanici_birden_cok_cihaz_kaydedebilir(self):
        """Telefon + tablet: eski tasarımda ikincisi birincisini eziyordu."""
        self._kaydet(self.a, token="telefon-token")
        self._kaydet(self.a, token="tablet-token")
        self.assertEqual(self.a.push_devices.count(), 2)

    def test_platform_opsiyoneldir(self):
        self._kaydet(self.a, platform="ios")
        self.assertEqual(PushDevice.objects.get(token=TOKEN).platform, "ios")
        self._kaydet(self.b, token="baska-token")
        self.assertEqual(PushDevice.objects.get(token="baska-token").platform, "")

    # ── çıkış / hesap kapatma ──────────────────────────

    def test_cikista_YALNIZCA_o_cihaz_silinir(self):
        """Kullanıcının başka telefonu varsa ondan bildirim almayı kesmemeli."""
        self._kaydet(self.a, token="telefon-token")
        self._kaydet(self.a, token="tablet-token")
        self.client.force_login(self.a)
        self.client.post(
            LOGOUT_URL,
            {"refresh": str(RefreshToken.for_user(self.a)), "fcm_token": "telefon-token"},
            content_type="application/json",
        )
        kalan = list(self.a.push_devices.values_list("token", flat=True))
        self.assertEqual(kalan, ["tablet-token"])

    def test_cikista_gecersiz_refreshte_de_cihaz_silinir(self):
        """Niyet kesin: 400 dönse bile cihaz push almaya devam etmemeli."""
        self._kaydet(self.a)
        self.client.force_login(self.a)
        r = self.client.post(
            LOGOUT_URL,
            {"refresh": "bozuk-token", "fcm_token": TOKEN},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.a.push_devices.exists())

    def test_cikista_token_verilmezse_cihaz_KALIR(self):
        """Eski mobil sürümler fcm_token göndermez — kırılmamalı."""
        self._kaydet(self.a)
        self.client.force_login(self.a)
        self.client.post(
            LOGOUT_URL,
            {"refresh": str(RefreshToken.for_user(self.a))},
            content_type="application/json",
        )
        self.assertTrue(self.a.push_devices.exists())

    def test_baskasinin_cihazi_cikista_silinemez(self):
        self._kaydet(self.b)
        self.client.force_login(self.a)
        self.client.post(
            LOGOUT_URL,
            {"refresh": str(RefreshToken.for_user(self.a)), "fcm_token": TOKEN},
            content_type="application/json",
        )
        self.assertTrue(self.b.push_devices.exists())

    def test_deactivate_tum_cihazlari_siler(self):
        self._kaydet(self.a, token="telefon-token")
        self._kaydet(self.a, token="tablet-token")
        self.client.force_login(self.a)
        self.client.post(DEACTIVATE_URL, {}, content_type="application/json")
        self.assertFalse(self.a.push_devices.exists())

    # ── yarış koşulu ───────────────────────────────────

    def test_yaris_kosulunda_500_DONMEZ(self):
        """İki istek aynı token'ı aynı anda yazarsa ikinci tur yakınsamalı."""
        gercek = PushDevice.objects.update_or_create
        cagri = {"n": 0}

        def ilk_turda_patla(*args, **kwargs):
            cagri["n"] += 1
            if cagri["n"] == 1:
                raise IntegrityError("simüle edilmiş yarış")
            return gercek(*args, **kwargs)

        with patch.object(PushDevice.objects, "update_or_create", side_effect=ilk_turda_patla):
            r = self._kaydet(self.a)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(cagri["n"], 2)
        self.assertEqual(PushDevice.objects.filter(token=TOKEN).count(), 1)
