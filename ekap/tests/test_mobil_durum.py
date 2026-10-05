"""
Toplu durum taraması (`ekap/mobil/durum.py`).

Ölçülmüş sözleşme (2026-10-05): liste ucu `ihaleDurumu` ile süzer, kodlar
`IHALE_DURUM` ile birebir aynıdır; satırda durum metni yoktur → durum, dilimin
hangi filtreyle istendiğinden okunur.
"""
from datetime import timedelta
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from ekap.mobil import constants as C
from ekap.mobil import durum as D
from ekap.models import SyncCheckpoint, Tender
from tenders.models import TenderAlarm


def _ihale(ikn, durum=2, gun_once=10):
    return Tender.objects.create(
        ikn=ikn, ekap_id=f"e-{ikn}", ihale_adi=ikn, ihale_durum=durum,
        ihale_tarihi=timezone.now() - timedelta(days=gun_once),
    )


class UygulaTests(TestCase):
    def test_bayat_acik_ihale_sonuclandi_olur(self):
        _ihale("2026/1")
        self.assertEqual(D.uygula({"2026/1"}, 15)["degisen"], 1)
        t = Tender.objects.get(ikn="2026/1")
        self.assertEqual(t.ihale_durum, 15)
        self.assertEqual(t.ihale_durum_aciklama, "Sonuç İlanı Yayımlanmış")

    def test_sonuclanmis_ihale_GERI_CEKILMEZ(self):
        _ihale("2026/1", durum=15)
        self.assertEqual(D.uygula({"2026/1"}, 3)["degisen"], 0)
        self.assertEqual(Tender.objects.get(ikn="2026/1").ihale_durum, 15)

    def test_db_de_olmayan_ikn_sessizce_gecer(self):
        self.assertEqual(D.uygula({"2099/1"}, 15)["degisen"], 0)

    def _alarm(self, t):
        u = get_user_model().objects.create(email=f"{t.ikn}@t.l", username=t.ikn)
        return TenderAlarm.objects.create(user=u, tender_id=t.ekap_id, tender_ikn=t.ikn,
                                          completed=True, last_ihale_durum=2)

    @override_settings(EKAP_MOBIL_DURUM_ALARM_SESSIZ_GUN=14)
    def test_ESKI_ihalenin_toplu_gecisi_alarm_URETMEZ(self):
        a = self._alarm(_ihale("2026/1", gun_once=60))
        D.uygula({"2026/1"}, 15)
        a.refresh_from_db()
        self.assertTrue(a.completed_notified)

    @override_settings(EKAP_MOBIL_DURUM_ALARM_SESSIZ_GUN=14)
    def test_YAKIN_ihalenin_gecisi_alarmi_normal_yola_birakir(self):
        a = self._alarm(_ihale("2026/1", gun_once=3))
        D.uygula({"2026/1"}, 15)
        a.refresh_from_db()
        self.assertFalse(a.completed_notified)


class AdimTests(TestCase):
    def _cli(self, satirlar):
        cli = MagicMock()
        cli.liste_govdesi.side_effect = lambda **kw: kw
        cli.liste.return_value = satirlar
        return cli

    def _yigin(self, *dilimler):
        SyncCheckpoint.objects.update_or_create(
            name=D.CHECKPOINT,
            defaults={"extra": {"yigin": list(dilimler),
                                "son_yakin": timezone.now().isoformat(),
                                "son_uzak": timezone.now().isoformat()}})

    def test_filtre_degeri_istege_gider_ve_durum_yazilir(self):
        _ihale("2026/5")
        self._yigin(["2026-08-01", "2026-08-10", 1, 15, 0])
        cli = self._cli([{"ikn": "2026/5"}])
        r = D.adim(cli)
        self.assertEqual(cli.liste.call_args.args[0]["ihaleDurumu"], 15)
        self.assertEqual(r["degisen"], 1)
        self.assertEqual(r["kalan_dilim"], 0)

    def test_tavana_takilan_dilim_ikiye_bolunur(self):
        self._yigin(["2026-08-01", "2026-08-10", 1, 15, 0])
        D.adim(self._cli([{"ikn": f"2026/{i}"} for i in range(C.LISTE_TAVAN)]))
        kalan = SyncCheckpoint.objects.get(name=D.CHECKPOINT).extra["yigin"]
        self.assertEqual([k[:2] for k in kalan],
                         [["2026-08-01", "2026-08-05"], ["2026-08-06", "2026-08-10"]])
        self.assertTrue(all(k[3] == 15 for k in kalan))

    def test_bos_yigin_iki_bolgeyle_kurulur(self):
        y = D.yigin(olustur=True)
        self.assertEqual(len(y), 2 * len(D.DURUMLAR) * len(D.TURLER))
        # Kurulduktan hemen sonra yığın bitse bile yeniden kurulmaz (tazelik).
        SyncCheckpoint.objects.filter(name=D.CHECKPOINT).update(
            extra={**SyncCheckpoint.objects.get(name=D.CHECKPOINT).extra, "yigin": []})
        self.assertEqual(D.yigin(olustur=True), [])
