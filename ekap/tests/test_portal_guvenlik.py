"""Portal isteği #3/#4/#6: belge erişimi, firma maskeleme, bilinmeyen anahtarda 404.

Her blok önce **mobilin bugünkü davranışının kırılmadığını** doğrular.
"""
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from ekap.models import Contract, Contractor, Tender
from ekap.views import _dokuman_onbellek_anahtari, belge_imzasi


def _kullanici(ad, pro=False):
    u = get_user_model().objects.create_user(username=ad, email=f"{ad}@t.local",
                                             password="x")
    if pro:
        u.subscription_tier = "pro"
        u.save(update_fields=["subscription_tier"])
    return u


class BelgeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.t = Tender.objects.create(ikn="2026/123", ekap_id="mobil:2026-123")
        cls.u = _kullanici("belge")

    def setUp(self):
        cache.clear()
        cache.set(_dokuman_onbellek_anahtari("2026/123"), {
            "ihale": [{"ad": "dok.zip", "boyut": 10, "tarih": "", "aciklama": "",
                       "anahtar": "ABC123"}],
            "teknik": [{"dosyaId": 7, "dosyaAdi": "teknik.pdf", "boyut": 5}],
        })

    def _liste(self):
        return self.client.get(f"/api/v1/ekap/tenders/{self.t.ekap_id}/documents/")

    def _indir(self, url):
        p = urlparse(url)
        # Mobil RNFS gibi: BAŞLIKSIZ istek, URL olduğu gibi.
        with mock.patch("ekap.mobil.client.EkapMobilClient") as cli:
            from ekap.mobil.client import MobilYokError

            cli.return_value.dokuman_liste.side_effect = MobilYokError("yok")
            cli.return_value.teknik_sartname_indir.side_effect = MobilYokError("yok")
            self.client.logout()
            return self.client.get(p.path, {k: v[0] for k, v in parse_qs(p.query).items()})

    def test_liste_baglantilari_imzali_sekil_ayni(self):
        self.client.force_login(self.u)
        d = self._liste().json()["data"]
        self.assertEqual(set(d), {"ihale_dokumani", "ihale_dokumanlari",
                                  "teknik_sartnameler", "mesaj"})
        for url in (d["ihale_dokumani"]["url"], d["ihale_dokumanlari"][0]["url"],
                    d["teknik_sartnameler"][0]["url"]):
            self.assertIn("imza=", url)
        self.assertIn("dosya=ABC123", d["ihale_dokumanlari"][0]["url"])

    def test_bayrak_kapali_bugunku_davranis(self):
        """Vars.: anonim liste + imzasız indirme hâlâ çalışır (permission geçer → 404)."""
        self.assertEqual(self._liste().status_code, 200)
        self.assertEqual(
            self._indir(f"http://x/api/v1/ekap/tenders/{self.t.ekap_id}/document/").status_code,
            404)

    @override_settings(EKAP_BELGE_GIRIS_ZORUNLU=True)
    def test_bayrak_acik_tokensiz_401_imzali_basliksiz_indirme_calisir(self):
        self.assertEqual(self._liste().status_code, 401)
        self.assertEqual(self.client.get(
            f"/api/v1/ekap/tenders/{self.t.ekap_id}/document-url/").status_code, 401)
        self.assertEqual(
            self._indir(f"http://x/api/v1/ekap/tenders/{self.t.ekap_id}/document/").status_code,
            401)

        self.client.force_login(self.u)
        url = self._liste().json()["data"]["ihale_dokumanlari"][0]["url"]
        # İmzalı bağlantı başlıksız indirilebilir (mobil RNFS) → permission geçer.
        self.assertEqual(self._indir(url).status_code, 404)

    @override_settings(EKAP_BELGE_GIRIS_ZORUNLU=True)
    def test_imza_baska_ihaleye_ya_da_anonime_gecmez(self):
        diger = Tender.objects.create(ikn="2026/999", ekap_id="e999")
        imza = belge_imzasi(self.u.pk, self.t.ekap_id)
        self.assertEqual(self._indir(
            f"http://x/api/v1/ekap/tenders/{diger.ekap_id}/document/?imza={imza}").status_code,
            401)
        anonim = belge_imzasi(0, self.t.ekap_id)
        self.assertEqual(self._indir(
            f"http://x/api/v1/ekap/tenders/{self.t.ekap_id}/document/?imza={anonim}").status_code,
            401)


class FirmaMaskeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.f = Contractor.objects.create(
            kanonik_ad="YILMAZ", kanonik_anahtar="yilmaz", sozlesme_sayisi=3,
            toplam_sozlesme_bedeli=1000, ortalama_indirim_orani="0.2",
            indirim_orani_ornek_sayisi=2)
        t = Tender.objects.create(ikn="2026/5", ekap_id="e5")
        Contract.objects.create(tender=t, yuklenici=cls.f, sozlesme_bedeli_num=1000,
                                sektor="ilac", il_id=251)
        cls.free = _kullanici("free")
        cls.pro = _kullanici("pro", pro=True)

    def setUp(self):
        cache.clear()

    def test_anonim_ve_free_liste_maskeli_sayilar_acik(self):
        for u in (None, self.free):
            if u:
                self.client.force_login(u)
            d = self.client.get("/api/v1/ekap/contractors/").json()["data"]
            r = d["list"][0]
            self.assertTrue(d["kilitli"])
            self.assertIsNone(r["toplam_sozlesme_bedeli"])
            self.assertIsNone(r["ortalama_indirim_orani"])
            self.assertEqual(r["sozlesme_sayisi"], 3)

    def test_pro_liste_bugunku_gibi(self):
        self.client.force_login(self.pro)
        d = self.client.get("/api/v1/ekap/contractors/").json()["data"]
        self.assertFalse(d["kilitli"])
        self.assertEqual(d["list"][0]["toplam_sozlesme_bedeli"], "1000.00")

    def test_detay_maskeli_sihirbaz_alanlari_korunur(self):
        d = self.client.get(f"/api/v1/ekap/contractors/{self.f.pk}/").json()["data"]
        self.assertTrue(d["kilitli"])
        self.assertIsNone(d["istatistik"]["toplam_sozlesme_bedeli"])
        self.assertIsNone(d["istatistik"]["ortalama_indirim_orani"])
        self.assertEqual(d["istatistik"]["sozlesme_sayisi"], 3)
        sektor = d["dagilim"]["sektor"][0]
        self.assertEqual((sektor["sektor"], sektor["adet"]), ("ilac", 1))
        self.assertIsNone(sektor["toplam_bedel"])

    def test_pro_detay_acik(self):
        self.client.force_login(self.pro)
        d = self.client.get(f"/api/v1/ekap/contractors/{self.f.pk}/").json()["data"]
        self.assertFalse(d["kilitli"])
        self.assertIsNotNone(d["dagilim"]["sektor"][0]["toplam_bedel"])

    def test_sozlesmeler_free_403_pro_200(self):
        url = f"/api/v1/ekap/contractors/{self.f.pk}/contracts/"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "premium_required")
        self.client.force_login(self.pro)
        self.assertEqual(self.client.get(url).status_code, 200)

    @override_settings(EKAP_FIRMA_SUNUCU_MASKE=False)
    def test_bayrakla_geri_alinir(self):
        d = self.client.get("/api/v1/ekap/contractors/").json()["data"]
        self.assertEqual(d["list"][0]["toplam_sozlesme_bedeli"], "1000.00")


class BilinmeyenAnahtarTest(TestCase):
    def test_db_de_olmayan_anahtar_404_ekap_cagrilmaz(self):
        with mock.patch("ekap.client.EkapV2Client") as cli:
            r = self.client.get("/api/v1/ekap/tenders/mobil:2099-1/")
        self.assertEqual(r.status_code, 404)
        cli.assert_not_called()

    def test_kayitli_ama_detaysiz_ihalede_canli_deneme_surer(self):
        Tender.objects.create(ikn="2026/77", ekap_id="e77")
        with mock.patch("ekap.client.EkapV2Client") as cli:
            cli.return_value.get_detail.side_effect = RuntimeError("ekap kapalı")
            self.client.get("/api/v1/ekap/tenders/e77/")
        cli.assert_called_once()

    @override_settings(EKAP_DETAY_BILINMEYEN_CANLI=True)
    def test_bayrakla_eski_davranis(self):
        with mock.patch("ekap.client.EkapV2Client") as cli:
            cli.return_value.get_detail.side_effect = RuntimeError("x")
            self.assertEqual(self.client.get("/api/v1/ekap/tenders/e404/").status_code, 404)
        cli.assert_called_once()
