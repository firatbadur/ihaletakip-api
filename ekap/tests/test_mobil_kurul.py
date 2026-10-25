"""KİK kurul karar tutanakları (`ekap/mobil/kurul.py` + uçlar). Satırlar 21.10.2026
toplantısının gerçek yanıtından (EKAP iOS 2.2.0 trafiği)."""
from unittest.mock import MagicMock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from ekap.mobil import kurul as K
from ekap.mobil import tasks as T
from ekap.models import KurulKarari, Tender


def _satir(no="2026/UH.II-2688", ikn="2026/1519213", **kw):
    s = {
        "gundemMaddesiId": "0bc1cd97a85e1f23bae5d03c3ed27841f0b5dd7175e0a33fee67fba4f4a43f17",
        "kararNo": no, "kararTarih": "2026-10-21T11:35:30.293", "toplantiTarihSaat": "10:00",
        "kurulYil": "2026", "kurulSayi": "41", "gundemMaddesiSiraNo": 36,
        "kararNitelikKod": 2, "raporTuru": 10, "gorusmeBaslamaTarih": "2026-10-21T11:35:12.623",
        "gorusmeBitisTarih": None, "karsiOyDetay": "OYBIRLIGI", "kararTurKod": 102,
        "kararAciklama": "İtirazen şikayet başvurusunun reddine,    ",
        "yayinTarihi": "2026-10-22T00:00:00",
        "idareadi": "KOCAELİ ÜNİVERSİTESİ REKTÖRLÜĞÜ SAĞLIK, KÜLTÜR VE SPOR DAİRE BAŞKANLIĞI",
        "basvuruSahibi": "Nlc İnşaat Turizm Otomotiv Danışmanlık Tic. Ve San. Ltd. Şti.",
        "ihaleAdi": "PERSONEL HİZMETİ ALIMI", "ikn": ikn,
    }
    s.update(kw)
    return s


class AyristirmaTest(TestCase):
    def test_aciklama_sondaki_virgul_ve_bosluklardan_temizlenir(self):
        self.assertEqual(K.satirdan(_satir())["karar_aciklama"],
                         "İtirazen şikayet başvurusunun reddine")

    def test_tarihler_yerel_saatle(self):
        a = K.satirdan(_satir())
        self.assertEqual(timezone.localtime(a["yayin_tarihi"]).hour, 0)
        self.assertEqual(timezone.localtime(a["karar_tarihi"]).hour, 11)

    def test_upsert_karar_no_ile_ve_ikinci_gorulme_yeni_degil(self):
        self.assertEqual(K.upsert([_satir()])["yeni"], ["2026/UH.II-2688"])
        self.assertEqual(K.upsert([_satir(kararAciklama="Düzeltici işlem belirlenmesine,")]),
                         {"yeni": [], "guncel": 1})
        self.assertEqual(KurulKarari.objects.get().karar_aciklama,
                         "Düzeltici işlem belirlenmesine")

    def test_kontrol_son_N_toplantiyi_okur(self):
        cli = MagicMock()
        cli.kurul_tarihler.return_value = ["2026-10-21T10:00:00", "2026-10-14T10:00:00",
                                           "2026-10-07T10:00:00"]
        cli.kurul_liste.side_effect = [[_satir()], [_satir(no="2026/UM.I-2600")]]
        sonuc = K.kontrol(cli, geri=2)
        self.assertEqual(sonuc["istek"], 3)
        self.assertEqual(cli.kurul_liste.call_count, 2)
        self.assertEqual(KurulKarari.objects.count(), 2)
        k = KurulKarari.objects.get(karar_no="2026/UH.II-2688")
        self.assertEqual(timezone.localtime(k.toplanti_tarihi).day, 21)

    @override_settings(EKAP_MOBIL_ENABLED=True, EKAP_MOBIL_KURUL_ENABLED=False)
    def test_kill_switch(self):
        self.assertEqual(T.kurul_kontrol(), {"atlandi": "kapali"})


class UcTest(TestCase):
    def setUp(self):
        self.c = APIClient()
        K.upsert([_satir(), _satir(no="2026/UY.II-2697", ikn="2026/1088440",
                                   ihaleAdi="ASYA BÖLGESİ ATIK SU", idareadi="İSKİ")],
                 toplanti=timezone.now())

    def test_TURKCE_katlamali_arama(self):
        r = self.c.get("/api/v1/ekap/kurul-kararlari/", {"q": "kocaeli universitesi"})
        self.assertEqual([x["karar_no"] for x in r.json()["data"]["list"]], ["2026/UH.II-2688"])
        r = self.c.get("/api/v1/ekap/kurul-kararlari/", {"q": "iski"})   # "İSKİ" (noktalı İ) ile eşleşmeli
        self.assertEqual(r.json()["data"]["totalCount"], 1)

    def test_ikn_filtresi_ve_ekap_id_cozumu(self):
        Tender.objects.create(ikn="2026/1519213", ekap_id="E1")
        d = self.c.get("/api/v1/ekap/kurul-kararlari/", {"ikn": "2026/1519213"}).json()["data"]
        self.assertEqual(d["list"][0]["ekap_id"], "E1")
        self.assertEqual(d["list"][0]["kurul"], "2026 / 41")

    def test_ihale_detayina_gomulur_ham_veri_degismez(self):
        t = Tender.objects.create(ikn="2026/1519213", ekap_id="E1",
                                  detail_synced_at=timezone.now(),
                                  detail_raw={"item": {"ikn": "2026/1519213"}})
        d = self.c.get("/api/v1/ekap/tenders/E1/").json()["data"]
        self.assertEqual([x["karar_no"] for x in d["kurulKararlari"]], ["2026/UH.II-2688"])
        t.refresh_from_db()
        self.assertNotIn("kurulKararlari", t.detail_raw["item"])

    def test_ihale_kararlari_ucu_ve_toplantilar(self):
        Tender.objects.create(ikn="2026/1088440", ekap_id="E2")
        r = self.c.get("/api/v1/ekap/tenders/E2/kurul-kararlari/").json()["data"]
        self.assertEqual([x["karar_no"] for x in r], ["2026/UY.II-2697"])
        t = self.c.get("/api/v1/ekap/kurul-kararlari/toplantilar/").json()["data"]
        self.assertEqual(t[0]["karar_sayisi"], 2)
