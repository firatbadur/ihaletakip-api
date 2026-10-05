"""
Mobil çekmeli döngünün iş sırası — sonuç ilanı ve tazeleme.

⚠️ Bu dosyanın var oluş sebebi üretim arızasıdır (2026-10-05): `_sirada_sonuc`
denormalize `sozlesme_sayisi=0` sayacına bakıyordu; sayacı bayat (0) ama v2 sözleşmesi
olan bir ihale her tikte YENİDEN seçildi, `sonuc()` onu atlayıp işareti sildi ve
tazelemeye hiç sıra gelmedi. Teklif tarihi geçmiş 27.691 ihale "Katılıma Açık" kaldı.

Diş kontrolü: `_sirada_sonuc`taki `Exists(Contract…)` dışlaması geri alınınca
`test_BAYAT_SAYACLI_ihale_sonuc_sirasina_girmez` kırılır.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from ekap import sync as sync_mod
from ekap.mobil import tasks as T
from ekap.models import Contract, Tender
from tenders.models import SavedTender

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _ihale(ikn, *, durum=2, gun_once=10, senkron_gun_once=5, sozlesme_sayisi=0):
    simdi = timezone.now()
    return Tender.objects.create(
        ikn=ikn, ekap_id=f"e-{ikn}", ihale_adi=f"İş {ikn}", ihale_durum=durum,
        ihale_tarihi=simdi - timedelta(days=gun_once),
        detail_synced_at=simdi - timedelta(days=senkron_gun_once),
        sozlesme_sayisi=sozlesme_sayisi,
    )


@override_settings(CACHES=_LOCMEM)
class SonucSirasiTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_BAYAT_SAYACLI_ihale_sonuc_sirasina_girmez(self):
        """Asıl arıza: sayaç 0 ama Contract satırı var → sıraya girmemeli."""
        t = _ihale("2026/1309012", durum=15, sozlesme_sayisi=0)
        Contract.objects.create(tender=t, ekap_sozlesme_id="v2-123")
        self.assertIsNone(T._sirada_sonuc())

    def test_sozlesmesiz_sonuclanmis_ihale_secilir(self):
        _ihale("2026/1", durum=15)
        self.assertEqual(T._sirada_sonuc(), "2026/1")

    def test_pencere_disindaki_arsiv_secilmez(self):
        _ihale("2019/1", durum=15, gun_once=2000)
        self.assertIsNone(T._sirada_sonuc())

    def test_v2_sozlesmesi_var_atlamasi_ayni_ihaleyi_TEKRAR_sectirmez(self):
        """Savunma katmanı: sorgu kaçırsa bile işaret silinmemeli."""
        t = _ihale("2026/7", durum=15)
        Contract.objects.create(tender=t, ekap_sozlesme_id="v2-7")
        self.assertEqual(T.sonuc("2026/7", cli=MagicMock()), {"atlandi": "v2_sozlesmesi_var"})
        self.assertFalse(T._isaretle("sonuc:2026/7"))       # işaret hâlâ duruyor


@override_settings(CACHES=_LOCMEM)
class TurDonusumTests(TestCase):
    def setUp(self):
        cache.clear()

    def _tur(self, tur):
        with patch.object(T, "_kesif_yigini", return_value=[]), \
             patch.object(T.durum_mod, "adim", return_value={"atlandi": "durum_yok"}), \
             patch.object(T, "_sirada_detay", return_value=None), \
             patch.object(T, "sonuc", return_value={"is": "sonuc"}) as s, \
             patch.object(T, "detay", return_value={"is": "detay"}) as d:
            T._tur_yap(MagicMock(), tur)
        return s, d

    def test_sonuc_kuyrugu_doluyken_tazeleme_de_sira_alir(self):
        _ihale("2026/10", durum=15)                     # sonuç adayı
        _ihale("2026/20", durum=2, gun_once=5)          # tazeleme adayı
        cagrilar = []
        for tur in (0, 1, 2):
            cache.clear()
            s, d = self._tur(tur)
            cagrilar.append("sonuc" if s.called else "tazeleme" if d.called else "-")
        self.assertEqual(set(cagrilar), {"sonuc", "tazeleme"})

    def test_istek_harcamayan_atlama_tiki_YEMEZ(self):
        _ihale("2026/20", durum=2, gun_once=5)
        with patch.object(T, "_kesif_yigini", return_value=[]), \
             patch.object(T.durum_mod, "adim", return_value={"atlandi": "durum_yok"}), \
             patch.object(T, "_sirada_detay", return_value=None), \
             patch.object(T, "_sirada_sonuc", return_value="2026/99"), \
             patch.object(T, "sonuc", return_value={"atlandi": "v2_sozlesmesi_var"}), \
             patch.object(T, "detay", return_value={"is": "detay"}) as d:
            sonuc_ = T._tur_yap(MagicMock(), 1)            # tur 1 → önce sonuç
        self.assertEqual(sonuc_, {"is": "detay"})
        d.assert_called_once()


@override_settings(CACHES=_LOCMEM)
class TazelemeOncelikTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_takip_edilen_ihale_ONCE_gelir(self):
        _ihale("2026/1", gun_once=60, senkron_gun_once=50)   # en eski senkron
        _ihale("2026/2", gun_once=100, senkron_gun_once=2)   # takipte, yaşlı
        u = get_user_model().objects.create(email="a@t.local", username="a")
        SavedTender.objects.create(user=u, tender_ikn="2026/2")
        self.assertEqual(T._sirada_tazeleme(), "2026/2")

    def test_yeni_gecmis_ihale_eskilerden_once_gelir(self):
        _ihale("2026/1", gun_once=90, senkron_gun_once=60)
        _ihale("2026/2", gun_once=3, senkron_gun_once=2)
        self.assertEqual(T._sirada_tazeleme(), "2026/2")


class TazelemeKuraliTests(TestCase):
    def _t(self, gun_once, senkron_gun_once, durum=2):
        simdi = timezone.now()
        t = Tender(ihale_durum=durum, ihale_tarihi=simdi - timedelta(days=gun_once),
                   detail_synced_at=simdi - timedelta(days=senkron_gun_once))
        return t, simdi

    def test_yeni_gecmis_gunluk(self):
        t, n = self._t(10, 2)
        self.assertTrue(sync_mod.should_refresh_detail(t, n))

    def test_yasli_gecmis_haftalik(self):
        t, n = self._t(60, 2)
        self.assertFalse(sync_mod.should_refresh_detail(t, n))
        t, n = self._t(60, 8)
        self.assertTrue(sync_mod.should_refresh_detail(t, n))

    def test_takipte_yasa_bakmadan_gunluk(self):
        t, n = self._t(60, 2)
        self.assertTrue(sync_mod.should_refresh_detail(t, n, takipte=True))
        t, n = self._t(200, 2)
        self.assertTrue(sync_mod.should_refresh_detail(t, n, takipte=True))


@override_settings(EKAP_MOBIL_ENABLED=True, EKAP_V2_TAZELEME=False)
class V2RefreshStaleTests(TestCase):
    def test_mobil_birincilken_SyncRun_yazmaz(self):
        from ekap.models import SyncRun
        from ekap.tasks import refresh_stale

        self.assertEqual(refresh_stale(), {"status": "mobil_birincil"})
        self.assertFalse(SyncRun.objects.filter(task="refresh_stale").exists())
