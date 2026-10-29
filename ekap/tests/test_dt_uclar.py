"""Doğrudan temin uçları (`ekap/views_dt.py`)."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ekap.mobil import constants as C
from ekap.mobil.client import MobilCaptchaError, MobilError, MobilYokError
from ekap.models import DogrudanTemin

URL = "/api/v1/ekap/dogrudan-temin/"


def _dt(dtn, **kw):
    simdi = timezone.now()
    alan = dict(ad=f"{dtn} alımı", ad_norm=f"{dtn.lower()} alimi", idare_adi="X OKULU",
                idare_adi_norm="x okulu", durum=C.DT_DURUM_ACIK, tur=1, il_id=265,
                son_teklif=simdi + timedelta(hours=3), ilan_tarihi=timezone.localdate(),
                son_gorulme=simdi, enc_id=f"enc-{dtn}")
    alan.update(kw)
    return DogrudanTemin.objects.create(dtn=dtn, **alan)


_DETAY = {
    "dogrudanTeminBilgileri": {
        "dtDurumu": "Doğrudan Temin Duyurusu Yayımlanmış", "dtTarihSaati": "27.10.2026 16:00",
        "dtn": "26DT1", "ilaninSekli": "Doğrudan Temin İlanı", "kismiTeklif": "Verilemez",
        "dogrudanTeminKapsami": "4734 Kapsamında",
    },
    "idareBilgileri": {"enUstIdare": "SAĞLIK BAKANLIĞI", "idare": "GÜRSU D.H.", "ili": "BURSA",
                       "ustIdare": "BAKAN YARDIMCILIKLARI"},
    "ilanBilgileri": {"dogrudanTeminIlanListesi": [
        {"encIlanId": "x", "ilanTarihi": "25.10.2026", "ilanTipi": "1",
         "ilanHTML": "<b>8 KALEM &#304;LA&#199; ALIMI</b>"}], "sonucIlanListesi": []},
    "sozlesmeBilgileri": {"sozlesmeBilgisiList": []},
    "kod": 0, "mesaj": None, "sonuc": True,
}


class ListeTest(TestCase):
    def setUp(self):
        self.c = APIClient()

    def _liste(self, **qp):
        r = self.c.get(URL, qp)
        self.assertEqual(r.status_code, 200)
        return r.json()["data"]

    def test_yalnizca_acik_olanlar_ve_acil_once(self):
        _dt("26DT3", son_teklif=timezone.now() + timedelta(days=2))
        _dt("26DT2", son_teklif=timezone.now() + timedelta(hours=1))
        _dt("26DT9", son_teklif=None)
        _dt("26DT1", son_teklif=timezone.now() - timedelta(minutes=5))   # kapandı
        _dt("26DT5", durum=15)                                           # sonuçlandı
        veri = self._liste()
        self.assertEqual([x["dtn"] for x in veri["list"]], ["26DT2", "26DT3", "26DT9"])
        self.assertEqual(veri["totalCount"], 3)
        self.assertGreater(veri["list"][0]["kalan_dakika"], 0)
        self.assertIsNone(veri["list"][2]["kalan_dakika"])

    def test_filtreler(self):
        _dt("26DT1", tur=1, il_id=265)
        _dt("26DT2", tur=3, il_id=6, ad="Yemek hizmeti", ad_norm="yemek hizmeti")
        self.assertEqual([x["dtn"] for x in self._liste(tur="3")["list"]], ["26DT2"])
        self.assertEqual([x["dtn"] for x in self._liste(il_id="265")["list"]], ["26DT1"])
        self.assertEqual([x["dtn"] for x in self._liste(q="YEMEK")["list"]], ["26DT2"])
        self.assertEqual([x["dtn"] for x in self._liste(dtn="26dt1,26DT9")["list"]], ["26DT1"])

    def test_dokuman_url_yalnizca_dokumanliysa(self):
        _dt("26DT1", dokuman_var=True)
        _dt("26DT2")
        satirlar = {x["dtn"]: x for x in self._liste()["list"]}
        self.assertTrue(satirlar["26DT1"]["dokuman_url"].endswith("/dogrudan-temin/26DT1/dokuman/"))
        self.assertIsNone(satirlar["26DT2"]["dokuman_url"])

    def test_filtre_secenekleri(self):
        r = self.c.get(URL + "filtreler/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["data"]["maddeler"]), 11)


@patch("ekap.mobil.client.EkapMobilClient")
class DetayTest(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()

    def test_ayrinti_cozulur_html_normalize_ve_onbellek(self, Cli):
        _dt("26DT1")
        Cli.return_value.dt_detay.return_value = _DETAY
        r = self.c.get(URL + "26DT1/")
        d = r.json()["data"]
        self.assertEqual(d["detay"]["idare"]["en_ust"], "SAĞLIK BAKANLIĞI")
        self.assertIn("İLAÇ", d["detay"]["ilanlar"][0]["html"])
        self.assertTrue(d["birim_fiyat_cetveli_url"].endswith("dogrudanTeminId=enc-26DT1"))
        self.c.get(URL + "26DT1/")
        self.assertEqual(Cli.return_value.dt_detay.call_count, 1)

    def test_ayrinti_KAYITTAN_doner_onbellek_bossa_bile_EKAPa_gitmez(self, Cli):
        """Ürün kararı: DT ayrıntısı değişmiyor → bir kez alınır, hep kayıttan döner."""
        _dt("26DT1")
        Cli.return_value.dt_detay.return_value = _DETAY
        self.c.get(URL + "26DT1/")
        cache.clear()                                     # önbellek süresi dolmuş gibi
        d = self.c.get(URL + "26DT1/").json()["data"]
        self.assertEqual(Cli.return_value.dt_detay.call_count, 1)
        self.assertEqual(d["detay"]["idare"]["en_ust"], "SAĞLIK BAKANLIĞI")
        self.assertEqual(d["mesaj"], "")

    def test_detay_daha_taze_durumu_tabloya_yazar(self, Cli):
        _dt("26DT1")
        iptal = dict(_DETAY, dogrudanTeminBilgileri=dict(
            _DETAY["dogrudanTeminBilgileri"], dtDurumu="Doğrudan Temin Sonuçlandırıldı"))
        Cli.return_value.dt_detay.return_value = iptal
        self.c.get(URL + "26DT1/")
        self.assertEqual(DogrudanTemin.objects.get().durum, 4)

    def test_EKAP_yanit_vermezse_liste_alanlari_yine_gelir_ve_ONBELLEGE_YAZILMAZ(self, Cli):
        _dt("26DT1")
        Cli.return_value.dt_detay.side_effect = MobilCaptchaError("duvar")
        d = self.c.get(URL + "26DT1/").json()["data"]
        self.assertIsNone(d["detay"])
        self.assertTrue(d["mesaj"])
        self.assertEqual(d["dtn"], "26DT1")
        self.assertIsNone(DogrudanTemin.objects.get().detail_raw)

    def test_bayat_encid_tazesiyle_BIR_KEZ_tekrar_denenir(self, Cli):
        _dt("26DT1")
        cli = Cli.return_value
        cli.dt_detay.side_effect = [MobilError("HTTP 500"), _DETAY]
        cli.dt_liste_govdesi.side_effect = lambda **kw: kw
        cli.dt_liste.return_value = [{"dtn": "26DT1", "encID": "taze"}]
        d = self.c.get(URL + "26DT1/").json()["data"]
        self.assertIsNotNone(d["detay"])
        self.assertEqual(cli.dt_detay.call_args.args[0], "taze")
        self.assertEqual(DogrudanTemin.objects.get().enc_id, "taze")
        # ⚠️ Yıl DÖRT haneli gider (ölçüldü: "26" → kayıt yok, "2026" → kayıt var).
        govde = cli.dt_liste.call_args.args[0]
        self.assertEqual((govde["dtnYil"], govde["dtnSayi"]), ("2026", "1"))

    def test_yok(self, Cli):
        self.assertEqual(self.c.get(URL + "26DT404/").status_code, 404)


@patch("ekap.mobil.client.EkapMobilClient")
class DokumanTest(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()

    def test_dokumansiz_kayitta_EKAPa_GIDILMEZ(self, Cli):
        _dt("26DT1", dokuman_var=False)
        self.assertEqual(self.c.get(URL + "26DT1/dokuman/").status_code, 404)
        Cli.return_value.dt_dokuman_indir.assert_not_called()

    def test_akis_dosya_adi_ve_tur(self, Cli):
        _dt("26DT1", dokuman_var=True)
        resp = MagicMock()
        resp.headers = {"content-type": "application/octet-stream", "content-length": "6",
                        "content-disposition":
                            'attachment; filename="25DT2095274_DT_dokumani_AnyConv.com__2563812.rar"'}
        resp.iter_content.return_value = [b"Rar!", b"\x1a\x07"]
        Cli.return_value.dt_dokuman_indir.return_value = resp
        r = self.c.get(URL + "26DT1/dokuman/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"Rar!\x1a\x07")
        self.assertEqual(r["Content-Type"], "application/vnd.rar")
        self.assertIn("25DT2095274_DT_dokumani_AnyConv.com__2563812.rar", r["Content-Disposition"])

    def test_EKAP_yok_derse_404_ve_hatirlanir(self, Cli):
        _dt("26DT1", dokuman_var=True)
        Cli.return_value.dt_dokuman_indir.side_effect = MobilYokError("yok")
        self.assertEqual(self.c.get(URL + "26DT1/dokuman/").status_code, 404)
        self.assertEqual(self.c.get(URL + "26DT1/dokuman/").status_code, 404)
        self.assertEqual(Cli.return_value.dt_dokuman_indir.call_count, 1)

    def test_captcha_503_ve_HATIRLANMAZ(self, Cli):
        _dt("26DT1", dokuman_var=True)
        Cli.return_value.dt_dokuman_indir.side_effect = MobilCaptchaError("duvar")
        self.assertEqual(self.c.get(URL + "26DT1/dokuman/").status_code, 503)
        self.assertIsNone(cache.get("dt:dokyok:26DT1"))


@override_settings(PORTAL_KEY="portal-sirri")
@patch("ekap.mobil.client.EkapMobilClient")
class BotKorumasiTest(TestCase):
    """⚠️ Doğrulanmış bot hız sınırından muaf → EKAP'ı tetikleyemez (kullanıcı bütçesi)."""

    BOT = {"HTTP_X_IT_PORTAL_KEY": "portal-sirri", "HTTP_X_IT_VERIFIED_BOT": "1"}

    def setUp(self):
        cache.clear()
        self.c = APIClient()

    def test_bot_detayda_EKAPa_GITMEZ_liste_alanlari_gelir(self, Cli):
        _dt("26DT1")
        d = self.c.get(URL + "26DT1/", **self.BOT).json()["data"]
        Cli.return_value.dt_detay.assert_not_called()
        self.assertIsNone(d["detay"])
        self.assertEqual(d["dtn"], "26DT1")

    def test_bot_kayitli_ayrintiyi_alir(self, Cli):
        _dt("26DT1")
        Cli.return_value.dt_detay.return_value = _DETAY
        self.c.get(URL + "26DT1/")                       # insan: doldurur
        d = self.c.get(URL + "26DT1/", **self.BOT).json()["data"]
        self.assertIsNotNone(d["detay"])
        self.assertEqual(Cli.return_value.dt_detay.call_count, 1)

    def test_bot_dokuman_INDIREMEZ(self, Cli):
        _dt("26DT1", dokuman_var=True)
        self.assertEqual(self.c.get(URL + "26DT1/dokuman/", **self.BOT).status_code, 403)
        Cli.return_value.dt_dokuman_indir.assert_not_called()

    def test_anahtarsiz_bot_basligi_YOK_SAYILIR(self, Cli):
        _dt("26DT1")
        Cli.return_value.dt_detay.return_value = _DETAY
        self.c.get(URL + "26DT1/", HTTP_X_IT_VERIFIED_BOT="1")
        Cli.return_value.dt_detay.assert_called_once()
