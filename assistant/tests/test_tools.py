"""
Asistan araç katmanı testleri — LLM ÇAĞIRMAZ.

Buradaki her test, model olmadan doğrulanabilen bir sözleşmeyi korur:
araçların asla exception sızdırmaması, premium kapısının delinmemesi, kırpma
tavanının tutması ve kart havuzunun dolması. Bunlar bozulursa asistan sessizce
yanlış davranır (Free kullanıcıya Pro veri, prompt'u şişiren kırpılmamış sonuç,
uydurma İKN) — LLM'li bir test bunları güvenilir biçimde yakalayamaz.
"""
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase

from assistant.tools import TOOL_IMPL, TOOL_SPECS
from assistant.tools.context import ToolContext
from assistant.tools.trim import AZAMI_KARAKTER, butceye_sigdir
from ekap.models import Contract, Contractor, Tender


def _ctx(premium=True, user=None):
    return ToolContext(user=user, premium=premium)


class AracKatalogTests(TestCase):
    def test_spec_ve_impl_ayrismaz(self):
        self.assertEqual({t["name"] for t in TOOL_SPECS}, set(TOOL_IMPL))

    def test_semalar_gecerli(self):
        for t in TOOL_SPECS:
            self.assertIn("name", t)
            self.assertIn("description", t)
            self.assertEqual(t["input_schema"]["type"], "object")
            self.assertIsInstance(t["input_schema"]["properties"], dict)

    def test_sira_sabit(self):
        """
        `tools` bloğu prompt cache önekinde render edilir; sıra değişirse cache havuzu
        bölünür ve her mesaj tam fiyattan ödenir. Yeni araç SONA eklenmeli.
        """
        self.assertEqual(
            [t["name"] for t in TOOL_SPECS],
            ["ihale_ara", "ihale_detay", "okas_ara", "idare_profili",
             "firma_ara", "firma_profili", "firma_isleri", "kullanicinin_verisi",
             "idare_ara"],
        )


class SozlesmeTests(TestCase):
    """Her araç, ne verilirse verilsin, `ok` anahtarlı bir sözlük döner."""

    def test_bos_parametreyle_hicbiri_patlamaz(self):
        ctx = _ctx()
        for ad, fn in TOOL_IMPL.items():
            with self.subTest(arac=ad):
                sonuc = fn(ctx)
                self.assertIsInstance(sonuc, dict, f"{ad} sözlük dönmedi")
                self.assertIn("ok", sonuc, f"{ad} 'ok' alanı yok")

    def test_sacma_parametre_exception_sizdirmaz(self):
        from assistant.services.agent import _guvenli_calistir

        ctx = _ctx()
        self.assertFalse(_guvenli_calistir(ctx, "yok_boyle_arac", {})["ok"])
        self.assertFalse(_guvenli_calistir(ctx, "ihale_ara", "sozluk_degil")["ok"])
        self.assertFalse(_guvenli_calistir(ctx, "ihale_ara", {"olmayan_param": 1})["ok"])


class PremiumKapisiTests(TestCase):
    """
    ⚠️ Bu sınıf gelir koruması. Maskeleme view katmanındadır; araçlar servis
    fonksiyonlarını DOĞRUDAN çağırdığı için kapıyı kendileri tutmak zorunda.
    """

    def test_idare_profili_free_kullaniciya_kapali(self):
        sonuc = TOOL_IMPL["idare_profili"](_ctx(premium=False), idare_id="1")
        self.assertFalse(sonuc["ok"])
        self.assertTrue(sonuc.get("kilitli"))

    def test_pro_filtre_free_kullaniciya_kapali(self):
        sonuc = TOOL_IMPL["ihale_ara"](_ctx(premium=False), q="okul", sonuclanmis=True)
        self.assertFalse(sonuc["ok"])
        self.assertTrue(sonuc.get("kilitli"))
        self.assertIn("sonuclanmis", sonuc.get("pro_parametreler", []))

    def test_temel_arama_free_kullaniciya_acik(self):
        self.assertTrue(TOOL_IMPL["ihale_ara"](_ctx(premium=False), q="okul")["ok"])

    def test_pro_parametre_listesi_ekap_ile_ayni_kaynak(self):
        """Kopyalanmış bir liste zamanla ayrışır; import edildiğini doğrula."""
        from ekap.views import _PRO_PARAMS

        self.assertIn("yaklasik_maliyet_min", _PRO_PARAMS)
        sonuc = TOOL_IMPL["ihale_ara"](_ctx(premium=False), yaklasik_maliyet_min=1)
        self.assertTrue(sonuc.get("kilitli"))


class VeriTests(TestCase):
    def setUp(self):
        # ⚠️ `ekap.views._cached_count` sonucu 600 sn cache'ler ve anahtar yalnızca
        # FİLTRE parametrelerinden üretilir — veritabanından değil. Test sınıfları aynı
        # süreçte aynı `q=okul` sorgusunu farklı fixture'larla çalıştırınca ilk sınıfın
        # sonucu (0) ikincisine sızıyordu. Üretimde bu doğru davranış (aynı filtre = aynı
        # sayı), testte izolasyon gerekiyor.
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        cls.tender = Tender.objects.create(
            ekap_id="test-ekap-1", ikn="2026/999001",
            ihale_adi="Test Okul Onarım İşi", ihale_adi_norm="test okul onarim isi",
            idare_adi="TEST İL ÖZEL İDARESİ", idare_adi_norm="test il ozel idaresi",
            idare_id="9001", ihale_il_adi="ANKARA", il_id=251, ihale_tip=2,
            ihale_durum=2, sozlesme_sayisi=1,
            toplam_sozlesme_bedeli=Decimal("1000000.00"),
        )
        cls.firma = Contractor.objects.create(
            kanonik_ad="TEST YAPI LTD ŞTİ", kanonik_anahtar="test yapi ltd sti",
            arama_norm="test yapi ltd sti", sozlesme_sayisi=1, ihale_sayisi=1,
            toplam_sozlesme_bedeli=Decimal("1000000.00"),
        )
        Contract.objects.create(
            tender=cls.tender, yuklenici=cls.firma,
            sozlesme_bedeli_num=Decimal("1000000.00"), il_id=251, ihale_tip=2,
        )

    def test_ihale_ara_kart_havuzunu_doldurur(self):
        ctx = _ctx()
        sonuc = TOOL_IMPL["ihale_ara"](ctx, q="okul")
        self.assertTrue(sonuc["ok"])
        self.assertEqual(sonuc["toplam"], 1)
        # Kart havuzu = uydurma İKN engeli; dolmazsa model hiç kart gösteremez.
        self.assertIn("2026/999001", ctx.card_pool)
        self.assertIn("2026/999001", ctx.son_grup)

    def test_ihale_detay_null_yaklasik_maliyeti_korur(self):
        """`yaklasik_maliyet` null 'veri yok'tur; 0'a çevrilirse model yanlış konuşur."""
        sonuc = TOOL_IMPL["ihale_detay"](_ctx(), key="2026/999001")
        self.assertTrue(sonuc["ok"])
        self.assertIsNone(sonuc["yaklasik_maliyet"])
        self.assertEqual(sonuc["toplam_sozlesme_bedeli"], "1000000.00")  # string kalmalı

    def test_ihale_detay_bulunamayinca_duzgun_hata(self):
        sonuc = TOOL_IMPL["ihale_detay"](_ctx(), key="2099/1")
        self.assertFalse(sonuc["ok"])
        self.assertIn("bulunamadı", sonuc["hata"])

    def test_firma_araclari(self):
        ctx = _ctx()
        arama = TOOL_IMPL["firma_ara"](ctx, q="test yapi")
        self.assertTrue(arama["ok"])
        self.assertEqual(arama["liste"][0]["id"], self.firma.pk)

        profil = TOOL_IMPL["firma_profili"](ctx, contractor_id=self.firma.pk)
        self.assertTrue(profil["ok"])
        # "Kazanma oranı yok" notu her yanıtta taşınmalı — modelin en sık hatası.
        self.assertIn("Kazanma oranı", profil["not"])

        isler = TOOL_IMPL["firma_isleri"](ctx, contractor_id=self.firma.pk)
        self.assertTrue(isler["ok"])
        self.assertEqual(isler["liste"][0]["ikn"], "2026/999001")

    def test_kisa_terim_reddedilir(self):
        """Trigram indeksi 3 karakterden kısasında çalışmaz — araç önden eler."""
        self.assertFalse(TOOL_IMPL["okas_ara"](_ctx(), terimler=["ab"])["ok"])
        self.assertFalse(TOOL_IMPL["firma_ara"](_ctx(), q="ab")["ok"])


class KirpmaTests(TestCase):
    def test_tavan_asilinca_kuyruk_atilir(self):
        buyuk = {"ok": True, "toplam": 900, "liste": [{"ad": "x" * 400} for _ in range(80)]}
        sonuc = butceye_sigdir(dict(buyuk))
        self.assertTrue(sonuc["kirpildi"])
        self.assertLess(len(sonuc["liste"]), 80)
        import json

        self.assertLessEqual(len(json.dumps(sonuc, ensure_ascii=False)), AZAMI_KARAKTER)

    def test_ust_duzey_sayilar_korunur(self):
        """Listeyi kaybetmek kabul; 'kaç sonuç var' bilgisini kaybetmek değil."""
        sonuc = butceye_sigdir(
            {"ok": True, "toplam": 900, "liste": [{"ad": "y" * 900} for _ in range(40)]}
        )
        self.assertEqual(sonuc["toplam"], 900)


class ModelYetenekTests(TestCase):
    """
    ⚠️ Adaptive thinking ve `output_config.effort` yalnızca 4.6+ modellerde var.
    Eski modele gönderilirse API **400** döner ve hata "geçici sorun" gibi görünür.
    Bu test, model kademesi değiştirilirken (ucuzlatma denemesi) sessizce kırılmayı önler.
    """

    def test_modern_modeller_ek_parametre_alir(self):
        from assistant.services.agent import _modelin_yetenekleri

        for model in ("claude-opus-5", "claude-sonnet-5", "claude-sonnet-4-6"):
            with self.subTest(model=model):
                ek = _modelin_yetenekleri(model)
                self.assertEqual(ek["thinking"], {"type": "adaptive"})
                self.assertIn("effort", ek["output_config"])

    def test_eski_modeller_ek_parametre_almaz(self):
        from assistant.services.agent import _modelin_yetenekleri

        for model in ("claude-haiku-4-5", "claude-sonnet-4-20250514"):
            with self.subTest(model=model):
                self.assertEqual(_modelin_yetenekleri(model), {})


class YilSerisiTests(TestCase):
    """`authority_profile` yıl serisini AZALAN üretir; dilimleme yönü kritik."""

    def test_en_yeni_yillar_kronolojik_doner(self):
        from assistant.tools.read import son_yillar

        azalan = [{"yil": y} for y in (2026, 2025, 2024, 2023, 2022, 2021, 2020, 2016)]
        self.assertEqual(
            [r["yil"] for r in son_yillar(azalan)],
            [2022, 2023, 2024, 2025, 2026],  # en yeni 5, eskiden yeniye
        )

    def test_kisa_seri_ve_bos_seri(self):
        from assistant.tools.read import son_yillar

        self.assertEqual([r["yil"] for r in son_yillar([{"yil": 2026}, {"yil": 2025}])],
                         [2025, 2026])
        self.assertEqual(son_yillar([]), [])
        self.assertEqual(son_yillar(None), [])
