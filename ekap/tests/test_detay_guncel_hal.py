"""
İhale detay ucu ham detayı (`detail_raw`) döndürür ama durumu ve sonuç ilanını
sonradan güncelleyen yollar (v2 tazelemesi, mobil durum taraması, 2→3 geçişi)
yalnızca kolona / ilan tablosuna yazar. Uç güncel hâli yanıtın kopyasına yazmalı.

Üretim vakası (2026-10-30, İKN 2026/1167073): liste "Sonuç İlanı Yayımlanmış",
detay "Katılıma Açık"; sonuç ilanı İlan sekmesinde yok.
"""
from datetime import datetime, timezone as dt_timezone

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from ekap.models import Announcement, Tender


def _ham(durum="2", aciklama="İhale İlanı Yayımlanmış, Katılıma Açık", ilanlar=None):
    return {"item": {
        "ikn": "2026/1167073",
        "ihaleDurum": durum,
        "ihaleBilgi": {"ihaleDurum": durum, "ihaleDurumAciklama": aciklama},
        "ilanList": ilanlar if ilanlar is not None else [
            {"id": "7760115", "ilanTip": "1", "ilanTarihi": "2026-08-13T00:00:00",
             "baslik": "İhale İlanı", "veriHtml": "<p>ilan</p>"},
        ],
        "sozlesmeBilgiList": [],
    }}


class DetayGuncelHalTest(TestCase):
    def setUp(self):
        self.t = Tender.objects.create(
            ikn="2026/1167073", ekap_id="abc123",
            ihale_durum=15, ihale_durum_aciklama="Sonuç İlanı Yayımlanmış",
            detail_raw=_ham(), detail_synced_at=timezone.now(),
        )
        Announcement.objects.create(
            tender=self.t, ekap_ilan_id="7760115", ilan_tip=1,
            ilan_tarihi=datetime(2026, 8, 13, tzinfo=dt_timezone.utc),
            baslik="İhale İlanı", veri_html="<p>ilan</p>",
        )
        Announcement.objects.create(
            tender=self.t, ekap_ilan_id="v2:i:4:e5015f252b31", ilan_tip=4,
            ilan_tarihi=datetime(2026, 10, 28, tzinfo=dt_timezone.utc),
            baslik="Sonuç İlanı", veri_html="<p>sonuç</p>",
        )

    def _getir(self):
        r = APIClient().get(f"/api/v1/ekap/tenders/{self.t.ekap_id}/")
        self.assertEqual(r.status_code, 200)
        return r.json()["data"]

    def test_DURUM_kolondan_gelir_ham_detaydan_DEGIL(self):
        d = self._getir()
        self.assertEqual(d["ihaleDurum"], "15")
        self.assertEqual(d["ihaleBilgi"]["ihaleDurum"], "15")
        self.assertEqual(d["ihaleBilgi"]["ihaleDurumAciklama"], "Sonuç İlanı Yayımlanmış")

    def test_SONUC_ILANI_tablodan_eklenir_ve_ilan_tekrarlanmaz(self):
        d = self._getir()
        tipler = [i["ilanTip"] for i in d["ilanList"]]
        self.assertEqual(tipler, ["1", "4"])  # ihale ilanı bir kez, tarih sırasıyla
        self.assertEqual(d["ilanList"][1]["veriHtml"], "<p>sonuç</p>")

    def test_ham_detay_DEGISTIRILMEZ(self):
        self._getir()
        self.t.refresh_from_db()
        self.assertEqual(self.t.detail_raw["item"]["ihaleDurum"], "2")
        self.assertEqual(len(self.t.detail_raw["item"]["ilanList"]), 1)

    def test_kolon_bossa_ham_durum_korunur(self):
        Tender.objects.filter(pk=self.t.pk).update(ihale_durum=None, ihale_durum_aciklama="")
        d = self._getir()
        self.assertEqual(d["ihaleDurum"], "2")
        self.assertEqual(d["ihaleBilgi"]["ihaleDurumAciklama"],
                         "İhale İlanı Yayımlanmış, Katılıma Açık")

    def test_MOBIL_tarih_bicimi_ilani_IKI_KEZ_gostermez(self):
        # Mobil ham detay `30.10.2026 00:00:00` verir, tablo ISO tutar; metin
        # karşılaştırması üretimde 10.540 ihale ilanını iki kez gösterecekti.
        ham = _ham(ilanlar=[{"id": "mobil:i:1", "ilanTip": "1",
                             "ilanTarihi": "13.08.2026 00:00:00", "veriHtml": "<p>ilan</p>"}])
        Tender.objects.filter(pk=self.t.pk).update(detail_raw=ham)
        Announcement.objects.filter(ilan_tip=1).update(ekap_ilan_id="mobil:i:1-baska")
        d = self._getir()
        self.assertEqual([i["ilanTip"] for i in d["ilanList"]], ["1", "4"])
