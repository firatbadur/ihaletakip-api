"""
İhale listesi sıralaması — `views.tender_sira_*` + `sirala_sayfala`.

Üç ayrı üretim hatasını kilitler (hepsi 2026-09-22'de ölçüldü). Ortak özellikleri:
**hata vermiyorlardı**, yalnızca yanlış sıra döndürüyorlardı — yani ancak test
tutabilir.

1. `ORDER BY ilan_tarihi DESC` Postgres'te NULLS FIRST'tür → kolonun %48,2'si boş
   olduğu için "en yeni ilan" ilk yarım milyon satırda tarihsiz kayıt gösteriyordu.
2. Tie-break yoktu → aynı damgayı paylaşan yüzlerce ihalenin (tek gün 897 kayıt)
   iç sırası plana kalıyordu; `OFFSET` sayfalamasında kayıt tekrarı/kaybı.
3. `order`/`siralamaTipi` ham string karşılaştırılıyordu → `ilanTarihi` (camelCase)
   ve `ASC` (büyük harf) sessizce başka bir sıralamaya düşüyordu.
"""

from datetime import datetime, timezone as dt_tz

from django.http import QueryDict
from django.test import TestCase

from ekap.models import Tender
from ekap.views import (
    sirala_sayfala,
    tender_sira_alani,
    tender_sira_artan,
    tender_sira_ifadesi,
)


def _dt(y, m, d, h=10):
    return datetime(y, m, d, h, 0, tzinfo=dt_tz.utc)


class SiraParametreTest(TestCase):
    """Parametre normalizasyonu — sessiz düşmeyi engeller."""

    def test_camel_case_ilan_tarihi_taninir(self):
        for deger in ("ilan_tarihi", "ilanTarihi", "ILAN_TARIHI", "ilantarihi"):
            self.assertEqual(tender_sira_alani(deger), "ilan_tarihi", deger)

    def test_ihale_tarihi_ve_bilinmeyen_varsayilana_duser(self):
        for deger in ("ihale_tarihi", "ihaleTarihi", None, "", "gecersiz"):
            self.assertEqual(tender_sira_alani(deger), "ihale_tarihi", deger)

    def test_yon_buyuk_harf_ve_es_anlamlilar(self):
        for deger in ("asc", "ASC", " Asc ", "artan", "ascending"):
            self.assertTrue(tender_sira_artan(deger), deger)
        for deger in ("desc", "DESC", None, "", "gecersiz"):
            self.assertFalse(tender_sira_artan(deger), deger)


class SiraIfadesiTest(TestCase):
    """Tie-break ve nulls_last kararlarının yapısı."""

    def test_her_yonde_tie_break_var(self):
        """⚠️ Tie-break olmadan sayfalama kararsız — kaldırılmasını engeller."""
        for alan in ("ihale_tarihi", "ilan_tarihi"):
            for artan in (True, False):
                ifade = tender_sira_ifadesi(alan, artan)
                self.assertEqual(len(ifade), 2, (alan, artan))
                self.assertEqual(ifade[1], "pk" if artan else "-pk", (alan, artan))

    def test_ihale_tarihi_nulls_last_kullanmaz(self):
        """
        ⚠️ Kolonda NULL yok; `NULLS LAST` istemek `(il_id, -ihale_tarihi)` gibi
        TÜM bileşik indeksleri devre dışı bırakır (EXPLAIN: paralel seq scan).
        Düz string ifade, indeks uyumunun göstergesi.
        """
        self.assertEqual(tender_sira_ifadesi("ihale_tarihi", False)[0], "-ihale_tarihi")
        self.assertEqual(tender_sira_ifadesi("ihale_tarihi", True)[0], "ihale_tarihi")

    def test_ilan_tarihi_nulls_last_ister(self):
        for artan in (True, False):
            birincil = tender_sira_ifadesi("ilan_tarihi", artan)[0]
            self.assertTrue(
                getattr(birincil, "nulls_last", False),
                f"ilan_tarihi (artan={artan}) nulls_last taşımalı",
            )


class SiralamaSonucTest(TestCase):
    """Uçtan uca: gerçek satırlarla dönen sıra."""

    @classmethod
    def setUpTestData(cls):
        # 3 dolu + 2 boş ilan tarihi. İki kayıt AYNI ihale_tarihi damgasını
        # paylaşıyor (tie-break sınaması).
        cls.satirlar = [
            ("2026/1", _dt(2026, 3, 1), _dt(2026, 1, 10)),
            ("2026/2", _dt(2026, 3, 5), _dt(2026, 1, 20)),
            ("2026/3", _dt(2026, 3, 5), _dt(2026, 1, 15)),  # aynı ihale_tarihi
            ("2026/4", _dt(2026, 3, 9), None),              # ilan tarihi yok
            ("2026/5", _dt(2026, 3, 9), None),              # ilan tarihi yok
        ]
        for ikn, ihale, ilan in cls.satirlar:
            Tender.objects.create(
                ikn=ikn, ekap_id=f"e{ikn[-1]}", ihale_adi=f"İş {ikn}",
                ihale_tarihi=ihale, ilan_tarihi=ilan,
            )

    def _iknler(self, **params):
        qs, _, _ = sirala_sayfala(
            Tender.objects.all(), QueryDict("&".join(f"{k}={v}" for k, v in params.items())),
            varsayilan_boyut=10,
        )
        return [t.ikn for t in qs]

    def test_ilan_tarihi_desc_bos_kayitlar_sonda(self):
        """⚠️ Asıl arıza: NULL'lar Postgres varsayılanıyla BAŞTA geliyordu."""
        sira = self._iknler(order="ilan_tarihi", siralamaTipi="desc")
        self.assertEqual(sira[:3], ["2026/2", "2026/3", "2026/1"])
        self.assertEqual(set(sira[3:]), {"2026/4", "2026/5"}, "boşlar sonda olmalı")

    def test_ilan_tarihi_asc_bos_kayitlar_yine_sonda(self):
        sira = self._iknler(order="ilan_tarihi", siralamaTipi="asc")
        self.assertEqual(sira[:3], ["2026/1", "2026/3", "2026/2"])
        self.assertEqual(set(sira[3:]), {"2026/4", "2026/5"})

    def test_camel_case_gercek_sirayi_degistirir(self):
        """`ilanTarihi` artık `ihale_tarihi`ye düşmemeli (eski sessiz hata)."""
        self.assertEqual(
            self._iknler(order="ilanTarihi", siralamaTipi="desc"),
            self._iknler(order="ilan_tarihi", siralamaTipi="desc"),
        )

    def test_buyuk_harf_asc_gercekten_artan(self):
        self.assertEqual(
            self._iknler(order="ihale_tarihi", siralamaTipi="ASC"),
            self._iknler(order="ihale_tarihi", siralamaTipi="asc"),
        )

    def test_esit_damgada_sira_deterministik(self):
        """
        ⚠️ Aynı `ihale_tarihi`ye sahip iki kayıt her çağrıda AYNI sırada gelmeli;
        aksi hâlde `OFFSET` sayfalaması kayıt tekrarlar/kaybeder.
        """
        ilk = self._iknler(order="ihale_tarihi", siralamaTipi="desc")
        for _ in range(5):
            self.assertEqual(self._iknler(order="ihale_tarihi", siralamaTipi="desc"), ilk)

    def test_sayfalar_ortusmez_ve_hepsini_kapsar(self):
        """Sayfalama bütünlüğü: 5 kaydın 3+2'si, tekrar yok, kayıp yok."""
        for order in ("ihale_tarihi", "ilan_tarihi"):
            s1 = self._iknler(order=order, siralamaTipi="desc", page=1, page_size=3)
            s2 = self._iknler(order=order, siralamaTipi="desc", page=2, page_size=3)
            self.assertEqual(len(s1), 3, order)
            self.assertEqual(len(s2), 2, order)
            self.assertEqual(set(s1) & set(s2), set(), f"{order}: sayfalar örtüştü")
            self.assertEqual(set(s1) | set(s2), {i for i, _, _ in self.satirlar}, order)
