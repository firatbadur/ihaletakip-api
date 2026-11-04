"""
Fiyat analizinin `anahtar` kademesi ihale TÜRÜNÜ korumalı.

Kullanıcı bildirdi (2026-11-04): öğretmenevi YAPIM işinin analizine "öğretmen"
geçen hizmet/mal sözleşmeleri karışıyordu. Ölçüm: 50 açık ihalenin 15'inde kümenin
%20+'si farklı türdendi. Keyword katmanı mock'lanır — test yalnızca kademenin
koşulunu sınar.
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from ekap import benchmark
from ekap.models import Contract, Tender


@override_settings(KEYWORD_BENCHMARK_ENABLED=True)
class AnahtarKademesiTurTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hedef = Tender.objects.create(ekap_id="bt-0", ikn="2026/550000",
                                          ihale_adi="Öğretmenevi Yapım İşi", ihale_tip=2)
        cls.yapim = Tender.objects.create(ekap_id="bt-1", ikn="2025/550001",
                                          ihale_adi="Öğretmenevi Yapımı", ihale_tip=2)
        cls.hizmet = Tender.objects.create(ekap_id="bt-2", ikn="2025/550002",
                                           ihale_adi="Öğretmen Servisi", ihale_tip=3)
        for t in (cls.yapim, cls.hizmet):
            Contract.objects.create(tender=t, ihale_tip=t.ihale_tip,
                                    sozlesme_bedeli_num=Decimal("1000"))

    def _kademe(self):
        ids = [self.yapim.pk, self.hizmet.pk]
        with mock.patch("ekap.keywords.kavram_gruplari", return_value=[["a"], ["b"]]), \
             mock.patch("ekap.keywords.capali_benzer_idler", return_value=(ids, ["ogretmen"])):
            return benchmark._keyword_kademesi(self.hedef)

    def test_farkli_turdeki_sozlesme_kumeye_GIRMEZ(self):
        kademe = self._kademe()
        tenderler = set(Contract.objects.filter(kademe.kosul).values_list("tender_id", flat=True))
        self.assertEqual(tenderler, {self.yapim.pk})

    def test_turu_bilinmeyen_ihalede_suzgec_uygulanmaz(self):
        self.hedef.ihale_tip = None
        kademe = self._kademe()
        self.assertEqual(Contract.objects.filter(kademe.kosul).count(), 2)
