"""Web portalı için eklenen uçlar: `tenders/stats/` ve `authorities/<detsis_no>/`."""
from datetime import datetime, time, timedelta, timezone as dt_tz

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from ekap.models import Authority, Tender
from ekap.utils import local_day_range

STATS = "/api/v1/ekap/tenders/stats/"


def _t(n, **kw):
    return Tender.objects.create(ikn=f"2026/{n}", ekap_id=f"e{n}", ihale_adi=f"İş {n}", **kw)


class IstatistikTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        simdi = timezone.now()
        bugun = timezone.localdate(simdi)
        # ⚠️ Üretimdeki gibi: `ilan_tarihi` UTC gece yarısı (yerelde 03:00).
        bugun_utc = datetime.combine(bugun, time.min, tzinfo=dt_tz.utc)
        bas, bit = local_day_range(bugun)
        ileri = simdi + timedelta(days=5)
        _t(1, ilan_tarihi=bugun_utc, ihale_tarihi=ileri, ihale_durum=2, sektor="ilac",
           il_id=251)
        _t(2, ilan_tarihi=bugun_utc, ihale_tarihi=ileri, ihale_durum=2, sektor="",
           il_id=251)
        _t(3, ilan_tarihi=bugun_utc - timedelta(days=3), ihale_tarihi=ileri, ihale_durum=2,
           sektor="ilac")
        _t(4, ilan_tarihi=bugun_utc - timedelta(days=9), ihale_tarihi=bit - timedelta(minutes=1),
           ihale_durum=6)  # bugün yapılacak ama iptal → açık değil
        _t(5, ihale_tarihi=simdi - timedelta(days=2), ihale_durum=2)  # süresi geçmiş

    def setUp(self):
        cache.clear()

    def test_sayilar_ve_kiriimlar(self):
        d = self.client.get(STATS).json()["data"]
        self.assertEqual(d["bugun_yayinlanan"], 2)
        self.assertEqual(d["bugun_yapilacak"], 1)
        self.assertIsNone(d["bugun_sonuclanan"])
        self.assertEqual(d["acik_ihale"], 3)
        insaat = next(x for x in d["sektorler"] if x["kod"] == "ilac")
        self.assertEqual((insaat["acik"], insaat["bugun"]), (2, 1))
        # Sınıflandırılmamış gizlenmez (toplamlar tutsun).
        bos = next(x for x in d["sektorler"] if x["kod"] == "")
        self.assertEqual((bos["acik"], bos["bugun"]), (1, 1))
        ist = next(x for x in d["iller"] if x["ekap_il_id"] == 251)
        self.assertEqual((ist["acik"], ist["bugun"]), (2, 2))
        self.assertEqual(len(d["iller"]), 81)
        self.assertEqual(sum(x["acik"] for x in d["sektorler"]), d["acik_ihale"])

    def test_sayilar_liste_ucuyla_tutarli(self):
        d = self.client.get(STATS).json()["data"]
        for ad, params in d["sorgular"].items():
            liste = self.client.get("/api/v1/ekap/tenders/", params).json()["data"]
            self.assertEqual(liste["totalCount"], d[ad], ad)

    def test_onbellekli(self):
        self.client.get(STATS)
        _t(99, ilan_tarihi=Tender.objects.get(ikn="2026/1").ilan_tarihi,
           ihale_tarihi=timezone.now() + timedelta(days=1), ihale_durum=2)
        self.assertEqual(self.client.get(STATS).json()["data"]["bugun_yayinlanan"], 2)


class IdareDetayTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        Authority.objects.create(detsis_no="1", parent_detsis="", ad="KÖK BAKANLIK",
                                 has_items=True, seviye=0)
        Authority.objects.create(detsis_no="74731203", parent_detsis="1", idare_id="555",
                                 ad="İSTANBUL BÜYÜKŞEHİR BELEDİYE BAŞKANLIĞI",
                                 has_items=False, seviye=1)

    def test_detay_ve_yol(self):
        d = self.client.get("/api/v1/ekap/authorities/74731203/").json()["data"]
        self.assertEqual(d["ad"], "İSTANBUL BÜYÜKŞEHİR BELEDİYE BAŞKANLIĞI")
        self.assertEqual(d["idare_id"], "555")
        self.assertEqual(d["path"], ["KÖK BAKANLIK"])

    def test_bilinmeyen_404(self):
        r = self.client.get("/api/v1/ekap/authorities/999/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["message"], "İdare bulunamadı.")

    def test_sabit_yollar_golgelenmez(self):
        self.assertEqual(self.client.get("/api/v1/ekap/authorities/search/",
                                         {"q": "istanbul"}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/ekap/authorities/tree/").status_code, 200)
