"""
Push gönderimi cihaz bazlıdır: aynı telefona iki kez push gitmez, çok cihazlı
kullanıcının her cihazı kendi push'unu alır.

⚠️ Semptomun testi (üretim, 2026-10-05): dört hesap aynı telefonda sırayla giriş
yapmıştı; token dördünde birden kayıtlı kaldığı için 08:00 turunda aynı telefona
ÜÇ push düştü (10/93/84 ihale). Artık `accounts.PushDevice.token` unique ve
`FCMTokenView` cihazı devrediyor → o telefona yalnızca son giriş yapan hesap yazar.

⚠️ Uygulama-içi `Notification` satırları bundan ETKİLENMEZ: her hesap kendi
listesinde eşleşmelerini görmeye devam eder. Kısılan yalnızca push.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import PushDevice
from tenders.services import notify, push

FCM_URL = "/api/v1/auth/fcm-token/"
TELEFON = "ayni-telefonun-tokeni-abc123"


class AyniCihazaTekPushTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        U = get_user_model()
        cls.hesaplar = [
            U.objects.create(email=f"h{i}@test.local", username=f"h{i}-test")
            for i in range(4)
        ]

    def setUp(self):
        # ⚠️ Cache testler arasında PAYLAŞILIR (LocMemCache) ve pacing sayaçları
        # `notif:pushcount:{uid}:{tarih}` anahtarlı; `setUpTestData` kullanıcıları
        # her testte aynı pk'yı aldığı için temizlenmezse sayaç testten teste taşar.
        from django.core.cache import cache

        cache.clear()

    def _kaydet(self, user, token):
        self.client.force_login(user)
        self.client.post(FCM_URL, {"fcm_token": token}, content_type="application/json")

    def test_dort_hesap_ayni_telefonda_AYNI_TOKENE_TEK_push(self):
        """Arızanın birebir yeniden kurulumu."""
        for u in self.hesaplar:                      # sırayla giriş yapılmış gibi
            self._kaydet(u, TELEFON)

        with patch.object(push, "send_fcm", return_value=push.SENT) as gonder:
            for u in self.hesaplar:
                notify.push_to_user(u, title="Size Uygun İhaleler", body="…")

        tokenlar = [c.args[0] for c in gonder.call_args_list]
        # ⚠️ ASIL İDDİA: aynı telefona iki push ASLA gitmez.
        self.assertEqual(tokenlar, [TELEFON])
        self.assertEqual(len(tokenlar), len(set(tokenlar)))
        # Cihaz son giriş yapan hesaba ait
        self.assertEqual(PushDevice.objects.get(token=TELEFON).user, self.hesaplar[-1])

    def test_cok_cihazli_kullanicinin_HER_cihazina_gider(self):
        u = self.hesaplar[0]
        self._kaydet(u, "telefon")
        self._kaydet(u, "tablet")

        with patch.object(push, "send_fcm", return_value=push.SENT) as gonder:
            self.assertTrue(notify.push_to_user(u, title="x"))

        self.assertEqual(sorted(c.args[0] for c in gonder.call_args_list), ["tablet", "telefon"])

    def test_gunluk_limit_cihaz_sayisiyla_CARPILMAZ(self):
        """Bir bildirim tek bir olaydır; iki cihaz limiti ikiye katlamaz."""
        u = self.hesaplar[0]
        self._kaydet(u, "telefon")
        self._kaydet(u, "tablet")

        with patch.object(push, "send_fcm", return_value=push.SENT):
            notify.push_to_user(u, title="x")

        from django.core.cache import cache
        from django.utils import timezone

        anahtar = f"notif:pushcount:{u.pk}:{timezone.localdate().isoformat()}"
        self.assertEqual(cache.get(anahtar), 1)      # 2 değil

    def test_olu_token_YALNIZCA_o_cihazi_siler(self):
        u = self.hesaplar[0]
        self._kaydet(u, "olu-cihaz")
        self._kaydet(u, "canli-cihaz")

        def sahte(token, *a, **k):
            return push.INVALID_TOKEN if token == "olu-cihaz" else push.SENT

        with patch.object(push, "send_fcm", side_effect=sahte):
            self.assertTrue(notify.push_to_user(u, title="x"))   # biri gitti → True

        kalan = list(u.push_devices.values_list("token", flat=True))
        self.assertEqual(kalan, ["canli-cihaz"])

    def test_cihazi_olmayan_kullaniciya_push_denenmez(self):
        with patch.object(push, "send_fcm") as gonder:
            self.assertFalse(notify.push_to_user(self.hesaplar[0], title="x"))
        gonder.assert_not_called()

    def test_tum_cihazlar_oluyse_False_doner(self):
        u = self.hesaplar[0]
        self._kaydet(u, "olu-1")
        self._kaydet(u, "olu-2")

        with patch.object(push, "send_fcm", return_value=push.INVALID_TOKEN):
            self.assertFalse(notify.push_to_user(u, title="x"))

        self.assertFalse(u.push_devices.exists())
