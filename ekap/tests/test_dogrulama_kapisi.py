"""
Uyarlanır v2 doğrulama kapısı (`ekap.session`, 2026-10-27).

EKAP insan doğrulamasını kapattı (`enabled: false`) ve aynı gün bayat çerezle giden
istek 428 aldı → kapalıyken çerez GÖNDERİLMEZ. EKAP doğrulamayı geri açabilir →
açıkken köprü çerezi istenir, durum öğrenilemezse istek atılmaz.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from core.models import AppSetting
from ekap import session

KAPALI = {"http": 200, "enabled": False, "verified": True}
ACIK = {"http": 200, "enabled": True, "verified": False}


class DogrulamaKapisiTest(TestCase):
    def setUp(self):
        cache.clear()

    def _cerez_kaydet(self):
        AppSetting.objects.create(key=session.ANAHTAR_CEREZ, value="ekap.human-verification=eski")

    @patch("ekap.session.durum", return_value=KAPALI)
    def test_dogrulama_KAPALIYKEN_cerezsiz_acik_ve_BAYAT_CEREZ_GONDERILMEZ(self, durum):
        self._cerez_kaydet()
        self.assertTrue(session.gecerli())
        self.assertEqual(session.istek_cerezi(), "")
        session.gecerli()
        durum.assert_called_once_with(cerezli=False)       # 5 dk önbellek

    @patch("ekap.session.durum", return_value=ACIK)
    def test_dogrulama_ACIKKEN_kopru_cerezi_gerekir(self, _):
        self.assertFalse(session.gecerli())
        self._cerez_kaydet()
        cache.delete(session._CEREZ_CACHE)
        self.assertTrue(session.gecerli())
        self.assertEqual(session.istek_cerezi(), "ekap.human-verification=eski")

    @patch("ekap.session.durum", side_effect=RuntimeError("ağ yok"))
    def test_durum_OGRENILEMEZSE_kapi_kapali(self, _):
        self.assertFalse(session.gecerli())

    @patch("ekap.session.durum", return_value={"http": 503, "hata": "bakım"})
    def test_durum_ucu_hata_verirse_kapi_kapali(self, _):
        self.assertFalse(session.gecerli())

    @patch("ekap.session.durum")
    def test_428_gorulunce_durum_YENIDEN_sorulur(self, durum):
        durum.return_value = KAPALI
        self.assertTrue(session.gecerli())
        session.dustu("HTTP 428")                           # EKAP doğrulamayı geri açtı
        self.assertFalse(session.gecerli())                  # düştü bayrağı
        cache.delete(session._DUSTU_CACHE)
        durum.return_value = ACIK
        self.assertFalse(session.gecerli())                  # yeni durum: çerez gerekiyor
        self.assertEqual(durum.call_count, 2)

    @patch("ekap.session.durum")
    def test_pano_EKAPa_istek_ATMAZ(self, durum):
        self.assertIsNone(session.dogrulama_gerekli(canli=False))
        durum.assert_not_called()

    @patch("ekap.session.durum", return_value=KAPALI)
    def test_pano_kapi_onbellegi_DUSUNCE_eski_DUSTU_kaydini_gostermez(self, _):
        # 2026-10-28: v2 çalışırken pano dünkü "DÜŞTÜ … 428" kaydını gösteriyordu.
        from datetime import timedelta
        from django.utils import timezone
        from core.dashboard import ekap_dogrulama_durumu
        AppSetting.objects.create(key=session.ANAHTAR_DURUM, value="DÜŞTÜ 2026-10-27 11:34 — HTTP 428")
        self.assertFalse(session.dogrulama_gerekli())          # görev sordu
        cache.delete(session._GEREKLI_CACHE)                    # 5 dk sonra kapı önbelleği düştü
        d = ekap_dogrulama_durumu()
        self.assertEqual(d["seviye"], "ok")
        self.assertIn("son kontrol", d["metin"])
        # Son cevap 2 saatten eskiyse güvenilmez → eski kayda düşer.
        eski = (timezone.now() - timedelta(hours=3)).isoformat()
        cache.set(session._GEREKLI_SON, f"0|{eski}", 3600)
        self.assertEqual(ekap_dogrulama_durumu()["seviye"], "hata")
