"""
Doğrudan temin şeridi (`ekap/mobil/dt.py`).

Satır biçimi EKAP iOS 2.2.0 trafiğinden (2026-10-25) birebir alındı.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from ekap.mobil import constants as C
from ekap.mobil import dt as D
from ekap.mobil import tasks as T
from ekap.mobil import throttle
from ekap.models import City, DogrudanTemin, SyncCheckpoint


def _satir(dtn="26DT2112913", **kw):
    gun = timezone.localdate()
    s = {
        "adi": "BAĞLAR İLÇE MİLLİ EĞİTİM MÜDÜRLÜĞÜ ÖVÜNDÜLER KÖYÜ TOKAÇLI MEZRASI İLKOKULU MÜDÜRLÜĞÜ",
        "dogrudanTeminAdi": "MAZOT ALIMI",
        "dogrudanTeminDokumaniVarMi": False,
        "dogrudanTeminDurum": "Doğrudan Temin Duyurusu Yayımlanmış",
        "dogrudanTeminIlaniVarMi": True,
        "dogrudanTeminIstisnaAliminDayanagi": None,
        "dogrudanTeminTarihSaat": (gun + timedelta(days=2)).strftime("%d.%m.%Y") + " 11:30",
        "dogrudanTeminTipi": "Mal",
        "dogrudanTeminYasaKapsamiTeminMadde": "22-d* (Parasal Limit Kapsamında)",
        "dtn": dtn,
        "encID": "9c975a0f531cde496ab9b4f24697faf30becf8dc284c04576791da85c5edb277",
        "encIdareID": "617847fe04117bb8c93c0ee730bc3663d856c2e67b6e91941fb5154ac5ed2a07",
        "il": "DİYARBAKIR",
        "ilanTarihi": gun.strftime("%d.%m.%Y"),
    }
    s.update(kw)
    return s


class AyristirmaTest(TestCase):
    def test_dtn_parcala(self):
        self.assertEqual(D.dtn_parcala("26DT2112816"), ("2026", "2112816"))
        self.assertEqual(D.dtn_parcala("2026/123"), (None, None))

    def test_tarih_belirtilmemis_NULL_olur_uydurulmaz(self):
        a = D.satirdan(_satir(dogrudanTeminTarihSaat="Tarih Belirtilmemiş"))
        self.assertIn("son_teklif", a)
        self.assertIsNone(a["son_teklif"])

    def test_son_teklif_yerel_saatle_okunur(self):
        a = D.satirdan(_satir(dogrudanTeminTarihSaat="30.10.2026 11:30"))
        yerel = timezone.localtime(a["son_teklif"])
        self.assertEqual((yerel.day, yerel.hour, yerel.minute), (30, 11, 30))

    def test_madde_22_i_ile_22_dotsuz_i_KARISMAZ(self):
        # normalize_tr ikisini aynı dizgeye katlar; madde haritası katlanmamalı.
        self.assertEqual(D.satirdan(_satir(dogrudanTeminYasaKapsamiTeminMadde="22-ı"))["madde_kod"], 110)
        self.assertEqual(D.satirdan(_satir(dogrudanTeminYasaKapsamiTeminMadde="22-i"))["madde_kod"], 111)

    def test_bilinmeyen_durum_koda_cevrilmez_metin_kalir(self):
        a = D.satirdan(_satir(dogrudanTeminDurum="Zamazingo"))
        self.assertIsNone(a["durum"])
        self.assertEqual(a["durum_metin"], "Zamazingo")

    def test_acik_durum_202(self):
        self.assertEqual(D.satirdan(_satir())["durum"], C.DT_DURUM_ACIK)


@patch("ekap.mobil.dt.idare_mod.coz", return_value=("", ""))
class UpsertTest(TestCase):
    def setUp(self):
        City.objects.create(ad="DİYARBAKIR", ekap_il_id=265, plaka=21)

    def test_yeni_kayit_ve_il_cozumu(self, _coz):
        sonuc = D.upsert([_satir()])
        self.assertEqual(sonuc["yeni"], ["26DT2112913"])
        d = DogrudanTemin.objects.get()
        self.assertEqual((d.il_id, d.tur, d.madde_kod), (265, 1, 104))
        self.assertTrue(d.enc_id)
        self.assertEqual(d.ad_norm, "mazot alimi")

    def test_ikinci_gorulmede_yeni_DEGIL_ve_encid_guncellenir(self, _coz):
        D.upsert([_satir()])
        sonuc = D.upsert([_satir(encID="yeni-enc")])
        self.assertEqual(sonuc, {"yeni": [], "guncel": 1})
        self.assertEqual(DogrudanTemin.objects.get().enc_id, "yeni-enc")

    def test_KORUYUCU_anahtar_yoksa_alan_ellenmez(self, _coz):
        D.upsert([_satir(dogrudanTeminDokumaniVarMi=True)])
        kismi = _satir()
        del kismi["dogrudanTeminDokumaniVarMi"]
        D.upsert([kismi])
        self.assertTrue(DogrudanTemin.objects.get().dokuman_var)

    def test_ayni_turda_tekrar_eden_dtn_tek_satir(self, _coz):
        D.upsert([_satir(), _satir()])
        self.assertEqual(DogrudanTemin.objects.count(), 1)


@override_settings(EKAP_DT_ACIK_AZAMI_GUN=30)
class AcikTanimiTest(TestCase):
    def _dt(self, dtn, **kw):
        simdi = timezone.now()
        alan = dict(durum=C.DT_DURUM_ACIK, son_teklif=simdi + timedelta(hours=3),
                    ilan_tarihi=timezone.localdate(), son_gorulme=simdi)
        alan.update(kw)
        return DogrudanTemin.objects.create(dtn=dtn, **alan)

    def _acik(self):
        return set(DogrudanTemin.objects.filter(D.acik_q()).values_list("dtn", flat=True))

    def test_son_teklif_gecen_dusur(self):
        self._dt("A")
        self._dt("B", son_teklif=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self._acik(), {"A"})

    def test_ZOMBI_eski_ilan_son_teklifi_ileride_olsa_da_acik_DEGIL(self):
        self._dt("Z", ilan_tarihi=timezone.localdate() - timedelta(days=340),
                 son_teklif=timezone.now() + timedelta(days=900))
        self.assertEqual(self._acik(), set())

    def test_tarihsiz_acik_sayilir(self):
        self._dt("N", son_teklif=None)
        self.assertEqual(self._acik(), {"N"})

    def test_sonuca_gecen_dusur(self):
        self._dt("S", durum=15)
        self.assertEqual(self._acik(), set())

    def test_IKI_tam_taramadir_gorulmeyen_gizlenir_biri_yetmez(self):
        simdi = timezone.now()
        eski, yeni = simdi - timedelta(hours=4), simdi - timedelta(hours=2)
        SyncCheckpoint.objects.create(name=D.CHECKPOINT, extra={
            "tam_baslar": [eski.isoformat(), yeni.isoformat()]})
        self._dt("IKI_TUR_ONCE", son_gorulme=eski - timedelta(minutes=1))
        self._dt("SON_TURDA_KACTI", son_gorulme=eski + timedelta(minutes=1))
        self._dt("TAZE", son_gorulme=simdi)
        self.assertEqual(self._acik(), {"SON_TURDA_KACTI", "TAZE"})

    def test_hic_tam_tarama_yoksa_gorulme_esigi_uygulanmaz(self):
        self._dt("A", son_gorulme=timezone.now() - timedelta(days=3))
        self.assertEqual(self._acik(), {"A"})


def _cli(*sayfalar):
    cli = MagicMock()
    cli.dt_liste_govdesi.side_effect = lambda **kw: kw
    cli.dt_liste.side_effect = list(sayfalar)
    return cli


def _sayfa(bas, adet):
    return [_satir(dtn=f"26DT{bas - i:07d}") for i in range(adet)]


@patch("ekap.mobil.dt.idare_mod.coz", return_value=("", ""))
class YoklamaTest(TestCase):
    def test_tek_sayfa_tek_istek_ve_bugunun_filtresi(self, _coz):
        cli = _cli(_sayfa(2000000, 3))
        sonuc = D.yoklama(cli)
        self.assertEqual((sonuc["istek"], len(sonuc["yeni"])), (1, 3))
        govde = cli.dt_liste.call_args.args[0]
        bugun = timezone.localdate().strftime("%d.%m.%Y")
        self.assertEqual(govde["ilanTarihiBaslangic"], bugun)
        self.assertEqual(govde["ilanTarihiBitis"], bugun)
        self.assertEqual(govde["dogrudanTeminDurumu"], C.DT_DURUM_ACIK)

    def test_dolu_sayfanin_sonu_YENIYSE_ikinci_sayfa(self, _coz):
        cli = _cli(_sayfa(2000000, 250), _sayfa(1999750, 10))
        self.assertEqual(D.yoklama(cli)["istek"], 2)

    def test_dolu_sayfanin_sonu_BILINIYORSA_durur(self, _coz):
        D.upsert([_satir(dtn="26DT1999751")])
        cli = _cli(_sayfa(2000000, 250))
        self.assertEqual(D.yoklama(cli)["istek"], 1)

    def test_zaman_damgasi_yazilir_ve_aralik_dolmadan_tekrar_istenmez(self, _coz):
        D.yoklama(_cli([]))
        self.assertFalse(D.yoklama_zamani_mi())


@patch("ekap.mobil.dt.idare_mod.coz", return_value=("", ""))
class TamTaramaTest(TestCase):
    def test_tur_sayfa_sayfa_ilerler_ve_tamamlaninca_baslangic_yazilir(self, _coz):
        cli = _cli(_sayfa(2000000, 250), _sayfa(1999750, 4))
        ilk = D.tam_tarama_adimi(cli)
        self.assertEqual((ilk["sayfa"], ilk.get("tamamlandi")), (1, None))
        self.assertEqual(D.gorulme_esigi(), None)   # yarım tur eşik üretmez
        ikinci = D.tam_tarama_adimi(cli)
        self.assertEqual((ikinci["sayfa"], ikinci["tamamlandi"]), (2, True))
        self.assertIsNotNone(D.gorulme_esigi())
        self.assertEqual(cli.dt_liste.call_args.args[0]["pageIndex"], 2)

    def test_aralik_dolmadiysa_ISTEK_HARCANMAZ(self, _coz):
        cli = _cli([])
        D.tam_tarama_adimi(cli)
        self.assertIsNone(D.tam_tarama_adimi(cli))
        self.assertEqual(cli.dt_liste.call_count, 1)

    def test_son_iki_tur_tutulur(self, _coz):
        cp = SyncCheckpoint.objects.create(name=D.CHECKPOINT, extra={
            "tam_baslar": ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"]})
        D.tam_tarama_adimi(_cli([]))
        cp.refresh_from_db()
        self.assertEqual(len(cp.extra["tam_baslar"]), 2)
        self.assertEqual(cp.extra["tam_baslar"][0], "2026-01-02T00:00:00+00:00")

    def test_tam_tarama_ilan_yasi_tabaniyla_sorar(self, _coz):
        cli = _cli([])
        D.tam_tarama_adimi(cli)
        govde = cli.dt_liste.call_args.args[0]
        self.assertTrue(govde["ilanTarihiBaslangic"])
        self.assertNotEqual(govde["ilanTarihiBaslangic"], govde["ilanTarihiBitis"])


@override_settings(EKAP_MOBIL_ENABLED=True, EKAP_MOBIL_DT_ENABLED=True)
@patch("ekap.mobil.dt.idare_mod.coz", return_value=("", ""))
class DtTikTest(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(EKAP_MOBIL_DT_ENABLED=False)
    def test_kill_switch(self, _coz):
        self.assertEqual(T.dt_tik(), {"atlandi": "kapali"})

    def test_yoklama_ve_tam_sayfa_ayni_turda_sayac_artar(self, _coz):
        cli = _cli(_sayfa(2000000, 2), _sayfa(2000000, 2))
        with patch.object(T, "_dt_cli", return_value=cli), \
             patch.object(T, "_dt_yeni_bildir") as bildir:
            sonuc = T.dt_tik()
        self.assertIn("yoklama", sonuc)
        self.assertIn("tam", sonuc)
        self.assertEqual(throttle.sayac_oku("dt"), 2)
        bildir.assert_called_once()

    def test_yeni_yoksa_bildirim_tetiklenmez(self, _coz):
        D.upsert(_sayfa(2000000, 2))
        cli = _cli(_sayfa(2000000, 2), _sayfa(2000000, 2))
        with patch.object(T, "_dt_cli", return_value=cli), \
             patch.object(T, "_dt_yeni_bildir") as bildir:
            T.dt_tik()
        bildir.assert_not_called()


class TemizlikTest(TestCase):
    @override_settings(EKAP_DT_SAKLA_GUN=30)
    def test_eski_satirlar_silinir(self):
        simdi = timezone.now()
        DogrudanTemin.objects.create(dtn="ESKI", son_teklif=simdi - timedelta(days=31))
        DogrudanTemin.objects.create(dtn="YENI", son_teklif=simdi - timedelta(days=2))
        self.assertEqual(D.temizle(), 1)
        self.assertEqual(list(DogrudanTemin.objects.values_list("dtn", flat=True)), ["YENI"])
