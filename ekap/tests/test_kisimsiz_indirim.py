"""
Kısımsız tek sözleşmeli ihalede sözleşme YM = ihale YM (`sync._kisimsiz_tek_sozlesme`,
2026-10-28). Sonuç ilanı kısım satırı taşımadığı için indirim oranı hiç dolmuyordu.
"""
import copy
import io
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from ekap import v2_tazeleme as v2
from ekap.models import Contract, Tender
from ekap.sync import YM_KAYNAK_IHALE

FIX = Path(__file__).parent / "fixtures"


def _istemci(veri):
    cli = MagicMock()
    cli.build_search_body.side_effect = lambda **kw: kw
    cli.search.return_value = {"list": [veri["liste_satiri"]], "totalCount": 1}
    cli.get_detail.return_value = veri["detay"]
    return cli


class CanliSenkronTest(TestCase):
    """Gerçek v2 yanıtı: 2026/1852675 kısımsız (`kismiIhale: false`), ilanda kısım satırı yok."""

    def setUp(self):
        cache.clear()
        self.veri = json.loads((FIX / "v2_detay_sonuclanmis.json").read_text())

    def _isle(self, kismi_ihale=None, veri=None):
        t = Tender.objects.create(ikn="2026/1852675", ekap_id="mobil:2026-1852675",
                                  ihale_durum=15, kismi_ihale=kismi_ihale)
        v2.isle(t, True, cli=_istemci(veri or self.veri))
        return Contract.objects.get(tender=t)

    def test_kisimsiz_tek_sozlesme_IHALE_YM_ile_indirim_hesaplanir(self):
        # Mobil kaynaklı ihalede kolon boş → v2'nin `kismiIhale`si kullanılır.
        c = self._isle(kismi_ihale=None)
        self.assertEqual(c.yaklasik_maliyet_num, Decimal("812073.33"))
        self.assertEqual(c.yaklasik_maliyet_kaynak, YM_KAYNAK_IHALE)
        self.assertEqual(c.indirim_orani, Decimal("-0.0078"))
        self.assertIsNone(Tender.objects.get(pk=c.tender_id).kismi_ihale)   # kolona yazılmaz

    def test_kisim_beyani_IHALE_YM_yolunu_iptal_etmez(self):
        # Mobil XML'i kısımsız ihalede "kısım YM gösterilmesin" der; bu beyan yalnızca
        # KISIM satırından okunan değer içindir.
        veri = copy.deepcopy(self.veri)
        veri["detay"]["item"]["sozlesmeBilgiList"][0]["kisimYMGosterilsinMi"] = False
        c = self._isle(kismi_ihale=False, veri=veri)
        self.assertEqual(c.yaklasik_maliyet_num, Decimal("812073.33"))
        self.assertEqual(c.indirim_orani, Decimal("-0.0078"))

    def test_kisimli_ihalede_YAZILMAZ(self):
        c = self._isle(kismi_ihale=True)
        self.assertIsNone(c.yaklasik_maliyet_num)
        self.assertIsNone(c.indirim_orani)

    def test_kismi_ihale_BILINMIYORSA_yazilmaz(self):
        veri = copy.deepcopy(self.veri)
        veri["detay"]["item"].pop("kismiIhale")
        c = self._isle(kismi_ihale=None, veri=veri)
        self.assertIsNone(c.yaklasik_maliyet_num)


class OnarimKomutuTest(TestCase):
    def _s(self, ikn, kismi=False, n=1, ilan="v2:i:4:x", ym=None):
        t = Tender.objects.create(ikn=ikn, ekap_id=ikn, kismi_ihale=kismi)
        for i in range(n):
            Contract.objects.create(tender=t, ekap_sozlesme_id=f"{ikn}:{i}", ekap_ilan_id=ilan,
                                    tender_yaklasik_maliyet_num=Decimal("1000"),
                                    sozlesme_bedeli_num=Decimal("900"), yaklasik_maliyet_num=ym)
        return t

    def test_yalnizca_kisimsiz_tek_sozlesmeli_ilanli_satirlar(self):
        hedef = self._s("kisimsiz")
        self._s("kisimli", kismi=True)
        self._s("bilinmiyor", kismi=None)
        self._s("iki_sozlesme", n=2)
        self._s("ilansiz", ilan="")
        self._s("zaten_dolu", ym=Decimal("950"))
        call_command("fix_kisimsiz_indirim", "--dry-run", stdout=io.StringIO())
        self.assertFalse(Contract.objects.filter(yaklasik_maliyet_kaynak=YM_KAYNAK_IHALE).exists())
        call_command("fix_kisimsiz_indirim", parca=2, stdout=io.StringIO())
        dolan = Contract.objects.filter(yaklasik_maliyet_kaynak=YM_KAYNAK_IHALE)
        self.assertEqual(list(dolan.values_list("tender_id", flat=True)), [hedef.pk])
        self.assertEqual(dolan.get().indirim_orani, Decimal("0.1000"))
        self.assertEqual(Contract.objects.get(tender__ikn="zaten_dolu").yaklasik_maliyet_num,
                         Decimal("950"))
