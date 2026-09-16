"""
Mobil keşif bölümlemesi testleri — **asıl hedef: kapsama garantisi**.

Tur artık önceki turun yapraklarından başlıyor (`_yapraklardan_yigin`). Bu, istek
sayısını ~48'den ~24'e indiriyor ama yanlış kurulursa pencerede **delik** bırakır ve
o aralıkta yayımlanan ilanlar **sessizce** hiç keşfedilmez — hata da üretmez. Bu
yüzden her senaryoda önce kapsama doğrulanır, sonra istek sayısı.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from ekap.mobil import constants as C
from ekap.mobil import tasks as T
from ekap.models import SyncCheckpoint

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# 2026-09-16 üretim turunun gerçek yaprakları (worker logundan).
_URETIM_YAPRAKLARI = [
    ["2026-09-13", "2026-09-14", 1, 0, 96], ["2026-09-15", "2026-09-16", 1, 0, 221],
    ["2026-09-17", "2026-09-19", 1, 0, 191], ["2026-09-20", "2026-09-21", 1, 0, 107],
    ["2026-09-22", "2026-09-22", 1, 0, 149], ["2026-09-23", "2026-09-24", 1, 0, 175],
    ["2026-09-25", "2026-09-25", 1, 0, 95], ["2026-09-26", "2026-09-28", 1, 0, 102],
    ["2026-09-29", "2026-09-30", 1, 0, 192], ["2026-10-01", "2026-10-01", 1, 0, 81],
    ["2026-10-02", "2026-10-04", 1, 0, 59], ["2026-10-05", "2026-10-07", 1, 0, 199],
    ["2026-10-08", "2026-10-31", 1, 0, 180],
    ["2026-09-13", "2026-09-16", 2, 0, 241], ["2026-09-17", "2026-09-19", 2, 0, 145],
    ["2026-09-20", "2026-09-22", 2, 0, 187], ["2026-09-23", "2026-09-25", 2, 0, 200],
    ["2026-09-26", "2026-10-01", 2, 0, 242], ["2026-10-02", "2026-10-07", 2, 0, 103],
    ["2026-10-08", "2026-10-31", 2, 0, 64],
    ["2026-09-13", "2026-09-19", 3, 0, 248], ["2026-09-20", "2026-09-25", 3, 0, 226],
    ["2026-09-26", "2026-10-01", 3, 0, 171], ["2026-10-02", "2026-10-07", 3, 0, 161],
    ["2026-10-08", "2026-10-31", 3, 0, 141],
    ["2026-09-13", "2026-10-31", 4, 0, 27],
]


def _kapsama_dogrula(test, yigin, bas, bit):
    """Her tür için dilimler `bas`→`bit` aralığını deliksiz ve örtüşmesiz örtmeli."""
    for tur in C.IHALE_TURU_DILIMLERI:
        dilimler = [d for d in yigin if d[2] == tur]
        test.assertTrue(dilimler, f"tür {tur} için dilim yok")
        test.assertEqual(date.fromisoformat(dilimler[0][0]), bas, f"tür {tur} başı")
        test.assertEqual(date.fromisoformat(dilimler[-1][1]), bit, f"tür {tur} sonu")
        for onceki, sonraki in zip(dilimler, dilimler[1:]):
            test.assertEqual(
                date.fromisoformat(sonraki[0]),
                date.fromisoformat(onceki[1]) + timedelta(days=1),
                f"tür {tur}: {onceki} ile {sonraki} arasında delik/örtüşme",
            )
        for d in dilimler:
            test.assertLessEqual(d[0], d[1])
            test.assertEqual(d[3], 0)


class YapraklardanYiginTest(TestCase):
    bas, bit = date(2026, 9, 14), date(2026, 11, 1)   # 2026-09-17 turunun penceresi

    def test_onceki_tur_yoksa_tur_basina_tam_pencere(self):
        yigin = T._yapraklardan_yigin([], self.bas, self.bit)
        self.assertEqual(
            yigin,
            [[self.bas.isoformat(), self.bit.isoformat(), t, 0]
             for t in C.IHALE_TURU_DILIMLERI],
        )

    def test_uretim_bolumlemesi_kaydirilinca_kapsama_tam_ve_istek_yariya_iner(self):
        yigin = T._yapraklardan_yigin(_URETIM_YAPRAKLARI, self.bas, self.bit)
        _kapsama_dogrula(self, yigin, self.bas, self.bit)
        # Aynı tur tam pencereyle başlayınca 48 istek sürmüştü.
        sayilar = {t: sum(1 for d in yigin if d[2] == t) for t in C.IHALE_TURU_DILIMLERI}
        self.assertEqual(sayilar, {1: 11, 2: 6, 3: 5, 4: 1})

    def test_kucuk_komsular_birlesir_buyukler_birlesmez(self):
        onceki = [
            ["2026-09-14", "2026-09-20", 1, 0, 60],
            ["2026-09-21", "2026-09-25", 1, 0, 70],     # 60+70 ≤ 200 → birleşir
            ["2026-09-26", "2026-10-05", 1, 0, 190],    # 130+190 > 200 → ayrı
            ["2026-10-06", "2026-11-01", 1, 0, 30],     # 190+30 > 200 → ayrı
        ]
        yigin = [d for d in T._yapraklardan_yigin(onceki, self.bas, self.bit) if d[2] == 1]
        self.assertEqual(yigin, [
            ["2026-09-14", "2026-09-25", 1, 0],
            ["2026-09-26", "2026-10-05", 1, 0],
            ["2026-10-06", "2026-11-01", 1, 0],
        ])

    def test_pencereden_dusen_gunler_atilir_yeni_gunler_son_dilime_eklenir(self):
        onceki = [
            ["2026-09-01", "2026-09-10", 1, 0, 180],    # tümüyle pencere dışı
            ["2026-09-11", "2026-09-20", 1, 0, 180],    # başı dışarıda kalır
            ["2026-09-21", "2026-10-20", 1, 0, 180],    # sonu bit'e uzatılır
        ]
        yigin = [d for d in T._yapraklardan_yigin(onceki, self.bas, self.bit) if d[2] == 1]
        self.assertEqual(yigin, [
            ["2026-09-14", "2026-09-20", 1, 0],
            ["2026-09-21", "2026-11-01", 1, 0],
        ])

    def test_pencere_daralirsa_sondaki_yapraklar_tek_dilimde_toplanir(self):
        onceki = [
            ["2026-09-14", "2026-10-20", 1, 0, 100],
            ["2026-10-21", "2026-11-10", 1, 0, 150],    # ikisi de bit'e kırpılır
            ["2026-11-11", "2026-11-20", 1, 0, 150],
        ]
        yigin = [d for d in T._yapraklardan_yigin(onceki, self.bas, self.bit) if d[2] == 1]
        self.assertEqual(yigin, [
            ["2026-09-14", "2026-10-20", 1, 0],
            ["2026-10-21", "2026-11-01", 1, 0],
        ])

    def test_bozuk_kayit_o_turu_tam_pencereye_dusurur(self):
        onceki = [["tarih-degil", "yine-degil", 1, 0, 10], *_URETIM_YAPRAKLARI[13:]]
        yigin = T._yapraklardan_yigin(onceki, self.bas, self.bit)
        _kapsama_dogrula(self, yigin, self.bas, self.bit)
        self.assertEqual(
            [d for d in yigin if d[2] == 1],
            [[self.bas.isoformat(), self.bit.isoformat(), 1, 0]],
        )

    def test_sayisi_olmayan_eski_kayit_dolu_sayilir(self):
        """Sayı yoksa birleştirilmez — eksik bilgiyle dilimi büyütmek taşırırdı."""
        onceki = [["2026-09-14", "2026-09-20", 1, 0], ["2026-09-21", "2026-11-01", 1, 0]]
        yigin = [d for d in T._yapraklardan_yigin(onceki, self.bas, self.bit) if d[2] == 1]
        self.assertEqual(len(yigin), 2)


@override_settings(CACHES=_LOCMEM)
class KesifAdimiYaprakKaydiTest(TestCase):
    def _cp(self, **extra):
        SyncCheckpoint.objects.update_or_create(name=T.CHECKPOINT, defaults={"extra": extra})

    def _extra(self):
        return SyncCheckpoint.objects.get(name=T.CHECKPOINT).extra

    def _calistir(self, kayit_sayilari):
        """Her istek sırayla `kayit_sayilari`ndaki kadar satır döndürür."""
        cli = MagicMock()
        cli.liste.side_effect = [[{}] * n for n in kayit_sayilari]
        with patch.object(T, "_satirlari_yaz", side_effect=lambda s: (len(s), 0)):
            return [T.kesif_adimi(cli) for _ in kayit_sayilari]

    def test_tur_bitince_yapraklar_bolumleme_olur(self):
        self._cp(yigin=[["2026-09-14", "2026-09-30", 1, 0]], yapraklar=[])
        # 250 → ikiye bölünür (yaprak değil), sonra iki yaprak.
        self._calistir([250, 120, 90])
        extra = self._extra()
        self.assertEqual(extra["yigin"], [])
        self.assertEqual(extra["yapraklar"], [])
        self.assertEqual(extra["son_bolumleme"], [
            ["2026-09-14", "2026-09-22", 1, 0, 120],
            ["2026-09-23", "2026-09-30", 1, 0, 90],
        ])

    def test_tur_surerken_onceki_bolumleme_korunur(self):
        eski = [["2026-09-14", "2026-11-01", 1, 0, 50]]
        self._cp(yigin=[["2026-09-14", "2026-09-20", 1, 0], ["2026-09-21", "2026-09-30", 1, 0]],
                 yapraklar=[], son_bolumleme=eski)
        self._calistir([40])
        extra = self._extra()
        self.assertEqual(extra["son_bolumleme"], eski)
        self.assertEqual(extra["yapraklar"], [["2026-09-14", "2026-09-20", 1, 0, 40]])

    def test_il_dilimleri_gun_duzeyinde_tek_dolu_yaprak_olur(self):
        self._cp(yigin=[["2026-09-20", "2026-09-20", 1, 6], ["2026-09-20", "2026-09-20", 1, 34]],
                 yapraklar=[])
        self._calistir([12, 30])
        self.assertEqual(
            self._extra()["son_bolumleme"],
            [["2026-09-20", "2026-09-20", 1, 0, C.LISTE_TAVAN]],
        )

    def test_yeni_tur_onceki_bolumlemeden_baslar_ve_yarim_yapraklari_siler(self):
        self._cp(yigin=[], yapraklar=[["2026-09-01", "2026-09-02", 1, 0, 5]],
                 son_bolumleme=_URETIM_YAPRAKLARI)
        with override_settings(EKAP_MOBIL_KESIF_GERI_GUN=3, EKAP_MOBIL_KESIF_ILERI_GUN=45), \
                patch.object(T.timezone, "localdate", return_value=date(2026, 9, 17)):
            yigin = T._kesif_yigini(olustur=True)
        _kapsama_dogrula(self, yigin, date(2026, 9, 14), date(2026, 11, 1))
        self.assertEqual(len(yigin), 23)
        extra = self._extra()
        self.assertEqual(extra["yigin"], yigin)
        self.assertEqual(extra["yapraklar"], [])
        self.assertEqual(extra["son_bolumleme"], _URETIM_YAPRAKLARI)
