"""
Firma detayındaki idare kırılımı `detsis_no` taşımalı.

Mobil tanışma sihirbazı firmanın çalıştığı idareleri doğrudan favoriye ekliyor;
`FavoriteAuthority`nin doğal anahtarı `detsis_no` (idare_id DEĞİL). Bu alan
düşerse sihirbaz idareleri sessizce takip edemez.
"""
from django.test import TestCase

from ekap.models import Authority, Contract, Contractor, Tender


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
