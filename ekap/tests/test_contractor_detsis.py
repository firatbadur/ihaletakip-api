"""
Firma detayındaki idare kırılımı `detsis_no` taşımalı.

Mobil tanışma sihirbazı firmanın çalıştığı idareleri doğrudan favoriye ekliyor;
`FavoriteAuthority`nin doğal anahtarı `detsis_no` (idare_id DEĞİL). Bu alan
düşerse sihirbaz idareleri sessizce takip edemez.
"""
from django.test import TestCase

from ekap.models import Authority, Contract, Contractor, Tender
from ekap.utils import normalize_tr


class FirmaIdareDetsisTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firma = Contractor.objects.create(
            kanonik_ad="YILMAZ INSAAT", kanonik_anahtar="yilmaz-insaat"
        )
        Authority.objects.create(detsis_no="12345678", ad="ANKARA BSB", idare_id="777")
        for i, idare_id in enumerate(["777", "888"]):
            t = Tender.objects.create(
                ekap_id=f"e{i}", ikn=f"2026/{i}", idare_id=idare_id,
                idare_adi=f"IDARE {idare_id}",
            )
            Contract.objects.create(tender=t, yuklenici=cls.firma, idare_id=idare_id)

    def test_detsis_no_cozumlenir_eslesmeyen_none(self):
        data = self.client.get(f"/api/v1/ekap/contractors/{self.firma.pk}/").json()["data"]
        idareler = {r["idare_id"]: r for r in data["dagilim"]["idare"]}
        self.assertEqual(idareler["777"]["detsis_no"], "12345678")
        self.assertEqual(idareler["777"]["ad"], "IDARE 777")
        # DETSIS ağacında karşılığı olmayan idare → None (mobil takip düğmesini kapatır)
        self.assertIsNone(idareler["888"]["detsis_no"])


class SektorUcuTests(TestCase):
    """Sektör listesi + `sektor` arama filtresi (mobil sihirbaz seçimi buna dayanır)."""

    def test_sektor_listesi_ada_gore_sirali_diger_sonda(self):
        data = self.client.get("/api/v1/ekap/sektorler/").json()["data"]
        kodlar = [x["kod"] for x in data]
        self.assertIn("yol_altyapi", kodlar)
        self.assertEqual(kodlar[-1], "diger")  # "Diğer" her zaman sonda
        adlar = [x["ad"] for x in data[:-1]]
        self.assertEqual(adlar, sorted(adlar, key=lambda a: normalize_tr(a)))

    def test_sektor_filtresi_eslesmeyeni_eler(self):
        Tender.objects.create(ekap_id="s1", ikn="2026/900", sektor="yol_altyapi",
                              ihale_adi="ASFALT YAPIM")
        Tender.objects.create(ekap_id="s2", ikn="2026/901", sektor="temizlik_hizmeti",
                              ihale_adi="TEMIZLIK")
        Tender.objects.create(ekap_id="s3", ikn="2026/902", sektor="",
                              ihale_adi="SINIFLANDIRILMAMIS")
        r = self.client.get("/api/v1/ekap/tenders/?sektor=yol_altyapi").json()["data"]
        self.assertEqual([t["ikn"] for t in r["list"]], ["2026/900"])
        # Boş sektör hiçbir seçimde gelmez
        r2 = self.client.get("/api/v1/ekap/tenders/?sektor=yol_altyapi,temizlik_hizmeti").json()["data"]
        self.assertEqual(r2["totalCount"], 2)


class FirmaSektorKiriliminTests(TestCase):
    def test_firma_sektorleri_adet_sirali(self):
        firma = Contractor.objects.create(kanonik_ad="ABC", kanonik_anahtar="abc")
        for i, sektor in enumerate(["yol_altyapi", "yol_altyapi", "temizlik_hizmeti", ""]):
            t = Tender.objects.create(ekap_id=f"x{i}", ikn=f"2026/8{i}")
            Contract.objects.create(tender=t, yuklenici=firma, sektor=sektor)
        data = self.client.get(f"/api/v1/ekap/contractors/{firma.pk}/").json()["data"]
        sektorler = data["dagilim"]["sektor"]
        self.assertEqual(sektorler[0]["sektor"], "yol_altyapi")
        self.assertEqual(sektorler[0]["adet"], 2)
        self.assertEqual(sektorler[0]["ad"], "Yol, Asfalt ve Altyapı")
        self.assertNotIn("", [x["sektor"] for x in sektorler])  # boş sektör listelenmez
