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


class Faz3EylemTests(TestCase):
    """Faz 3 eylemleri: toplu kaydet, klasöre taşı, firma/idare favori, silme."""

    @classmethod
    def setUpTestData(cls):
        from ekap.models import Authority, Contractor

        U = get_user_model()
        cls.user = U.objects.create(email="f3@test.local", username="f3-test")
        cls.firma = Contractor.objects.create(kanonik_ad="ÖRNEK İNŞAAT A.Ş.", arama_norm="ornek insaat")
        cls.idare = Authority.objects.create(detsis_no="12345678", ad="ÖRNEK BELEDİYESİ")

    def setUp(self):
        self.client.force_login(self.user)

    def _calistir(self, tur, params):
        e = _eylem(self.user, tur=tur, params=params)
        return self.client.post(f"/api/v1/assistant/actions/{e.id}/execute/"), e

    def test_toplu_kaydet_tek_kartla_hepsini_yazar(self):
        r, _ = self._calistir("toplu_ihale_kaydet", {
            "klasor": "Yol İşleri",
            "kayitlar": [{"tender_ikn": "2026/1"}, {"tender_ikn": "2026/2"}],
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SavedTender.objects.filter(user=self.user).count(), 2)
        # Klasör YOKSA oluşturulmalı: sessizce Genel'e düşmek asistanı yalancı yapar.
        from tenders.models import TenderGroup

        self.assertTrue(TenderGroup.objects.filter(user=self.user, name="Yol İşleri").exists())

    def test_klasor_limiti_asilirsa_eylem_hata_durumuna_duser(self):
        from tenders.models import MAX_TENDER_GROUPS, TenderGroup

        for i in range(MAX_TENDER_GROUPS):
            TenderGroup.objects.create(user=self.user, name=f"K{i}")
        r, e = self._calistir("ihale_kaydet", {"tender_ikn": "2026/3", "klasor": "Fazladan"})
        self.assertEqual(r.status_code, 400)
        e.refresh_from_db()
        # ⚠️ `bekliyor` KALMAMALI: bu istek tekrarlanınca da başarısız olacak bir iş
        # kuralı reddi; kartı canlı bırakmak kullanıcıya sonuçsuz bir buton bırakır.
        self.assertEqual(e.durum, AssistantAction.Durum.HATA)
        self.assertFalse(SavedTender.objects.filter(tender_ikn="2026/3").exists())

    def test_ihale_tasi_gruba_tasir(self):
        SavedTender.objects.create(user=self.user, tender_ikn="2026/4")
        r, _ = self._calistir("ihale_tasi", {"tender_ikn": "2026/4", "klasor": "Arşiv"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SavedTender.objects.get(tender_ikn="2026/4").group.name, "Arşiv")

    def test_ihale_tasi_kayit_yoksa_hata(self):
        r, e = self._calistir("ihale_tasi", {"tender_ikn": "2026/yok", "klasor": "Arşiv"})
        self.assertEqual(r.status_code, 400)
        e.refresh_from_db()
        self.assertEqual(e.durum, AssistantAction.Durum.HATA)

    def test_firma_takip_ve_idare_favori(self):
        from tenders.models import FavoriteAuthority, FavoriteContractor

        r, _ = self._calistir("firma_takip", {"contractor_id": self.firma.pk, "alarm": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(FavoriteContractor.objects.filter(user=self.user, contractor=self.firma).exists())

        r, _ = self._calistir("idare_favori", {"detsis_no": "12345678", "alarm": True})
        self.assertEqual(r.status_code, 200)
        fav = FavoriteAuthority.objects.get(user=self.user, detsis_no="12345678")
        # Ad sunucuda `ekap.Authority`'den zenginleştirilir, modelin yazdığından değil.
        self.assertEqual(fav.ad, "ÖRNEK BELEDİYESİ")

    def test_kayit_sil_siler_ve_ikinci_onay_sessizce_gecer(self):
        SavedTender.objects.create(user=self.user, tender_ikn="2026/5")
        r, e = self._calistir("kayit_sil", {"tur": "ihale", "anahtar": "2026/5"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(SavedTender.objects.filter(tender_ikn="2026/5").exists())
        # Aynı karta tekrar dokunma → idempotent 200, yeni silme denemesi yok.
        r2 = self.client.post(f"/api/v1/assistant/actions/{e.id}/execute/")
        self.assertEqual(r2.status_code, 200)

    def test_kayit_sil_olmayan_kayitta_patlamaz(self):
        r, _ = self._calistir("kayit_sil", {"tur": "alarm", "anahtar": "yok-boyle"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"]["sonuc"]["adet"], 0)


class KlasorServisTests(TestCase):
    """`klasor_bul_veya_olustur` kuralları `TenderGroupSerializer` ile aynı olmalı."""

    @classmethod
    def setUpTestData(cls):
        U = get_user_model()
        cls.user = U.objects.create(email="k@test.local", username="k-test")

    def test_genel_satir_uretmez(self):
        from tenders.services.actions import klasor_bul_veya_olustur

        self.assertIsNone(klasor_bul_veya_olustur(self.user, "Genel"))
        self.assertIsNone(klasor_bul_veya_olustur(self.user, "genel"))

    def test_turkce_buyuk_kucuk_harf_ayni_klasor(self):
        from tenders.models import TenderGroup
        from tenders.services.actions import klasor_bul_veya_olustur

        TenderGroup.objects.create(user=self.user, name="İşleri")
        g = klasor_bul_veya_olustur(self.user, "işleri")
        # ⚠️ `iexact` Türkçe İ/ı'da çalışmaz; eşleşmezse aynı klasörden iki tane olurdu.
        self.assertEqual(TenderGroup.objects.filter(user=self.user).count(), 1)
        self.assertEqual(g.name, "İşleri")
