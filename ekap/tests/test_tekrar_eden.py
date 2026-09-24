"""
Tekrar eden ihaleler ("beklenen ihaleler") — tahminin dürüstlüğü.

Test edilen arızalar **üretimde ölçüldü** (2026-09-24). Uç, mobilin varsayılan
parametreleriyle (90 gün, güven yüksek+orta) iki satır döndürüyordu ve ikisi de
uydurmaydı; filtresiz ilk sayfa ise şöyle başlıyordu:

    beklenen=2023-10-14  periyot=1145 gün  sapma=0  güven=orta    "4 Kalem Muhtelif Eldiven"
    beklenen=2023-12-24  periyot=1020 gün  sapma=0  güven=YÜKSEK  "TIBBİ CİHAZ ALIMI (5 KALEM)"

Üç ayrı mekanizma aynı anda çalışıyordu:

1. **Tek aralıktan "yüksek güven".** Aynı gün yayımlanan lotlar arası 0 günlük
   farklar süzülüp atılıyor, geriye tek aralık kalıyor, `pstdev([x]) == 0` oluyor ve
   güven ölçütü üye sayısına baktığı için `yuksek` yazılıyordu.
2. **`duzensiz` seriye de tarih yazılıyordu.** Aktif 202 serinin 156'sı düzensizdi
   ve hepsinde bir `beklenen_ilan_tarihi` vardı.
3. **Aktiflik penceresi periyodun 2 katıydı** → medyanı 1145 gün olan, 2020'de ölmüş
   bir seri 2026'da hâlâ "aktif" ve `order=beklenen` sıralamasında **listenin başında**.

⚠️ Kurgular gerçek vakaların küçültülmüş hâlidir; sayılar ölçülen değerlerden alındı.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from ekap import tasks
from ekap.constants import DURUM_IPTAL
from ekap.models import RecurringTenderSeries, Tender
from ekap.series import series_key


IPTAL = sorted(DURUM_IPTAL)[0]


def _uye(gun_once, durum=None):
    """`_donemler` bir sözlük listesi bekler (ilan_tarihi + ihale_durum)."""
    return {"ilan_tarihi": timezone.now() - timedelta(days=gun_once),
            "ihale_durum": durum}


class DonemBolmeTest(TestCase):
    """Bir 'tekrar' ihtiyacın yeniden doğmasıdır; aynı ihtiyacın parçaları değil."""

    def test_ayni_gun_yayimlanan_lotlar_tek_donem(self):
        # Kısımlı alımın 4 lotu aynı gün + bir yıl sonra aynısı → 2 dönem, 1 aralık.
        uyeler = [_uye(730)] * 4 + [_uye(365)]
        self.assertEqual(len(tasks._donemler(uyeler)), 2)

    def test_birkac_gun_arayla_yayimlanan_lotlar_tek_donem(self):
        uyeler = [_uye(740), _uye(735), _uye(726), _uye(365)]
        self.assertEqual(len(tasks._donemler(uyeler)), 2)

    def test_iptal_sonrasi_yeniden_ihale_ayni_donem(self):
        """Üretimde: 'Abdi İpekçi Okulu Otopark' 3 ayda 3 kez (ilk ikisi iptal) →
        eski kod medyan ~32 gün görüp **aylık otopark yapımı** tahmin ediyordu."""
        uyeler = [_uye(200, IPTAL), _uye(170, IPTAL), _uye(135)]
        self.assertEqual(len(tasks._donemler(uyeler)), 1,
                         "iptal → yeniden ihale zinciri tek ihtiyaçtır")

    def test_iptal_penceresi_uc_ayligi_YUTMAZ(self):
        """⚠️ İptal penceresi 60 gündür, 90+ DEĞİL: gerçek bir 3 aylık seri
        (75-110 gün) iptal yaşadığında dönemi yutulmamalı."""
        uyeler = [_uye(200, IPTAL), _uye(110), _uye(20)]
        self.assertEqual(len(tasks._donemler(uyeler)), 3)

    def test_zincirleme_birlesme_yok(self):
        """⚠️ Çapa DÖNEM BAŞIDIR, önceki ilan değil. Önceki ilana bakan bir kural
        20 gün arayla gelen 5 ilanı tek döneme akıtırdı (keyword katmanında
        12.309 üyelik mega-küme üreten geçişli kümelemenin aynı tuzağı)."""
        # 15 gün arayla 5 ilan: her biri ÖNCEKİNE 21 günden yakın. "Önceki ilana
        # bak" kuralı hepsini TEK döneme akıtırdı; dönem başına bakan kural
        # 0/30/60. günlerde yeni dönem açar → 3 dönem.
        uyeler = [_uye(100), _uye(85), _uye(70), _uye(55), _uye(40)]
        self.assertEqual(len(tasks._donemler(uyeler)), 3)


class GuvenTest(TestCase):
    def test_tek_aralik_asla_yuksek_olamaz(self):
        # Üretimdeki vaka: sapma=0, ama tek gözlemden.
        self.assertNotEqual(tasks._seri_guven(1, 1020, 0), "yuksek")

    def test_uc_duzenli_aralik_yuksek(self):
        self.assertEqual(tasks._seri_guven(3, 365, 20), "yuksek")

    def test_duzensiz_araliklar_dusuk(self):
        # 30/400/60/380 gün — üye sayısı bol ama düzen yok.
        self.assertEqual(tasks._seri_guven(4, 220, 180), "dusuk")


class BeklenenTest(TestCase):
    def test_duzensiz_seriye_TARIH_VERILMEZ(self):
        son = timezone.now() - timedelta(days=30)
        sonuc = tasks._beklenen(son, 1145, "duzensiz")
        self.assertIsNone(sonuc["beklenen_ilan_tarihi"])
        self.assertEqual(sonuc["beklenen_ay"], "")

    def test_yillik_seriye_tarih_verilir(self):
        son = timezone.now() - timedelta(days=300)
        sonuc = tasks._beklenen(son, 365, "yillik")
        self.assertIsNotNone(sonuc["beklenen_ilan_tarihi"])
        self.assertTrue(sonuc["aktif"])

    def test_mutlak_tavan_olu_seriyi_kapatir(self):
        """⚠️ Eski kural (2 × periyot) düzensiz serilerde 6 yıla uzanıyordu:
        medyanı 1145 gün olan, 2020'de ölmüş seri 2026'da hâlâ 'aktif'ti."""
        son = timezone.now() - timedelta(days=1500)
        self.assertFalse(tasks._beklenen(son, 1145, "duzensiz")["aktif"])

    def test_periyot_tipi_bantlari(self):
        self.assertEqual(tasks._periyot_tipi(365), "yillik")
        # ⚠️ Gerçek yıllık işler takvimde kayar; dar band onları tahminsiz bırakıyordu.
        self.assertEqual(tasks._periyot_tipi(428), "yillik")
        self.assertEqual(tasks._periyot_tipi(1145), "duzensiz")
        self.assertEqual(tasks._periyot_tipi(28), "aylik")


class TespitGoreviTest(TestCase):
    """Uçtan uca: görev gerçek ihalelerden dürüst bir seri üretir mi?"""

    def _ihale(self, i, ad, gun_once, durum=15):
        t = Tender.objects.create(
            ikn=f"2025/{i}", ekap_id=f"e{i}", ihale_adi=ad,
            idare_id="42", idare_adi="Test Müdürlüğü", ihale_durum=durum,
            ilan_tarihi=timezone.now() - timedelta(days=gun_once),
        )
        t.seri_anahtar = series_key(t.idare_id, t.ihale_adi)
        t.save(update_fields=["seri_anahtar"])
        return t

    def test_yillik_seri_tahmin_uretir(self):
        for i, gun in enumerate((1100, 735, 370, 5)):
            self._ihale(i, f"{2022 + i} Yılı Kuru Gıda Alımı", gun)
        tasks.detect_recurring_series()
        s = RecurringTenderSeries.objects.get()
        self.assertEqual(s.donem_sayisi, 4)
        self.assertEqual(s.periyot_tip, "yillik")
        self.assertEqual(s.guven, "yuksek")
        self.assertIsNotNone(s.beklenen_ilan_tarihi)
        self.assertTrue(s.aktif)

    def test_lot_yigini_seri_SAYILMAZ(self):
        """4 ihale ama hepsi aynı hafta → tek dönem → tekrar yok."""
        for i, gun in enumerate((400, 398, 395, 393)):
            self._ihale(i, f"Muhtelif Tıbbi Sarf Malzeme Alımı Kısım {i}", gun)
        tasks.detect_recurring_series()
        self.assertEqual(RecurringTenderSeries.objects.count(), 0)

    def test_duzensiz_seri_listelenir_ama_TARIHSIZ(self):
        for i, gun in enumerate((2000, 1200, 100)):
            self._ihale(i, f"Muhtelif Yedek Parça Alımı {i}", gun)
        tasks.detect_recurring_series()
        s = RecurringTenderSeries.objects.get()
        self.assertEqual(s.periyot_tip, "duzensiz")
        self.assertIsNone(s.beklenen_ilan_tarihi,
                          "düzensiz seriye tarih yazmak uydurmaktır")
