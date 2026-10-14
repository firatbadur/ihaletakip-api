"""
Benzer iş listesi free'de AÇIK, tutarlar MASKELİ (mobil "Benzer İşler" sekmesi).
"""
from django.test import SimpleTestCase

from ekap.views import benzer_satirlari_maskele


class BenzerMaskeleTest(SimpleTestCase):
    def test_isimler_acik_sayilar_maskeli_girdi_degismez(self):
        satir = {
            "ekap_id": "e1", "ikn": "2026/1", "ihale_adi": "Temizlik", "idare_adi": "Belediye",
            "sozlesme_tarihi": "2026-01-02", "sozlesme_bedeli": "100.00",
            "yaklasik_maliyet": "120.00", "indirim_orani": "0.16", "teklif_sayisi": 4,
            "yuklenici": {"id": 1, "ad": "Firma"},
        }
        girdi = [satir]
        cikti = benzer_satirlari_maskele(girdi)
        self.assertEqual(cikti[0]["ihale_adi"], "Temizlik")
        self.assertEqual(cikti[0]["yuklenici"], {"id": 1, "ad": "Firma"})
        for alan in ("sozlesme_bedeli", "yaklasik_maliyet", "indirim_orani", "teklif_sayisi"):
            self.assertIsNone(cikti[0][alan])
        # Önbellekteki nesne bozulmadı (Pro aynı nesneyi görebilir)
        self.assertEqual(girdi[0]["sozlesme_bedeli"], "100.00")

    def test_bos_liste(self):
        self.assertEqual(benzer_satirlari_maskele([]), [])
        self.assertIsNone(benzer_satirlari_maskele(None))
