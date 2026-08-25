"""
Onay kartı uçlarının uçtan uca testleri.

Buradaki her senaryo, kullanıcının verisine yazan bir yolu koruyor. Yazma hataları
geri alınamaz olduğu için bu testler okuma testlerinden daha kritiktir.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from assistant.models import AssistantAction
from tenders.models import SavedTender, TenderAlarm


def _eylem(user, tur="ihale_kaydet", params=None, gun=7):
    return AssistantAction.objects.create(
        user=user,
        tur=tur,
        params=params if params is not None else {"tender_ikn": "2026/999003"},
        ozet="Test önerisi",
        expires_at=timezone.now() + timedelta(days=gun),
    )


class EylemUcuTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        U = get_user_model()
        # `username` unique — iki kullanıcıda da boş kalırsa çakışır.
        cls.user = U.objects.create(email="a@test.local", username="a-test")
        cls.baskasi = U.objects.create(email="b@test.local", username="b-test")

    def setUp(self):
        self.client.force_login(self.user)

    def _url(self, eylem, ne="execute"):
        return f"/api/v1/assistant/actions/{eylem.id}/{ne}/"

    def test_ihale_kaydet_uygulanir(self):
        e = _eylem(self.user)
        r = self.client.post(self._url(e))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(SavedTender.objects.filter(user=self.user, tender_ikn="2026/999003").exists())
        e.refresh_from_db()
        self.assertEqual(e.durum, "onaylandi")
        self.assertIsNotNone(e.executed_at)

    def test_cift_dokunus_ikinci_kayit_uretmez(self):
        """Ağ tekrarı / kullanıcının iki kez basması ikinci bir kayıt yaratmamalı."""
        e = _eylem(self.user)
        self.assertEqual(self.client.post(self._url(e)).status_code, 200)
        r2 = self.client.post(self._url(e))
        self.assertEqual(r2.status_code, 200)  # idempotent, hata DEĞİL
        self.assertEqual(SavedTender.objects.filter(user=self.user).count(), 1)

    def test_suresi_dolmus_oneri_410(self):
        e = _eylem(self.user, gun=-1)
        r = self.client.post(self._url(e))
        self.assertEqual(r.status_code, 410)
        e.refresh_from_db()
        self.assertEqual(e.durum, "suresi_doldu")
        self.assertEqual(SavedTender.objects.count(), 0)

    def test_baskasinin_eylemi_404(self):
        """
        ⚠️ Sahiplik sorgudan gelir. 403 değil 404 döner: 'yetkiniz yok' demek eylemin
        VAR OLDUĞUNU sızdırır.
        """
        e = _eylem(self.baskasi)
        self.assertEqual(self.client.post(self._url(e)).status_code, 404)
        self.assertEqual(SavedTender.objects.count(), 0)

    def test_alarm_free_uyeye_403_ve_eylem_BEKLIYOR_kalir(self):
        """Kullanıcı Pro alıp aynı karta tekrar basabilmeli → durum değişmemeli."""
        e = _eylem(self.user, tur="alarm_kur", params={"tender_id": "abc", "tender_ikn": "2026/9"})
        r = self.client.post(self._url(e))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "premium_required")
        e.refresh_from_db()
        self.assertEqual(e.durum, "bekliyor")
        self.assertEqual(TenderAlarm.objects.count(), 0)

    def test_alarm_pro_uyede_kurulur(self):
        # `is_premium` hesaplanan bir property; katmanı doğrudan set etmek gerekir.
        self.user.subscription_tier = self.user.Tier.PRO
        self.user.save(update_fields=["subscription_tier"])
        e = _eylem(self.user, tur="alarm_kur", params={"tender_id": "abc", "tender_ikn": "2026/9"})
        self.assertEqual(self.client.post(self._url(e)).status_code, 200)
        self.assertTrue(TenderAlarm.objects.filter(user=self.user, tender_id="abc").exists())

    def test_reddetme(self):
        e = _eylem(self.user)
        self.assertEqual(self.client.post(self._url(e, "dismiss")).status_code, 200)
        e.refresh_from_db()
        self.assertEqual(e.durum, "reddedildi")
        self.assertEqual(SavedTender.objects.count(), 0)

    def test_reddedilen_eylem_uygulanamaz(self):
        e = _eylem(self.user)
        self.client.post(self._url(e, "dismiss"))
        r = self.client.post(self._url(e))
        self.assertEqual(r.status_code, 200)  # idempotent yanıt
        self.assertEqual(SavedTender.objects.count(), 0)  # ama YAZILMAZ
