"""Pro paketlerinin mağaza fiyatları — `GET /subscription/fiyatlar/` (web Pro sihirbazı)."""
from unittest.mock import patch

from django.test import TestCase

from core.models import AppSetting
from subscription.services import magaza_fiyat as mf

URL = "/api/v1/subscription/fiyatlar/"

APPS = {"items": [{"id": "app_ios", "type": "app_store"},
                  {"id": "app_and", "type": "play_store"},
                  {"id": "app_test", "type": "test_store"}]}


def _urun(app_id, kimlik, sure=None):
    u = {"object": "product", "app_id": app_id, "store_identifier": kimlik}
    if sure:
        u["subscription"] = {"duration": sure}
    return {"product": u, "eligibility_criteria": "all"}


def _offerings():
    return {"items": [
        {"lookup_key": "eski", "is_current": False, "packages": {"items": []}},
        {"lookup_key": "default", "is_current": True, "packages": {"items": [
            {"lookup_key": "$rc_annual", "position": 2, "products": {"items": [
                _urun("app_ios", "com.envisoft.ihaletakip.pro.yillik", "P1Y"),
                _urun("app_test", "test_yillik"),
            ]}},
            {"lookup_key": "$rc_monthly", "position": 1, "products": {"items": [
                _urun("app_ios", "com.envisoft.ihaletakip.pro.aylik"),
                _urun("app_and", "pro_abonelik:aylik"),
            ]}},
        ]}},
    ]}


class RcAyristirmaTest(TestCase):
    def test_current_offering_paketleri_sirali(self):
        p = mf.rc_paketleri_ayristir(_offerings(), APPS)
        self.assertEqual([x["rc_paket"] for x in p], ["$rc_monthly", "$rc_annual"])
        self.assertEqual(p[0]["urunler"], {"app_store": "com.envisoft.ihaletakip.pro.aylik",
                                           "play_store": "pro_abonelik:aylik"})
        # test_store ürünü atlanır; Play ürünü olmayan pakette anahtar hiç yok.
        self.assertEqual(p[1]["urunler"], {"app_store": "com.envisoft.ihaletakip.pro.yillik"})
        # Süre: ürünün kendisinden, yoksa paket anahtarından.
        self.assertEqual((p[0]["sure"], p[1]["sure"]), ("P1M", "P1Y"))

    def test_current_offering_yoksa_hata(self):
        with self.assertRaises(mf.MagazaFiyatHatasi):
            mf.rc_paketleri_ayristir({"items": [{"is_current": False}]}, APPS)


class AppStoreAyristirmaTest(TestCase):
    def _yanit(self, *kayitlar):
        data, included = [], [{"type": "territories", "id": "TUR",
                               "attributes": {"currency": "TRY"}}]
        for i, (baslangic, fiyat, korunan) in enumerate(kayitlar):
            data.append({"type": "subscriptionPrices", "id": f"p{i}",
                         "attributes": {"startDate": baslangic, "preserved": korunan},
                         "relationships": {
                             "subscriptionPricePoint": {"data": {"type": "subscriptionPricePoints",
                                                                 "id": f"pp{i}"}},
                             "territory": {"data": {"type": "territories", "id": "TUR"}}}})
            included.append({"type": "subscriptionPricePoints", "id": f"pp{i}",
                             "attributes": {"customerPrice": fiyat}})
        return {"data": data, "included": included}

    def test_yururlukteki_en_yeni_fiyat(self):
        y = self._yanit((None, "199.99", False), ("2026-09-01", "249.99", False),
                        ("2026-12-01", "299.99", False),  # ileri tarihli değişiklik
                        ("2026-10-01", "149.99", True))   # mevcut abonelere korunan
        self.assertEqual(mf.asc_fiyat_sec(y, "2026-10-29"),
                         {"fiyat": "249.99", "para_birimi": "TRY"})

    def test_fiyat_yoksa_none(self):
        self.assertIsNone(mf.asc_fiyat_sec({"data": []}, "2026-10-29"))

    def test_deneme(self):
        y = {"data": [
            {"attributes": {"offerMode": "PAY_AS_YOU_GO", "duration": "ONE_MONTH"}},
            {"attributes": {"offerMode": "FREE_TRIAL", "duration": "ONE_WEEK",
                            "endDate": "2026-01-01"}},  # bitmiş
            {"attributes": {"offerMode": "FREE_TRIAL", "duration": "TWO_WEEKS",
                            "numberOfPeriods": 1}},
        ]}
        self.assertEqual(mf.asc_deneme_sec(y, "2026-10-29"), "P2W")
        self.assertIsNone(mf.asc_deneme_sec({"data": []}, "2026-10-29"))


class PlayAyristirmaTest(TestCase):
    ABONELIK = {"basePlans": [
        {"basePlanId": "eski", "state": "INACTIVE", "regionalConfigs": [
            {"regionCode": "TR", "price": {"currencyCode": "TRY", "units": "99"}}]},
        {"basePlanId": "aylik", "state": "ACTIVE", "regionalConfigs": [
            {"regionCode": "US", "price": {"currencyCode": "USD", "units": "9", "nanos": 990000000}},
            {"regionCode": "TR", "newSubscriberAvailability": True,
             "price": {"currencyCode": "TRY", "units": "249", "nanos": 990000000}}]},
    ]}

    def test_tr_fiyati(self):
        self.assertEqual(mf.play_fiyat_sec(self.ABONELIK, "aylik"),
                         {"fiyat": "249.99", "para_birimi": "TRY"})
        self.assertIsNone(mf.play_fiyat_sec(self.ABONELIK, "eski"))  # aktif değil
        self.assertIsNone(mf.play_fiyat_sec(self.ABONELIK, "yok"))

    def test_deneme(self):
        teklifler = {"subscriptionOffers": [
            {"state": "INACTIVE", "regionalConfigs": [{"regionCode": "TR"}],
             "phases": [{"duration": "P1M", "regionalConfigs": [{"regionCode": "TR", "free": {}}]}]},
            {"state": "ACTIVE", "regionalConfigs": [{"regionCode": "TR",
                                                     "newSubscriberAvailability": True}],
             "phases": [{"duration": "P1W", "recurrenceCount": 2,
                         "regionalConfigs": [{"regionCode": "TR", "free": {}}]}]},
        ]}
        self.assertEqual(mf.play_deneme_sec(teklifler), "P2W")
        self.assertIsNone(mf.play_deneme_sec({}))

    def test_temel_plansiz_kimlik_tahmin_edilmez(self):
        istemci = object.__new__(mf.PlayIstemcisi)
        with patch.object(mf.PlayIstemcisi, "_get") as get:
            self.assertIsNone(istemci.fiyat("pro_abonelik"))
            get.assert_not_called()


class FiyatMetniTest(TestCase):
    def test_bicim(self):
        self.assertEqual(mf.fiyat_metni("249.990"), "249.99")
        self.assertEqual(mf.fiyat_metni("1999.9"), "1999.90")
        self.assertIsNone(mf.fiyat_metni("0"))
        self.assertIsNone(mf.fiyat_metni("abc"))
        self.assertIsNone(mf.fiyat_metni(None))


class _SahteAppStore:
    FIYATLAR = {"com.envisoft.ihaletakip.pro.aylik": {"fiyat": "249.99", "para_birimi": "TRY",
                                                     "deneme": None},
                "com.envisoft.ihaletakip.pro.yillik": {"fiyat": "1999.99", "para_birimi": "TRY",
                                                      "deneme": None}}

    def fiyat(self, urun):
        return self.FIYATLAR.get(urun)


class _Yapilandirilmamis:
    def __init__(self):
        raise mf.Yapilandirilmamis("yok")


class _BozukPlay:
    def fiyat(self, urun):
        raise mf.MagazaFiyatHatasi("Google Play: HTTP 500")


def _paketler():
    return mf.rc_paketleri_ayristir(_offerings(), APPS)


class GuncelleTest(TestCase):
    def _calistir(self, play=_Yapilandirilmamis, paketler=_paketler):
        with patch.object(mf, "rc_paketleri", paketler), \
                patch.dict(mf._ISTEMCILER, {"app_store": _SahteAppStore, "play_store": play}):
            return mf.guncelle()

    def test_yapilandirilmamis_magaza_null_digeri_gelir(self):
        self.assertTrue(self._calistir()["ok"])
        kayit = mf.oku()
        aylik = kayit["paketler"][0]
        self.assertEqual(aylik["kod"], "aylik")
        self.assertEqual(aylik["magazalar"]["app_store"]["fiyat"], "249.99")
        self.assertIsNone(aylik["magazalar"]["play_store"])
        self.assertEqual(kayit["paketler"][1]["kod"], "yillik")

    def test_hata_onceki_kaydi_ve_guncellenmeyi_korur(self):
        self._calistir()
        once = AppSetting.get(mf.KAYIT_ANAHTARI)
        sonuc = self._calistir(play=_BozukPlay)
        self.assertFalse(sonuc["ok"])
        self.assertEqual(AppSetting.get(mf.KAYIT_ANAHTARI), once)
        durum = mf.oku(mf.DURUM_ANAHTARI)
        self.assertFalse(durum["ok"])
        self.assertIn("HTTP 500", durum["hata"])

    def test_rc_hatasi_kaydi_korur(self):
        self._calistir()
        once = AppSetting.get(mf.KAYIT_ANAHTARI)

        def bozuk():
            raise mf.MagazaFiyatHatasi("RevenueCat: HTTP 403")

        self.assertFalse(self._calistir(paketler=bozuk)["ok"])
        self.assertEqual(AppSetting.get(mf.KAYIT_ANAHTARI), once)

    def test_hic_fiyat_yoksa_yazilmaz(self):
        with patch.dict(_SahteAppStore.FIYATLAR, {}, clear=True):
            self.assertFalse(self._calistir()["ok"])
        self.assertIsNone(mf.oku())


class UcTest(TestCase):
    def test_kayit_yoksa_bos_liste(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"], {"ulke": "TR", "guncellenme": None, "paketler": []})

    def test_kayit_doner_bayat_token_401_vermez(self):
        with patch.object(mf, "rc_paketleri", _paketler), \
                patch.dict(mf._ISTEMCILER, {"app_store": _SahteAppStore,
                                            "play_store": _Yapilandirilmamis}):
            mf.guncelle()
        r = self.client.get(URL, HTTP_AUTHORIZATION="Bearer bozuk.token")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["guncellenme"])
        self.assertEqual([p["kod"] for p in data["paketler"]], ["aylik", "yillik"])
        self.assertEqual(data["paketler"][1]["magazalar"]["app_store"]["fiyat"], "1999.99")
