"""
Kayıtlı doğrudan teminler — ihalelerle AYNI klasörler (2026-10-29, ürün kararı).

Sözleşme: istemci yalnızca `dtn` (+ `group`) gönderir, kart alanları sunucudaki DT
satırından kopyalanır; satır silinse de kayıt kalır (`acik: false`).
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from ekap.mobil import constants as C
from ekap.models import DogrudanTemin
from tenders.models import SavedDogrudanTemin, SavedTender, TenderGroup

URL = "/api/v1/dogrudan-temin/kayitli/"
GRUP_URL = "/api/v1/tender-groups/"


def _veri(r):
    j = r.json()
    return j.get("data", j) if isinstance(j, dict) else j


def _dt(dtn, **kw):
    alan = dict(ad=f"{dtn} mazot alımı", idare_adi="OKUL", il_adi="ANKARA", tur=1,
                madde="22-d* (Parasal Limit)", durum=C.DT_DURUM_ACIK,
                son_teklif=timezone.now() + timedelta(hours=5), dokuman_var=True)
    alan.update(kw)
    return DogrudanTemin.objects.create(dtn=dtn, **alan)


class KayitliDtTest(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user(username="u", email="u@x.com", password="x")
        self.baska = User.objects.create_user(username="b", email="b@x.com", password="x")
        self.c = APIClient()
        self.c.force_authenticate(self.u)
        self.dt = _dt("26DT1000001")

    def test_kaydet_kart_alanlari_sunucudan_kopyalanir(self):
        r = self.c.post(URL, {"dtn": "26dt1000001", "ad": "UYDURMA"}, format="json")
        self.assertEqual(r.status_code, 201)
        row = SavedDogrudanTemin.objects.get(user=self.u)
        self.assertEqual(row.dtn, "26DT1000001")
        self.assertEqual(row.ad, self.dt.ad)  # istemcinin `ad`ı yok sayılır
        self.assertIsNone(row.group)
        d = _veri(r)
        self.assertTrue(d["acik"])
        self.assertEqual(d["tur_adi"], "Mal")
        self.assertGreater(d["kalan_dakika"], 0)

    def test_olmayan_dtn_404(self):
        r = self.c.post(URL, {"dtn": "26DT9999999"}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(SavedDogrudanTemin.objects.exists())

    def test_tekrar_kaydet_upsert_grup_gonderilmezse_korunur(self):
        g = TenderGroup.objects.create(user=self.u, name="Ankara")
        self.assertEqual(self.c.post(URL, {"dtn": self.dt.dtn, "group": g.pk}, format="json").status_code, 201)
        self.assertEqual(self.c.post(URL, {"dtn": self.dt.dtn}, format="json").status_code, 200)
        self.assertEqual(SavedDogrudanTemin.objects.count(), 1)
        self.assertEqual(SavedDogrudanTemin.objects.get().group_id, g.pk)

    def test_baskasinin_klasorune_kaydedilemez(self):
        g = TenderGroup.objects.create(user=self.baska, name="X")
        r = self.c.post(URL, {"dtn": self.dt.dtn, "group": g.pk}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_durum_tasima_silme(self):
        g = TenderGroup.objects.create(user=self.u, name="Okullar")
        self.c.post(URL, {"dtn": self.dt.dtn}, format="json")
        d = _veri(self.c.get(f"{URL}{self.dt.dtn}/"))
        self.assertEqual(d, {"is_saved": True, "group": None, "group_name": None})
        r = self.c.patch(f"{URL}{self.dt.dtn}/", {"group": g.pk}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_veri(self.c.get(f"{URL}{self.dt.dtn}/"))["group_name"], "Okullar")
        self.assertEqual(self.c.delete(f"{URL}{self.dt.dtn}/").status_code, 204)
        self.assertEqual(self.c.delete(f"{URL}{self.dt.dtn}/").status_code, 204)  # idempotent
        self.assertFalse(_veri(self.c.get(f"{URL}{self.dt.dtn}/"))["is_saved"])

    def test_dt_satiri_silinse_de_kayit_kalir_acik_false(self):
        self.c.post(URL, {"dtn": self.dt.dtn}, format="json")
        self.dt.delete()  # ekap.mobil.dt.temizle 30 gün sonra siler
        liste = _veri(self.c.get(URL))
        self.assertEqual(len(liste), 1)
        self.assertEqual(liste[0]["ad"], f"{self.dt.dtn} mazot alımı")
        self.assertFalse(liste[0]["acik"])

    def test_liste_guncel_son_teklifi_kullanir(self):
        self.c.post(URL, {"dtn": self.dt.dtn}, format="json")
        yeni = timezone.now() + timedelta(days=3)
        DogrudanTemin.objects.filter(pk=self.dt.pk).update(son_teklif=yeni)
        d = _veri(self.c.get(URL))[0]
        self.assertGreater(d["kalan_dakika"], 60 * 24 * 2)

    def test_kullanici_yalniz_kendi_kayitlarini_gorur(self):
        SavedDogrudanTemin.objects.create(user=self.baska, dtn=self.dt.dtn)
        self.assertEqual(_veri(self.c.get(URL)), [])

    def test_klasor_sayilari_ayri_ve_carpilmaz(self):
        # ⚠️ İki Count JOIN'i distinct'siz 2 ihale × 3 DT = 6 sayardı.
        g = TenderGroup.objects.create(user=self.u, name="Karma")
        for i in range(2):
            SavedTender.objects.create(user=self.u, tender_ikn=f"2026/{i}", group=g)
        for i in range(3):
            SavedDogrudanTemin.objects.create(user=self.u, dtn=f"26DT200000{i}", group=g)
        satir = [x for x in _veri(self.c.get(GRUP_URL)) if x["id"] == g.pk][0]
        self.assertEqual(satir["tender_count"], 2)
        self.assertEqual(satir["dt_count"], 3)

    def test_klasor_silinince_dt_kaydi_genele_doner(self):
        g = TenderGroup.objects.create(user=self.u, name="Geçici")
        self.c.post(URL, {"dtn": self.dt.dtn, "group": g.pk}, format="json")
        self.assertEqual(self.c.delete(f"{GRUP_URL}{g.pk}/").status_code, 204)
        self.assertIsNone(SavedDogrudanTemin.objects.get().group_id)
