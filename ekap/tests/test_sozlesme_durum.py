"""
Sözleşmesi olan ihale "Katılıma Açık" / "Değerlendirmede" kalamaz + kısımlı ihalede
firma ekranından gelinen firmanın sözleşmesi ilk sırada döner.

Üretim vakası (2026-10-30, İKN 2026/1167073): Medtronic'in "yeni iş aldı"
bildiriminden gelinen ihalede ilk kart Disamed'in sözleşmesiydi.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from ekap import sync as sync_mod
from ekap.models import Contract, Contractor, ContractorMembership, Tender


class SozlesmeDurumTest(TestCase):
    def _ihale(self, durum, ikn="2026/1"):
        return Tender.objects.create(ikn=ikn, ekap_id=ikn.replace("/", "-"), ihale_durum=durum)

    def test_SOZLESME_YAZILINCA_durum_5_olur(self):
        t = self._ihale(3)
        sync_mod.sync_contracts_from_raw(t, detail={"item": {"sozlesmeBilgiList": [
            {"id": 1, "yukleniciAdi": "ABC LTD ŞTİ", "sozlesmeBedeliDegeri": 1000.0}]}},
            buda=False)
        t.refresh_from_db()
        self.assertEqual(t.ihale_durum, 5)
        self.assertEqual(t.ihale_durum_aciklama, "Sözleşme İmzalanmış")

    def test_ileri_durumlara_DOKUNMAZ(self):
        for i, durum in enumerate((6, 15, 20, 5)):
            t = self._ihale(durum, ikn=f"2026/{10 + i}")
            Contract.objects.create(tender=t, ekap_sozlesme_id=f"x{i}")
            self.assertFalse(sync_mod.sozlesme_durumunu_duzelt(t))
            t.refresh_from_db()
            self.assertEqual(t.ihale_durum, durum)

    def test_sozlesmesiz_ihale_DEGISMEZ(self):
        t = self._ihale(2)
        self.assertFalse(sync_mod.sozlesme_durumunu_duzelt(t))

    def test_onarim_komutu(self):
        a = self._ihale(4, ikn="2026/20")
        Contract.objects.create(tender=a, ekap_sozlesme_id="a")
        b = self._ihale(None, ikn="2026/21")
        Contract.objects.create(tender=b, ekap_sozlesme_id="b")
        c = self._ihale(3, ikn="2026/22")  # sözleşmesiz → kalır
        call_command("fix_sozlesme_durum", stdout=StringIO())
        durumlar = dict(Tender.objects.values_list("ikn", "ihale_durum"))
        self.assertEqual(durumlar, {"2026/20": 5, "2026/21": 5, "2026/22": 3})


class SozlesmeSirasiTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.t = Tender.objects.create(ikn="2026/1167073", ekap_id="abc", ihale_durum=15,
                                      detail_synced_at=timezone.now())
        cls.disamed = Contractor.objects.create(kanonik_ad="DİSAMED", kanonik_anahtar="disamed")
        cls.medtronic = Contractor.objects.create(kanonik_ad="MEDTRONİC", kanonik_anahtar="medtronic")
        cls.og = Contractor.objects.create(kanonik_ad="X-Y İŞ ORTAKLIĞI", kanonik_anahtar="xy")
        cls.uye = Contractor.objects.create(kanonik_ad="Y", kanonik_anahtar="y")
        ContractorMembership.objects.create(ortak_girisim=cls.og, uye=cls.uye)
        Contract.objects.create(tender=cls.t, ekap_sozlesme_id="d", yuklenici=cls.disamed,
                                sozlesme_bedeli_num=7517500)
        Contract.objects.create(tender=cls.t, ekap_sozlesme_id="o", yuklenici=cls.og,
                                sozlesme_bedeli_num=2000000)
        Contract.objects.create(tender=cls.t, ekap_sozlesme_id="m", yuklenici=cls.medtronic,
                                sozlesme_bedeli_num=1275000)

    def _sira(self, firma=None):
        url = "/api/v1/ekap/tenders/abc/contracts/" + (f"?yuklenici_id={firma}" if firma else "")
        return [c["yuklenici"]["id"] for c in APIClient().get(url).json()["data"]["list"]]

    def test_parametresiz_bedel_sirasi_AYNEN(self):
        self.assertEqual(self._sira(), [self.disamed.pk, self.og.pk, self.medtronic.pk])

    def test_GELINEN_FIRMA_ilk_sirada(self):
        self.assertEqual(self._sira(self.medtronic.pk),
                         [self.medtronic.pk, self.disamed.pk, self.og.pk])

    def test_ortak_girisim_UYESI_de_one_alinir(self):
        self.assertEqual(self._sira(self.uye.pk)[0], self.og.pk)

    def test_gecersiz_parametre_yok_sayilir(self):
        r = APIClient().get("/api/v1/ekap/tenders/abc/contracts/?yuklenici_id=abc")
        self.assertEqual(r.status_code, 200)
