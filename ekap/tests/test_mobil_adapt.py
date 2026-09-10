"""
Mobil adapter testleri — **asıl hedef: koruyucu yazma regresyonu**.

Mobil API v2'nin verdiği bazı alanları hiç vermiyor (`idare_id`, `enUstIdareKod`,
`ihaleOzellikList`, `ihtiyacKalemiOkasList`, `sozlesmeBilgiList`). Bu alanların
mobil kaynaklı bir tazelemede **silinmediği** burada garanti altına alınır; 2026-08-27'de
üretimde yaşanan `_LISTE_EZMEZ` arızasının (bir liste turu `ilan_tarihi`yi 157/213 →
0 yaptı) mobil sürümü tam olarak bu olurdu ve yine **sessizce** olurdu.
"""
from decimal import Decimal

from django.test import TestCase

from ekap import sync as sync_mod
from ekap.mobil import adapt
from ekap.mobil import okas as okas_mod
from ekap.models import Announcement, Contract, OkasCode, OkasItem, Tender


class DurumMetniTest(TestCase):
    def test_bilinen_metinler(self):
        self.assertEqual(adapt.durum_kodu("Sonuç İlanı Yayımlanmış"), 15)
        self.assertEqual(
            adapt.durum_kodu("İhale İlanı Yayımlanmış/İlansız, Katılıma Açık"), 2
        )
        self.assertEqual(adapt.durum_kodu("İptal Edilmiş"), 6)

    def test_bilinmeyen_metin_kod_uretmez(self):
        """⚠️ Uydurma kod, `DURUM_SONUCLANMIS` üzerine kurulu her şeyi bozardı."""
        self.assertIsNone(adapt.durum_kodu("Zamazingo Durumu"))
        self.assertIsNone(adapt.durum_kodu(""))

    def test_kapsam_tur_usul(self):
        self.assertEqual(adapt.kapsam_tur_usul("4734 Kapsamında - Mal - Açık"), (1, 1, 1))
        # ⚠️ Üçüncü parça usul değil madde numarası → usul boş kalmalı, uydurulmamalı.
        kapsam, tip, usul = adapt.kapsam_tur_usul("İstisna - Hizmet - 4734 / 3-g")
        self.assertEqual((kapsam, tip), (3, 3))
        self.assertIsNone(usul)


class EkapIdTest(TestCase):
    def test_gercek_id_korunur(self):
        """v2'den gelen gerçek `ekap_id` sentetik id ile EZİLMEMELİ (yedek yol kapanır)."""
        Tender.objects.create(ikn="2026/1", ekap_id="998877")
        self.assertEqual(adapt.ekap_id_coz("2026/1"), "998877")

    def test_yeni_ihaleye_sentetik_id(self):
        # ⚠️ `/` yok: `ekap_id` yol parametresi olarak kullanılıyor (`<str:>` `/` eşleştirmez).
        self.assertEqual(adapt.ekap_id_coz("2026/2"), "mobil:2026-2")
        self.assertNotIn("/", adapt.ekap_id_coz("2026/2"))


class KoruyucuYazmaTest(TestCase):
    """v2 verisiyle dolu bir ihale, mobil detayla tazelendiğinde bozulmamalı."""

    def setUp(self):
        self.t = Tender.objects.create(
            ikn="2026/500", ekap_id="12345",
            ihale_adi="TEST ALIMI", idare_adi="TEST İDARESİ",
            idare_id="28484", okas_ana_kod="15100000", okas_ana_adi="Et",
            okas_bucket="1510", okas_kalem_sayisi=3,
            en_ust_idare_kod="9001", en_ust_idare_adi="SAĞLIK BAKANLIĞI",
            ozellikler=["E_IHALE", "KISMI_TEKLIF_VEREBILIR"],
            sozlesme_sayisi=2, toplam_sozlesme_bedeli=Decimal("100.00"),
            ihale_durum=15,
        )
        Contract.objects.create(tender=self.t, ekap_sozlesme_id="9001",
                                yuklenici_adi="ESKİ FİRMA")
        OkasItem.objects.create(tender=self.t, kodu="15100000", adi="Et")

    def _mobil_detay(self):
        return {
            "ikn": "2026/500",
            "ihaleAdi": "TEST ALIMI",
            "idareAdi": "TEST İDARESİ",
            "idareIlAdi": "ANKARA",
            "ihaleTarihi": "02.10.2026 10:00",
            "ihaleDurumu": "Sonuç İlanı Yayımlanmış",
            "ihaleKapsamTurUsul": "4734 Kapsamında - Mal - Açık",
            "bagliOlduguEnUstIdare": "BELEDİYELER",
            "ihaleIlani": {"ilanTarihi": "08.09.2026 00:00:00",
                           "ilanHtml": "<p>ilan</p>", "ilanTipi": 1},
        }

    def test_mobil_tazeleme_v2_verisini_silmez(self):
        govde = adapt.detaydan("2026/500", self._mobil_detay())
        sync_mod.upsert_tender_detail("12345", govde, koruyucu=True)
        self.t.refresh_from_db()

        # OKAS / bakanlık / özellik etiketleri korunmalı (mobil bunları vermiyor).
        self.assertEqual(self.t.okas_ana_kod, "15100000")
        self.assertEqual(self.t.okas_kalem_sayisi, 3)
        self.assertEqual(self.t.en_ust_idare_kod, "9001")
        self.assertEqual(self.t.ozellikler, ["E_IHALE", "KISMI_TEKLIF_VEREBILIR"])
        # ⚠️ Mobil ad üzerinden idare_id ÜRETMEZ (ölçüm: %11,7 yanlış eşleşme).
        self.assertEqual(self.t.idare_id, "28484")
        # ⚠️ Sözleşmeler ve para özeti silinmemeli.
        self.assertEqual(self.t.sozlesmeler.count(), 1)
        self.assertEqual(self.t.sozlesme_sayisi, 2)
        self.assertEqual(OkasItem.objects.filter(tender=self.t).count(), 1)
        # Mobilin gerçekten getirdiği alanlar yazılmış olmalı.
        self.assertIsNotNone(self.t.ilan_tarihi)
        self.assertEqual(self.t.detay_kaynak, "")  # görev yazar, sync değil
        self.assertEqual(self.t.ihale_durum, 15)

    def test_v2_yolu_hala_ezer(self):
        """`koruyucu=False` (v2) davranışı DEĞİŞMEMELİ: orada boş = 'artık yok'."""
        sync_mod.upsert_tender_detail(
            "12345", {"item": {"ikn": "2026/500", "ihaleBilgi": {}, "idare": {}}},
        )
        self.t.refresh_from_db()
        self.assertEqual(self.t.okas_ana_kod, "")
        self.assertEqual(self.t.ozellikler, [])


class OkasCikarTest(TestCase):
    def test_katalogla_kesistirir(self):
        OkasCode.objects.create(kod="15100000", adi="Et")
        html = "<p>Kalem 15100000 · tarih 20260904 · tutar 12345678 · kod 99999999</p>"
        self.assertEqual(okas_mod.okas_cikar(html), [{"kodu": "15100000", "adi": "Et"}])

    def test_katalog_bossa_bos_liste(self):
        """⚠️ Boş liste 'veri yok' demektir; çağıran alana dokunmamalı."""
        self.assertEqual(okas_mod.okas_cikar("<p>15100000</p>"), [])


class SonucGovdesiTest(TestCase):
    """Sonuç ilanı → sözleşme; para HTML'den, tam ünvan ve kısım beyanı XML'den."""

    HTML = (
        "<p>2- İhale konusu işin Yaklaşık Maliyeti : 65596282,36 TRY "
        "Sözleşmeye Esas Kısımlarının Yaklaşık Maliyeti : 2220750,00 TRY "
        "3- Teklifler Toplam Teklif Sayısı : 6 Toplam Geçerli Teklif Sayısı : 6 "
        "4- Sözleşmenin b) Bedeli : 1386000,00 TRY "
        "d) Yüklenicisi : OSEKA ÖZEL SAĞLIK HİZMETLERİ "
        "e) Yüklenicinin uyruğu : Türkiye</p>"
    )
    XML = (
        "<SonucIlan><IhaleKazanan>OSEKA ÖZEL SAĞLIK HİZMETLERİ MEDİKAL LTD ŞTİ</IhaleKazanan>"
        "<SozlesmeTarih>12.03.2025</SozlesmeTarih><SozlesmeBedel>1386000</SozlesmeBedel>"
        "<IhaleKisimYMGosterilsinMi>1</IhaleKisimYMGosterilsinMi>"
        "<IhaleYaklasikMaliyet>65596282.36</IhaleYaklasikMaliyet>"
        "<IhaleKisimYaklasikMaliyet>2220750.00</IhaleKisimYaklasikMaliyet></SonucIlan>"
    )

    def test_sozlesme_yazilir(self):
        t = Tender.objects.create(ikn="2024/9", ekap_id="mobil:2024-9")
        govde = adapt.sonuc_govdesi("2024/9", [
            {"ilanHtml": self.HTML, "ilanXml": self.XML,
             "ilanTarihi": "20.03.2025 00:00:00", "ilanTipi": 4},
        ])
        sync_mod.sync_contracts_from_raw(t, detail=govde, buda=False)
        c = Contract.objects.get(tender=t)
        # Tam ünvan XML'den gelmeli — HTML ayrıştırıcısı adı kesiyor ve kesilmiş ad
        # `contractors.canonical_key` üzerinden mükerrer firma doğurur.
        self.assertIn("MEDİKAL", c.yuklenici_adi)
        self.assertEqual(c.sozlesme_bedeli_num, Decimal("1386000.00"))
        # Kısım maliyeti: kaynak "gösteriliyor" dediği için değer YAZILMALI.
        self.assertEqual(c.yaklasik_maliyet_num, Decimal("2220750.00"))
        self.assertEqual(c.yaklasik_maliyet_kaynak, "sonuc_ilani")
        self.assertIsNotNone(c.indirim_orani)
        self.assertTrue(c.ekap_sozlesme_id.startswith(adapt.SOZLESME_ONEK))
        self.assertEqual(Announcement.objects.filter(tender=t, ilan_tip=4).count(), 1)

    def test_kisim_beyani_yoksa_deger_yazilmaz(self):
        """⚠️ `IhaleKisimYMGosterilsinMi=0` → kısım maliyeti BİLİNMİYOR, sıfır değil."""
        xml = self.XML.replace(
            "<IhaleKisimYMGosterilsinMi>1<", "<IhaleKisimYMGosterilsinMi>0<"
        )
        t = Tender.objects.create(ikn="2024/10", ekap_id="mobil:2024-10")
        govde = adapt.sonuc_govdesi("2024/10", [
            {"ilanHtml": self.HTML, "ilanXml": xml, "ilanTarihi": "", "ilanTipi": 4},
        ])
        sync_mod.sync_contracts_from_raw(t, detail=govde, buda=False)
        c = Contract.objects.get(tender=t)
        self.assertIsNone(c.yaklasik_maliyet_num)
        self.assertIsNone(c.indirim_orani)
        # İhalenin TOPLAM maliyeti doğrudur, korunur.
        self.assertEqual(c.tender_yaklasik_maliyet_num, Decimal("65596282.36"))

    def test_anahtar_kararli(self):
        """Aynı içerik → aynı anahtar (konum değişse bile satır yetim kalmamalı)."""
        kayit = {"ilanHtml": self.HTML, "ilanXml": self.XML, "ilanTarihi": "", "ilanTipi": 4}
        a = adapt.sonuc_govdesi("2024/9", [kayit])["item"]["sozlesmeBilgiList"][0]["id"]
        b = adapt.sonuc_govdesi("2024/9", [{"ilanHtml": "x", "ilanXml": ""}, kayit])
        b = b["item"]["sozlesmeBilgiList"][1]["id"]
        self.assertEqual(a, b)


class CaptchaEkraniTest(TestCase):
    """Operatör ekranı staff'a açık, herkese kapalı olmalı."""

    def test_staff_gorur_anonim_goremez(self):
        from django.contrib.auth import get_user_model

        url = "/admin/ekap/mobil-captcha/"
        self.assertEqual(self.client.get(url).status_code, 302)  # girişe yönlendirir

        User = get_user_model()
        User.objects.create_superuser("op", "op@example.com", "parola12345")
        self.client.login(username="op", password="parola12345")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Bekleyen captcha yok")


class DokumanUcuTest(TestCase):
    """Mobil kaynaklı ihalede `document-url` v2'ye gitmez, proxy adresini döner."""

    def test_sentetik_id_proxy_dondurur(self):
        Tender.objects.create(ikn="2026/777", ekap_id="mobil:2026-777")
        resp = self.client.get("/api/v1/ekap/tenders/mobil:2026-777/document-url/")
        self.assertEqual(resp.status_code, 200)
        veri = resp.json()["data"]
        self.assertTrue(veri["proxy"])
        self.assertIn("/document/", veri["url"])
