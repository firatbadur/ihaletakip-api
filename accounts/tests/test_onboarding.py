"""
Tanışma sihirbazı sözleşmesi: profil ucundan ad/yaş/durum yazılabilmesi,
tamamlanma damgası ve sosyal girişte ad doldurma.

Mobil `Onboarding` ekranı bu alanlara dayanır; biri kırılırsa sihirbaz her
açılışta yeniden çıkar ya da ad alanları boş gelir.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.managers import split_full_name

URL = "/api/v1/auth/profile/"


class ProfilSihirbazAlanlariTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create(
            email="onb@test.local", username="onb-test"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _patch(self, body):
        return self.client.patch(URL, body, content_type="application/json")

    def test_yeni_kullanici_pending_baslar(self):
        data = self.client.get(URL).json()["data"]
        self.assertEqual(data["onboarding_status"], "pending")
        self.assertIsNone(data["onboarding_completed_at"])
        for alan in ("first_name", "last_name", "age_range"):
            self.assertIn(alan, data)

    def test_tamamlama_ad_yas_yazar_ve_damgalar(self):
        r = self._patch({
            "first_name": "Fırat",
            "last_name": "Badur",
            "age_range": "25_34",
            "onboarding_status": "completed",
        })
        self.assertEqual(r.status_code, 200, r.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Fırat")
        self.assertEqual(self.user.last_name, "Badur")
        self.assertEqual(self.user.age_range, "25_34")
        self.assertEqual(self.user.onboarding_status, "completed")
        ilk = self.user.onboarding_completed_at
        self.assertIsNotNone(ilk)

        # Yeniden tamamlama ilk tarihi ezmez
        self._patch({"onboarding_status": "completed"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.onboarding_completed_at, ilk)

    def test_atlama_damgalamaz(self):
        self._patch({"onboarding_status": "skipped"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.onboarding_status, "skipped")
        self.assertIsNone(self.user.onboarding_completed_at)

    def test_gecersiz_deger_reddedilir(self):
        self.assertEqual(self._patch({"age_range": "99"}).status_code, 400)
        self.assertEqual(self._patch({"onboarding_status": "bitti"}).status_code, 400)

    def test_tamamlanma_tarihi_istemciden_yazilamaz(self):
        self._patch({"onboarding_completed_at": "2020-01-01T00:00:00Z"})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.onboarding_completed_at)


class SosyalGirisAdTests(TestCase):
    def test_ad_bolme(self):
        self.assertEqual(split_full_name("Ahmet Mehmet Yılmaz"), ("Ahmet Mehmet", "Yılmaz"))
        self.assertEqual(split_full_name("Ahmet"), ("Ahmet", ""))
        self.assertEqual(split_full_name("  "), ("", ""))

    def test_google_ad_soyad_doldurur(self):
        U = get_user_model()
        user, created = U.objects.get_or_create_social(
            email="g@test.local", provider="google", provider_uid="g1",
            display_name="Ayşe Nur Kaya", first_name="Ayşe Nur", last_name="Kaya",
        )
        self.assertTrue(created)
        self.assertEqual((user.first_name, user.last_name), ("Ayşe Nur", "Kaya"))

    def test_apple_tam_addan_bolunur(self):
        U = get_user_model()
        user, _ = U.objects.get_or_create_social(
            email="a@test.local", provider="apple", provider_uid="a1",
            display_name="Can Demir",
        )
        self.assertEqual((user.first_name, user.last_name), ("Can", "Demir"))

    def test_mevcut_ad_ezilmez_bos_ad_doldurulur(self):
        U = get_user_model()
        dolu = U.objects.create(
            email="d@test.local", username="d", provider="google", provider_uid="d1",
            first_name="Düzeltilmiş", last_name="Ad",
        )
        U.objects.get_or_create_social(
            email="d@test.local", provider="google", provider_uid="d1",
            first_name="Google", last_name="Adı",
        )
        dolu.refresh_from_db()
        self.assertEqual((dolu.first_name, dolu.last_name), ("Düzeltilmiş", "Ad"))

        bos = U.objects.create(
            email="b@test.local", username="b", provider="google", provider_uid="b1",
        )
        U.objects.get_or_create_social(
            email="b@test.local", provider="google", provider_uid="b1",
            first_name="Berk", last_name="Ak",
        )
        bos.refresh_from_db()
        self.assertEqual((bos.first_name, bos.last_name), ("Berk", "Ak"))
