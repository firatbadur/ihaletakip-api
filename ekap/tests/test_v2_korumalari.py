"""
v2 (eski EKAP) yeniden açılmadan önceki korumalar (2026-10-27).

v2 doğrulama çerezi 2026-09-11'den beri bayattı; aşağıdaki yollar yalnızca bu yüzden
sessizdi. Tazeleme v2'ye taşınırken çerez geçerli olunca çalışmaya başlayacaklardı:

- v2 liste upsert'i mobil keşfinin sahte `mobil:…` `ekap_id`'sini gerçek id'ye
  yeniden yazardı → istemcilerin anahtar olarak sakladığı `Favorite.tender_id`,
  `alarms/<tender_id>/` ve bildirim bağlantıları kopardı.
- `sync_detail` / eksik detay beslemesi / ihale sayfası sahte kimliği v2'ye yollardı.
- v2'nin `dokuman_sayisi` 0→N ilk yazımı sahte "doküman güncellendi" alarmı üretirdi.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from ekap.models import SyncCheckpoint, SyncRun, Tender
from ekap.sync import upsert_tender_from_list
from ekap.tasks import KATALOG_ESKI_GUN, katalog_durumu, sync_contractors, sync_detail
from tenders.tasks import _detect_alarm_events


def _v2_satiri(ikn, v2_id):
    return {"id": v2_id, "ikn": ikn, "ihaleAdi": "Yol Yapım İşi", "idareAdi": "X Belediyesi",
            "ihaleTarihSaat": "10.11.2026 10:00", "ihaleDurum": 2, "dokumanSayisi": 3}


class V2ListeKimlikTest(TestCase):
    def test_SAHTE_ekap_id_EZILMEZ_gercek_id_v2_id_ye_yazilir(self):
        Tender.objects.create(ikn="2026/5", ekap_id="mobil:2026-5", ihale_adi="İş")
        upsert_tender_from_list(_v2_satiri("2026/5", 98765))
        t = Tender.objects.get(ikn="2026/5")
        self.assertEqual(t.ekap_id, "mobil:2026-5")
        self.assertEqual(t.v2_id, "98765")

    def test_yeni_ihalede_gercek_id_iki_alana_da_yazilir(self):
        upsert_tender_from_list(_v2_satiri("2026/6", 111))
        t = Tender.objects.get(ikn="2026/6")
        self.assertEqual((t.ekap_id, t.v2_id), ("111", "111"))

    def test_gercek_id_li_satirda_eski_davranis_surer(self):
        # EKAP aynı İKN'yi yeni iç id ile yeniden yayımlayabiliyor (sync.py yorumu).
        Tender.objects.create(ikn="2026/7", ekap_id="500", ihale_adi="İş")
        upsert_tender_from_list(_v2_satiri("2026/7", 501))
        t = Tender.objects.get(ikn="2026/7")
        self.assertEqual((t.ekap_id, t.v2_id), ("501", "501"))

    def test_mobil_liste_yolu_v2_id_YAZMAZ(self):
        Tender.objects.create(ikn="2026/8", ekap_id="mobil:2026-8", ihale_adi="İş")
        upsert_tender_from_list({"id": "mobil:2026-8", "ikn": "2026/8", "ihaleAdi": "İş"},
                                koruyucu=True)
        self.assertEqual(Tender.objects.get(ikn="2026/8").v2_id, "")


@patch("ekap.tasks.EkapV2Client")
class SahteKimlikV2yeGitmezTest(TestCase):
    def test_sync_detail_sahte_kimligi_EKAPa_gondermez(self, Cli):
        sonuc = sync_detail("mobil:2026-9")
        self.assertEqual(sonuc["atlandi"], "sentetik_kimlik")
        Cli.assert_not_called()

    @patch("ekap.tasks._enqueue_detail")
    def test_eksik_detay_beslemesi_sahte_kimligi_kuyruklamaz(self, kuyruk, Cli):
        SyncCheckpoint.objects.create(name="contractors", done=True)   # artımlı mod
        Tender.objects.create(ikn="2026/10", ekap_id="mobil:2026-10", ihale_adi="İş")
        Tender.objects.create(ikn="2026/11", ekap_id="777", ihale_adi="İş")
        sync_contractors(max_seconds=5)
        self.assertEqual([c.args[0] for c in kuyruk.call_args_list], ["777"])


class IhaleSayfasiTazelemeTetiklemezTest(TestCase):
    URL = "/api/v1/ekap/tenders/{}/"

    @patch("ekap.tasks.sync_detail.delay")
    def test_detayli_ihale_acilinca_tazeleme_KUYRUKLANMAZ(self, gecikmeli):
        Tender.objects.create(ikn="2026/12", ekap_id="1200", ihale_adi="İş",
                              detail_raw={"item": {"ihaleAdi": "İş"}},
                              detail_synced_at=timezone.now() - timedelta(days=30))
        r = self.client.get(self.URL.format("1200"))
        self.assertEqual(r.status_code, 200)
        gecikmeli.assert_not_called()

    @patch("ekap.client.EkapV2Client")
    def test_detaysiz_SAHTE_kimlikli_ihale_v2ye_GITMEZ(self, Cli):
        Tender.objects.create(ikn="2026/13", ekap_id="mobil:2026-13", ihale_adi="İş")
        r = self.client.get(self.URL.format("mobil:2026-13"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["message"], "Detay güncel değil.")
        Cli.assert_not_called()


class DokumanAlarmiKaldirildiTest(TestCase):
    def test_dokuman_sayisi_degisse_de_olay_URETILMEZ(self):
        alarm = SimpleNamespace(reminder_day=False, document_change=True, completed=False,
                                completed_notified=False, last_dokuman_sayisi=0,
                                last_ihale_durum=2)
        ihale = SimpleNamespace(ihale_tarihi=None, dokuman_sayisi=4, ihale_durum=2)
        self.assertEqual(_detect_alarm_events(alarm, ihale, timezone.localdate(), {15}), set())


class KatalogDurumuTest(TestCase):
    def _run(self, task, status, gun):
        r = SyncRun.objects.create(task=task, status=status, items=10)
        SyncRun.objects.filter(pk=r.pk).update(started_at=timezone.now() - timedelta(days=gun))

    def test_son_BASARILI_tura_bakilir_dusen_tur_bayatligi_gizlemez(self):
        self._run("sync_okas", "ok", 50)
        self._run("sync_okas", "error", 1)            # son tur yakın ama başarısız
        durum = {k["gorev"]: k for k in katalog_durumu()}
        self.assertEqual(durum["sync_okas"]["gun"], 50)
        self.assertTrue(durum["sync_okas"]["eski"])

    def test_esik_alti_eski_DEGIL(self):
        self._run("sync_okas", "ok", KATALOG_ESKI_GUN - 1)
        self.assertFalse({k["gorev"]: k for k in katalog_durumu()}["sync_okas"]["eski"])

    def test_hic_basarili_tur_yoksa_eski(self):
        self.assertTrue({k["gorev"]: k for k in katalog_durumu()}["sync_okas"]["eski"])

    def test_DONDURULAN_DETSIS_uyari_URETMEZ(self):
        # Ürün kararı 2026-10-27: EKAP anonimleştirdi, ağaç 09-07 hâlinde donduruldu.
        self._run("sync_authorities", "ok", 50)
        detsis = {k["gorev"]: k for k in katalog_durumu()}["sync_authorities"]
        self.assertTrue(detsis["donduruldu"])
        self.assertFalse(detsis["eski"])


class DetsisBicimKilidiTest(TestCase):
    """2026-10-27: EKAP DETSIS'i anonimleştirdi (detsisNo -1, idareId karma)."""

    KARMA = "e3b92d4b62251a63a78a9bbf93f289965fed3a1c9a0c414f01efc79a052a8be3"

    def _istemci(self, satirlar):
        from unittest.mock import MagicMock
        cli = MagicMock()
        cli.detsis_roots.return_value = {"loadResult": {"data": []}}
        cli.detsis_all_descendants.return_value = {"loadResult": {"data": []}}
        cli.detsis_children.return_value = {"loadResult": {"data": satirlar}}
        return cli

    def test_ANONIM_veri_YAZILMAZ_tur_hata_verir(self):
        from ekap.models import Authority
        from ekap.sync import sync_authorities
        satirlar = [{"detsisNo": -1, "ad": f"KURUM {i}", "idareId": self.KARMA,
                     "parentIdareKimlikKodu": "-999999", "hasItems": False} for i in range(5)]
        with self.assertRaises(ValueError):
            sync_authorities(self._istemci(satirlar))
        self.assertFalse(Authority.objects.exists())

    def test_bos_yanit_YAZILMAZ(self):
        from ekap.sync import sync_authorities
        with self.assertRaises(ValueError):
            sync_authorities(self._istemci([]))

    def test_eksi_ama_GERCEK_detsis_no_gecer(self):
        from ekap.models import Authority
        from ekap.sync import sync_authorities
        satirlar = [{"detsisNo": -1006661, "ad": "Yatılı Okul", "idareId": 6661,
                     "parentIdareKimlikKodu": -999999, "hasItems": False}]
        sync_authorities(self._istemci(satirlar))
        self.assertTrue(Authority.objects.filter(detsis_no="-1006661", idare_id="6661").exists())
