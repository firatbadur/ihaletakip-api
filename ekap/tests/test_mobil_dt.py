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

    def test_taramada_gorulmemek_TEK_BASINA_gizlemez(self):
        """⚠️ EKAP dolu sayfayı 249 verebiliyor (ölçüldü) — görülmemek şüphedir, kanıt değil."""
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
    def test_bugunun_filtresi_ve_bos_sayfada_durur(self, _coz):
        cli = _cli(_sayfa(2000000, 3), [])
        sonuc = D.yoklama(cli)
        self.assertEqual((sonuc["istek"], len(sonuc["yeni"])), (2, 3))
        govde = cli.dt_liste.call_args.args[0]
        bugun = timezone.localdate().strftime("%d.%m.%Y")
        self.assertEqual(govde["ilanTarihiBaslangic"], bugun)
        self.assertEqual(govde["ilanTarihiBitis"], bugun)
        self.assertEqual(govde["dogrudanTeminDurumu"], C.DT_DURUM_ACIK)

    def test_dolu_sayfanin_sonu_YENIYSE_ikinci_sayfa(self, _coz):
        cli = _cli(_sayfa(2000000, 250), _sayfa(1999750, 10))
        self.assertEqual(D.yoklama(cli)["istek"], 2)

    def test_249_kayitli_sayfa_BITIS_DEGILDIR(self, _coz):
        """Ölçüldü: EKAP dolu sayfayı 249 kayıtla döndürüyor, sonraki sayfa yine dolu."""
        cli = _cli(_sayfa(2000000, 249), _sayfa(1999751, 250))
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
    def test_tur_BOS_SAYFAYA_kadar_surer_249_bitis_degildir(self, _coz):
        cli = _cli(_sayfa(2000000, 250), _sayfa(1999750, 249), [])
        self.assertIsNone(D.tam_tarama_adimi(cli).get("tamamlandi"))
        self.assertIsNone(D.tam_tarama_adimi(cli).get("tamamlandi"))   # 249 → devam
        son = D.tam_tarama_adimi(cli)
        self.assertEqual((son["sayfa"], son["tamamlandi"]), (3, True))
        self.assertEqual(cli.dt_liste.call_args.args[0]["pageIndex"], 3)

    def test_turda_gorulmeyen_acik_kayit_DOGRULAMA_kuyruguna_girer(self, _coz):
        simdi = timezone.now()
        DogrudanTemin.objects.create(
            dtn="26DT0000001", durum=C.DT_DURUM_ACIK, son_teklif=simdi + timedelta(days=1),
            ilan_tarihi=timezone.localdate(), son_gorulme=simdi - timedelta(hours=5),
            enc_id="e")
        cli = _cli(_sayfa(2000000, 3), [])
        D.tam_tarama_adimi(cli)
        D.tam_tarama_adimi(cli)
        cp = SyncCheckpoint.objects.get(name=D.CHECKPOINT)
        self.assertEqual(cp.extra["dogrula"], ["26DT0000001"])
        # Hâlâ açık listede: görülmemek tek başına gizlemez.
        self.assertTrue(DogrudanTemin.objects.filter(D.acik_q(), dtn="26DT0000001").exists())

    def test_YARIM_tur_kuyruk_uretmez(self, _coz):
        DogrudanTemin.objects.create(
            dtn="26DT0000001", durum=C.DT_DURUM_ACIK, ilan_tarihi=timezone.localdate(),
            son_gorulme=timezone.now() - timedelta(hours=5), enc_id="e")
        D.tam_tarama_adimi(_cli(_sayfa(2000000, 250)))
        cp = SyncCheckpoint.objects.get(name=D.CHECKPOINT)
        self.assertFalse(cp.extra.get("dogrula"))

    def test_aralik_dolmadiysa_ISTEK_HARCANMAZ(self, _coz):
        cli = _cli([])
        D.tam_tarama_adimi(cli)
        self.assertIsNone(D.tam_tarama_adimi(cli))
        self.assertEqual(cli.dt_liste.call_count, 1)

    def test_tam_tarama_ilan_yasi_tabaniyla_sorar(self, _coz):
        cli = _cli([])
        D.tam_tarama_adimi(cli)
        govde = cli.dt_liste.call_args.args[0]
        self.assertTrue(govde["ilanTarihiBaslangic"])
        self.assertNotEqual(govde["ilanTarihiBaslangic"], govde["ilanTarihiBitis"])


class DogrulamaTest(TestCase):
    def setUp(self):
        simdi = timezone.now()
        self.d = DogrudanTemin.objects.create(
            dtn="26DT1", durum=C.DT_DURUM_ACIK, son_teklif=simdi + timedelta(days=1),
            ilan_tarihi=timezone.localdate(), son_gorulme=simdi - timedelta(hours=5),
            enc_id="e1")
        SyncCheckpoint.objects.create(name=D.CHECKPOINT, extra={
            "dogrula": ["26DT1"], "dogrula_bas": (simdi - timedelta(hours=1)).isoformat()})

    def _detay(self, durum):
        return {"dogrudanTeminBilgileri": {"dtDurumu": durum,
                                           "dtTarihSaati": "30.12.2099 10:00"}}

    def test_IPTAL_SONUC_dogrulaninca_listeden_duser(self, ):
        cli = MagicMock()
        cli.dt_detay.return_value = self._detay("Doğrudan Temin Sonuçlandırıldı")
        self.assertEqual(D.dogrulama_adimi(cli)["istek"], 1)
        self.assertFalse(DogrudanTemin.objects.filter(D.acik_q(), dtn="26DT1").exists())

    def test_hala_ACIK_dogrulaninca_listede_kalir(self):
        cli = MagicMock()
        cli.dt_detay.return_value = self._detay("Doğrudan Temin Duyurusu Yayımlanmış")
        D.dogrulama_adimi(cli)
        self.assertTrue(DogrudanTemin.objects.filter(D.acik_q(), dtn="26DT1").exists())
        self.assertIsNone(D.dogrulama_adimi(cli))       # kuyruk boşaldı

    def test_EKAP_yok_derse_acik_sayilmaz(self):
        from ekap.mobil.client import MobilYokError

        cli = MagicMock()
        cli.dt_detay.side_effect = MobilYokError("yok")
        D.dogrulama_adimi(cli)
        self.assertFalse(DogrudanTemin.objects.filter(D.acik_q(), dtn="26DT1").exists())

    def test_GECICI_hatada_kuyrukta_kalir(self):
        from ekap.mobil.client import MobilCaptchaError

        cli = MagicMock()
        cli.dt_detay.side_effect = MobilCaptchaError("duvar")
        with self.assertRaises(MobilCaptchaError):
            D.dogrulama_adimi(cli)
        self.assertEqual(SyncCheckpoint.objects.get(name=D.CHECKPOINT).extra["dogrula"], ["26DT1"])

    def test_tur_sonrasi_yoklamanin_gordugu_kayit_SORULMAZ(self):
        DogrudanTemin.objects.filter(pk=self.d.pk).update(son_gorulme=timezone.now())
        cli = MagicMock()
        self.assertIsNone(D.dogrulama_adimi(cli))
        cli.dt_detay.assert_not_called()


@override_settings(EKAP_MOBIL_ENABLED=True, EKAP_MOBIL_DT_ENABLED=True)
@patch("ekap.mobil.dt.idare_mod.coz", return_value=("", ""))
class DtTikTest(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(EKAP_MOBIL_DT_ENABLED=False)
    def test_kill_switch(self, _coz):
        self.assertEqual(T.dt_tik(), {"atlandi": "kapali"})

    def test_yoklama_ve_tam_sayfa_ayni_turda_sayac_artar(self, _coz):
        D.upsert(_sayfa(2000000, 2))                 # son satır bilinir → yoklama 1 istek
        cli = _cli(_sayfa(2000001, 3), _sayfa(2000000, 2))
        with patch.object(T, "_dt_cli", return_value=cli), \
             patch.object(T, "_dt_yeni_bildir") as bildir:
            sonuc = T.dt_tik()
        self.assertIn("yoklama", sonuc)
        self.assertIn("tam", sonuc)
        self.assertEqual(throttle.sayac_oku("dt"), 2)
        bildir.assert_called_once()

    def test_tur_ISTEK_TAVANINI_asmaz(self, _coz):
        """2 istek × (130 sn pencere + sapma) ≈ 300 sn görev sınırı → tur 2'de durur."""
        cli = _cli(_sayfa(2000000, 250), _sayfa(1999750, 250), _sayfa(2000000, 250))
        with patch.object(T, "_dt_cli", return_value=cli), \
             patch.object(T, "_dt_yeni_bildir"), \
             patch("ekap.mobil.dt.tam_tarama_adimi") as tam, \
             patch("ekap.mobil.dt.dogrulama_adimi") as dog:
            T.dt_tik()
        self.assertEqual(cli.dt_liste.call_count, 2)
        tam.assert_not_called()
        dog.assert_not_called()

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
