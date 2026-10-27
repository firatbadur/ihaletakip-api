"""
Durum odaklı v2 tazelemesi (`ekap/v2_tazeleme.py`, 2026-10-27).

Fixture'lar gerçek v2 yanıtları (2026-10-27, kamuya açık ihale verisi):
`v2_detay_sonuclanmis.json` (2026/1852675, durum 15, 1 sözleşme + sonuç ilanı) ve
`v2_detay_degerlendirme.json` (2026/1865935, durum 3). Kimlikleri karma, `idareId`
karma, ilan/sözleşme kimlikleri YOK, `enUstIdareKod`/`islemlerKuralSeti` YOK.
"""
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from ekap import v2_tazeleme as v2
from ekap.client import EkapError
from ekap.models import Announcement, Contract, Tender
from tenders.models import SavedTender

FIX = Path(__file__).parent / "fixtures"


def _fixture(ad):
    return json.loads((FIX / ad).read_text())


def _istemci(veri):
    cli = MagicMock()
    cli.build_search_body.side_effect = lambda **kw: kw
    cli.search.return_value = {"list": [veri["liste_satiri"]], "totalCount": 1}
    cli.get_detail.return_value = veri["detay"]
    return cli


def _mobil_ihale(ikn, durum, **kw):
    alan = dict(ikn=ikn, ekap_id="mobil:" + ikn.replace("/", "-"), ihale_adi="x",
                idare_id="12345", idare_kaynak="yol", en_ust_idare_kod="999",
                fiyat_disi_unsur_var=True, istekli_sayisi=7, ihale_durum=durum)
    alan.update(kw)
    return Tender.objects.create(**alan)


class IsleTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_sonuc_toplama_GERCEK_yanitla(self):
        veri = _fixture("v2_detay_sonuclanmis.json")
        t = _mobil_ihale("2026/1852675", 15)
        Announcement.objects.create(tender=t, ekap_ilan_id="mobil:i:1", ilan_tip=1, baslik="mobil")
        self.assertEqual(v2.isle(t, True, cli=_istemci(veri)), "sonuc")
        t.refresh_from_db()
        # ⚠️ Korunan: ekap_id (istemci anahtarı), SAYISAL idare_id (v2 karma veriyor),
        # v2'nin artık vermediği alanlar (koruyucu yazma).
        self.assertEqual(t.ekap_id, "mobil:2026-1852675")
        self.assertEqual((t.idare_id, t.en_ust_idare_kod), ("12345", "999"))
        self.assertEqual((t.fiyat_disi_unsur_var, t.istekli_sayisi), (True, 7))
        self.assertEqual(t.detay_kaynak, "v2")
        self.assertTrue(t.v2_id)
        self.assertIsNotNone(t.sonuc_denendi_at)
        # Sözleşme + sonuç ilanı bağı → ihale yaklaşık maliyeti okundu.
        self.assertEqual(str(t.yaklasik_maliyet_num), "812073.33")
        s = Contract.objects.get(tender=t)
        self.assertTrue(s.ekap_sozlesme_id.startswith(v2.SOZLESME_ONEK))
        self.assertEqual(str(s.sozlesme_bedeli_num), "818440.00")
        self.assertTrue(s.ekap_ilan_id.startswith(v2.ILAN_ONEK))
        # İki ilan ayrı satırda; mobilin İhale İlanı v2'ninkine yer bıraktı (çift yok).
        ilanlar = sorted(Announcement.objects.filter(tender=t).values_list("ilan_tip", flat=True))
        self.assertEqual(ilanlar, [1, 4])

    def test_tazeleme_sozlesme_tablosuna_DOKUNMAZ(self):
        veri = _fixture("v2_detay_degerlendirme.json")
        t = _mobil_ihale("2026/1865935", 2)
        self.assertEqual(v2.isle(t, False, cli=_istemci(veri)), "tazeleme")
        t.refresh_from_db()
        self.assertEqual(t.ihale_durum, 3)
        self.assertEqual(t.idare_id, "12345")
        self.assertIsNone(t.sonuc_denendi_at)
        self.assertFalse(Contract.objects.filter(tender=t).exists())

    def test_bayat_kimlik_IKN_aramasiyla_BIR_KEZ_tazelenir(self):
        veri = _fixture("v2_detay_degerlendirme.json")
        t = _mobil_ihale("2026/1865935", 3, v2_id="eski")
        cli = _istemci(veri)
        cli.get_detail.side_effect = [EkapError("HTTP 400"), veri["detay"]]
        self.assertEqual(v2.isle(t, False, cli=cli), "tazeleme")
        self.assertEqual(cli.search.call_count, 1)
        self.assertEqual(cli.get_detail.call_args_list[1].args[0], veri["liste_satiri"]["id"])

    def test_BASKA_ihalenin_detayi_ASLA_yazilmaz(self):
        veri = _fixture("v2_detay_degerlendirme.json")      # İKN 2026/1865935
        t = _mobil_ihale("2026/9999999", 2)
        cli = _istemci(veri)
        cli.search.return_value = {"list": [dict(veri["liste_satiri"], ikn="2026/9999999")]}
        with self.assertRaises(EkapError):
            v2.isle(t, False, cli=cli)
        t.refresh_from_db()
        self.assertEqual((t.ihale_durum, t.detay_kaynak), (2, ""))

    def test_v2de_yoksa_isaretlenir_sonuc_bir_daha_denenmez(self):
        t = _mobil_ihale("2026/7", 15)
        cli = MagicMock()
        cli.build_search_body.side_effect = lambda **kw: kw
        cli.search.return_value = {"list": [], "totalCount": 0}
        self.assertEqual(v2.isle(t, True, cli=cli), "bulunamadi")
        t.refresh_from_db()
        self.assertIsNotNone(t.sonuc_denendi_at)
        self.assertTrue(v2._isaretli("2026/7"))


class UyarlaTest(TestCase):
    def test_sayisal_idareId_KORUNUR_karma_atilir(self):
        g = v2.uyarla({"item": {"ikn": "2026/1", "idareId": "123", "ihaleBilgi": {"idareId": "a" * 64}}})
        self.assertEqual(g["item"]["idareId"], "123")
        self.assertNotIn("idareId", g["item"]["ihaleBilgi"])

    def test_bos_sozlesme_listesi_anahtariyla_birlikte_atilir(self):
        self.assertNotIn("sozlesmeBilgiList", v2.uyarla({"item": {"sozlesmeBilgiList": []}})["item"])


class AdaySecimiTest(TestCase):
    def setUp(self):
        cache.clear()
        self.simdi = timezone.now()

    def _t(self, ikn, durum, gun_once, senk_saat_once=None):
        senk = None if senk_saat_once is None else self.simdi - timedelta(hours=senk_saat_once)
        return Tender.objects.create(ikn=ikn, ekap_id=ikn, ihale_durum=durum,
                                     ihale_tarihi=self.simdi - timedelta(days=gun_once),
                                     detail_synced_at=senk)

    def _sira(self):
        return list(v2.tazeleme_adaylari(self.simdi).values_list("ikn", flat=True))

    def test_kapsam_ve_SIRA(self):
        self._t("d4", 4, 5, 48)
        self._t("d3", 3, 5, 48)
        self._t("d2", 2, 5, 48)
        self._t("bilinmiyor", None, 5, 48)
        self._t("takipli4", 4, 5, 48)
        SavedTender.objects.create(user=self._kullanici(), tender_ikn="takipli4", tender_id="takipli4")
        self._t("sonuclanmis", 15, 5, 48)
        self._t("iptal", 6, 5, 48)
        self._t("acik", 2, -3, 48)                      # teklif tarihi ileride
        self._t("cok_eski", 2, 130, 24 * 30)
        sira = self._sira()
        # Sıra: takipli → hâlâ "katılıma açık" (2) ya da bilinmeyen → 3 → 4.
        # Sonuçlanmış, iptal, açık (teklif tarihi ileride) ve >120 gün HİÇ seçilmez.
        self.assertEqual(sira[0], "takipli4")
        self.assertEqual(set(sira[1:3]), {"d2", "bilinmiyor"})
        self.assertEqual(sira[3:], ["d3", "d4"])

    def test_VADE_kurallari(self):
        self._t("yakin_taze", 3, 5, 10)                  # ≤30 g, 10 sa önce → daha değil
        self._t("yakin_bayat", 3, 5, 21)                 # ≤30 g, 21 sa önce → evet
        self._t("uzak_taze", 3, 60, 24 * 3)              # 30–120 g, 3 gün önce → daha değil
        self._t("uzak_bayat", 3, 60, 24 * 8)             # 30–120 g, 8 gün önce → evet
        self.assertEqual(set(self._sira()), {"yakin_bayat", "uzak_bayat"})

    def test_sozlesmesi_olan_tazelenmez(self):
        t = self._t("sozlesmeli", 4, 5, 48)
        Contract.objects.create(tender=t, ekap_sozlesme_id="x")
        self.assertEqual(self._sira(), [])

    def test_sonuc_adaylari(self):
        self._t("s15", 15, 10)
        self._t("s5", 5, 10)
        self._t("iptal", 6, 10)
        self._t("eski", 15, 200)
        Tender.objects.filter(ikn="s5").update(sonuc_denendi_at=self.simdi)
        sozlesmeli = self._t("sozlesmeli", 15, 10)
        Contract.objects.create(tender=sozlesmeli, ekap_sozlesme_id="x")
        self.assertEqual(list(v2.sonuc_adaylari(self.simdi).values_list("ikn", flat=True)), ["s15"])

    def _kullanici(self):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.create_user(username="u", email="u@x.com", password="p")


class AnahtarlarTest(TestCase):
    @override_settings(EKAP_V2_TAZELEME_ENABLED=False)
    def test_gorev_kill_switch(self):
        from ekap.tasks import v2_tazeleme
        self.assertEqual(v2_tazeleme(), {"atlandi": "kapali"})

    @override_settings(EKAP_TAZELEME_KAYNAK="v2")
    def test_kaynak_v2_iken_mobil_tik_durum_sonuc_tazeleme_KOSMAZ(self):
        from unittest.mock import patch
        from ekap.mobil import tasks as mt
        with patch.object(mt, "_kesif_yigini", return_value=[]), \
                patch.object(mt, "_sirada_detay", return_value=None), \
                patch.object(mt, "_durum_adimi") as durum, patch.object(mt, "_sonuc_adimi") as sonuc, \
                patch.object(mt, "_tazeleme_adimi") as taz:
            self.assertEqual(mt._tur_yap(MagicMock(), 1), {"atlandi": "tazeleme_v2de"})
        for adim in (durum, sonuc, taz):
            adim.assert_not_called()


class DenemeKomutuTest(TestCase):
    @override_settings(EKAP_V2_TAZELEME_ARALIK_SN=0)
    def test_gercek_yanitla_rapor_ve_SUPHELI_YOK(self):
        import tempfile
        from unittest.mock import patch
        from django.core.management import call_command
        cache.clear()
        veri = _fixture("v2_detay_degerlendirme.json")
        _mobil_ihale("2026/1865935", 2, ihale_tarihi=timezone.now() - timedelta(days=4),
                     detail_synced_at=timezone.now() - timedelta(days=2))
        with tempfile.NamedTemporaryFile(suffix=".json") as f, \
                patch("ekap.tasks._dogrulama_kapisi", return_value=True), \
                patch("ekap.v2_tazeleme.EkapV2Client", return_value=_istemci(veri)):
            call_command("v2_tazeleme_deneme", adet=1, sonuc_orani=0, rapor=f.name,
                         stdout=__import__("io").StringIO())
            rapor = json.loads(Path(f.name).read_text())
        self.assertEqual(rapor["islenen"], 1)
        self.assertEqual(rapor["sonuclar"], {"tazeleme": 1})
        self.assertEqual(rapor["durum_gecisleri"], {"2→3": 1})
        self.assertEqual(rapor["supheli_alanlari"], {})
