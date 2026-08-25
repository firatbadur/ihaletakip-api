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
        # ⚠️ Tam liste DEĞİL, ÖNEK sabitlenir: yeni araç eklemek listeyi bir kez
        # uzatır (cache bir kez yenilenir, kaçınılmaz). Asıl hata, mevcut bir aracın
        # yerini değiştirmek ya da araya sokmaktır — o her istekte cache'i böler.
        onek = ["ihale_ara", "ihale_detay", "okas_ara", "idare_profili",
                "firma_ara", "firma_profili", "firma_isleri", "kullanicinin_verisi",
                "idare_ara", "ihale_kaydet_oner", "alarm_kur_oner", "filtre_kaydet_oner",
                "ihale_benchmark", "tekrar_eden_ihaleler", "pazar_panosu",
                "dokuman_analizi", "toplu_ihale_kaydet_oner", "ihale_tasi_oner",
                "firma_takip_oner", "idare_favori_oner", "kayit_sil_oner"]
        adlar = [t["name"] for t in TOOL_SPECS]
        self.assertEqual(adlar[: len(onek)], onek, "Mevcut araçların sırası değişmiş")


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


class EylemOnerisiTests(TestCase):
    """
    ⚠️ Eylem araçları YAZMAZ. Bu sınıf o sınırı korur: bir gün biri "kullanıcı zaten
    istedi, doğrudan kaydedelim" diye kısayol yaparsa testler kırmızıya döner.
    """

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.user = get_user_model().objects.create(
            email="eylem@test.local", username="eylem-test"
        )
        cls.tender = Tender.objects.create(
            ekap_id="test-ekap-2", ikn="2026/999002", ihale_adi="Test Alarm İşi",
            ihale_adi_norm="test alarm isi", idare_adi="TEST İDARE", il_id=251,
        )

    def _ctx_kartli(self):
        ctx = _ctx(user=self.user)
        ctx.kart_ekle(self.tender)
        return ctx

    def test_havuz_disindaki_ikn_reddedilir(self):
        """Model uydurduğu bir İKN'yi kullanıcının kayıtlarına yazdıramamalı."""
        sonuc = TOOL_IMPL["ihale_kaydet_oner"](_ctx(user=self.user), ikn="2099/123456")
        self.assertFalse(sonuc["ok"])

    def test_oneri_uretilir_ama_kayit_YAPILMAZ(self):
        from assistant.models import AssistantAction
        from tenders.models import SavedTender

        ctx = self._ctx_kartli()
        sonuc = TOOL_IMPL["ihale_kaydet_oner"](ctx, ikn="2026/999002")
        self.assertTrue(sonuc["ok"])
        self.assertEqual(sonuc["durum"], "bekliyor")
        # Öneri var…
        eylem = AssistantAction.objects.get(id=sonuc["action_id"])
        self.assertEqual(eylem.tur, "ihale_kaydet")
        self.assertEqual(eylem.params["tender_ikn"], "2026/999002")
        self.assertEqual(len(ctx.oneriler), 1)
        # …ama kullanıcının verisine HİÇBİR ŞEY yazılmadı.
        self.assertEqual(SavedTender.objects.filter(user=self.user).count(), 0)

    def test_alarm_onerisi_de_yazmaz_ve_premium_ISTEMEZ(self):
        """Pro kontrolü onay anında (HTTP'de) yapılır; öneri üretmek serbesttir."""
        from tenders.models import TenderAlarm

        ctx = self._ctx_kartli()
        sonuc = TOOL_IMPL["alarm_kur_oner"](ctx, ikn="2026/999002")
        self.assertTrue(sonuc["ok"])
        self.assertEqual(TenderAlarm.objects.filter(user=self.user).count(), 0)

    def test_filtre_onerisinde_gecersiz_alan_reddedilir(self):
        """
        `SavedFilter.filters` doğrudan `apply_tender_filters`'a besleniyor; geçersiz
        anahtar sessizce yok sayılsa kullanıcı kurduğunu sandığından FARKLI bir filtre
        kaydeder ve alarmı yanlış çalışır.
        """
        ctx = _ctx(user=self.user)
        self.assertFalse(
            TOOL_IMPL["filtre_kaydet_oner"](ctx, ad="Test", filtreler={"il": "Ankara"})["ok"]
        )
        self.assertTrue(
            TOOL_IMPL["filtre_kaydet_oner"](ctx, ad="Test", filtreler={"il_id": [251]})["ok"]
        )

    def test_bos_filtre_reddedilir(self):
        ctx = _ctx(user=self.user)
        self.assertFalse(TOOL_IMPL["filtre_kaydet_oner"](ctx, ad="X", filtreler={})["ok"])


class Faz3YazmaAraciTests(TestCase):
    """
    Faz 3 eylem araçları öneri ÜRETMEDEN ÖNCE hedefi doğrular.

    Doğrulama olmasaydı model uydurduğu bir kimlik için onay kartı çıkarır, kullanıcı
    "Evet"e basar ve hata ancak yazma anında görünürdü — kullanıcı gözünde asistan
    var olmayan bir şeyi vaat etmiş olur.
    """

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        from ekap.models import Authority

        U = get_user_model()
        cls.user = U.objects.create(email="w3@test.local", username="w3-test")
        cls.tender = Tender.objects.create(
            ekap_id="test-ekap-3", ikn="2026/999010", ihale_adi="Test Toplu İş",
            ihale_adi_norm="test toplu is", idare_adi="TEST İDARE", il_id=251,
        )
        cls.firma = Contractor.objects.create(kanonik_ad="TEST FİRMA", arama_norm="test firma")
        Authority.objects.create(detsis_no="99887766", ad="TEST BELEDİYESİ")

    def _ctx_kartli(self):
        ctx = _ctx(user=self.user)
        ctx.kart_ekle(self.tender)
        return ctx

    def test_toplu_kaydet_havuz_disi_ikn_reddedilir(self):
        from assistant.tools import write

        sonuc = write.toplu_ihale_kaydet_oner(
            self._ctx_kartli(), iknler=["2026/999010", "2026/UYDURMA"]
        )
        self.assertFalse(sonuc["ok"])
        self.assertIn("2026/UYDURMA", sonuc["hata"])

    def test_toplu_kaydet_tek_oneri_uretir(self):
        from assistant.models import AssistantAction
        from assistant.tools import write

        ctx = self._ctx_kartli()
        sonuc = write.toplu_ihale_kaydet_oner(ctx, iknler=["2026/999010"], klasor="Test")
        self.assertTrue(sonuc["ok"])
        # Kart başına değil, TOPLAM bir öneri: 12 ihale = 12 kart sohbeti kullanılamaz kılar.
        self.assertEqual(len(ctx.oneriler), 1)
        self.assertEqual(AssistantAction.objects.count(), 1)

    def test_firma_takip_uydurma_id_reddedilir(self):
        from assistant.tools import write

        self.assertFalse(write.firma_takip_oner(_ctx(user=self.user), firma_id=99999999)["ok"])
        self.assertTrue(write.firma_takip_oner(_ctx(user=self.user), firma_id=self.firma.pk)["ok"])

    def test_idare_favori_uydurma_detsis_reddedilir(self):
        from assistant.tools import write

        self.assertFalse(write.idare_favori_oner(_ctx(user=self.user), detsis_no="00000")["ok"])
        self.assertTrue(write.idare_favori_oner(_ctx(user=self.user), detsis_no="99887766")["ok"])

    def test_silme_onerisi_olmayan_kayit_icin_cikmaz(self):
        from assistant.tools import write

        sonuc = write.kayit_sil_oner(_ctx(user=self.user), tur="ihale", anahtar="2026/999010")
        self.assertFalse(sonuc["ok"])

    def test_silme_onerisi_kayitli_ihale_icin_cikar(self):
        from assistant.tools import write
        from tenders.models import SavedTender

        SavedTender.objects.create(user=self.user, tender_ikn="2026/999010",
                                   tender_title="Test Toplu İş")
        sonuc = write.kayit_sil_oner(_ctx(user=self.user), tur="ihale", anahtar="2026/999010")
        self.assertTrue(sonuc["ok"])

    def test_alarm_silmede_ikn_ile_de_bulunur(self):
        """Alarm `tender_id` ile tutulur; kullanıcı İKN söyler. İkisi de çalışmalı."""
        from assistant.tools import write
        from tenders.models import TenderAlarm

        TenderAlarm.objects.create(user=self.user, tender_id="test-ekap-3",
                                   tender_ikn="2026/999010", tender_title="Test")
        self.assertTrue(write.kayit_sil_oner(_ctx(user=self.user), tur="alarm",
                                             anahtar="2026/999010")["ok"])


class YaklasanVeOnerilerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from datetime import timedelta

        from django.contrib.auth import get_user_model
        from django.utils import timezone

        U = get_user_model()
        cls.user = U.objects.create(email="y3@test.local", username="y3-test")
        cls.gelecek = Tender.objects.create(
            ekap_id="ek-gelecek", ikn="2026/900001", ihale_adi="Gelecek İş",
            ihale_adi_norm="gelecek is", idare_adi="İDARE", il_id=251,
            ihale_tarihi=timezone.now() + timedelta(days=5),
        )
        cls.gecmis = Tender.objects.create(
            ekap_id="ek-gecmis", ikn="2026/900002", ihale_adi="Geçmiş İş",
            ihale_adi_norm="gecmis is", idare_adi="İDARE", il_id=251,
            ihale_tarihi=timezone.now() - timedelta(days=5),
        )

    def test_yaklasan_yalnizca_gelecek_tarihlileri_verir(self):
        from assistant.tools import read
        from tenders.models import SavedTender

        for t in (self.gelecek, self.gecmis):
            SavedTender.objects.create(user=self.user, tender_ikn=t.ikn)
        sonuc = read.kullanicinin_verisi(_ctx(user=self.user), tur="yaklasan")
        self.assertTrue(sonuc["ok"])
        self.assertEqual([r["ikn"] for r in sonuc["liste"]], ["2026/900001"])
        self.assertEqual(sonuc["liste"][0]["kalan_gun"], 5)

    def test_onerilerim_gerekceyi_aynen_tasir(self):
        from django.utils import timezone

        from assistant.models import TenderRecommendation
        from assistant.tools import read

        TenderRecommendation.objects.create(
            user=self.user, tender=self.gelecek, score=7.0,
            reasons=["Şehir: ANKARA", "Anahtar kelime: asfalt"],
            date=timezone.localdate(),
        )
        sonuc = read.kullanicinin_verisi(_ctx(user=self.user), tur="onerilerim")
        self.assertTrue(sonuc["ok"])
        self.assertEqual(sonuc["liste"][0]["gerekce"], ["Şehir: ANKARA", "Anahtar kelime: asfalt"])


class GrafikBlokTests(TestCase):
    """Grafik blokları DETERMİNİSTİK üretilir — model karışmaz, uydurma yüzeyi yok."""

    def test_yil_grafigi_kronolojik_ve_bicimli(self):
        from assistant.tools.trim import yil_grafigi

        blok = yil_grafigi("Test", [
            {"yil": 2024, "medyan": "1500000", "adet": 12},
            {"yil": 2025, "medyan": "2400000", "adet": 2},
        ], "medyan")
        self.assertEqual(blok["type"], "bar_chart")
        self.assertEqual([d["label"] for d in blok["veri"]], ["2024", "2025"])
        self.assertEqual(blok["veri"][0]["valueText"], "1,5 M ₺")
        # Örneklemi 3'ten küçük yıl soluk çizilir: grafik yanlış kesinlik vermemeli.
        self.assertTrue(blok["veri"][1]["dim"])
        self.assertFalse(blok["veri"][0]["dim"])

    def test_tek_sutunluk_seri_grafik_uretmez(self):
        from assistant.tools.trim import yil_grafigi

        self.assertIsNone(yil_grafigi("Test", [{"yil": 2025, "medyan": "1"}], "medyan"))

    def test_bos_ve_bozuk_degerler_atlanir(self):
        from assistant.tools.trim import yil_grafigi

        blok = yil_grafigi("Test", [
            {"yil": 2023, "medyan": None},
            {"yil": 2024, "medyan": "abc"},
            {"yil": 2025, "medyan": "1000"},
            {"yil": 2026, "medyan": "2000"},
        ], "medyan")
        self.assertEqual([d["label"] for d in blok["veri"]], ["2025", "2026"])


class SoruOnerisiTests(TestCase):
    """Öneriler deterministik: hangi araçlar çalıştıysa ona göre — model karışmaz."""

    def _iz(self, *adlar):
        return [{"arac": a, "ok": True} for a in adlar]

    def test_arac_calismadiysa_oneri_yok(self):
        from assistant.tools.oneri import soru_onerileri

        # Mevzuat sorusunda ("geçici teminat nedir?") öneri çıkarmak gürültüdür.
        self.assertEqual(soru_onerileri(_ctx(), self._iz()), [])
        self.assertEqual(soru_onerileri(_ctx(), None), [])

    def test_liste_donen_aramada_ihaleye_ozel_oneri_cikmaz(self):
        from assistant.tools.oneri import soru_onerileri

        ctx = _ctx()
        ctx.son_grup = ["2026/1", "2026/2", "2026/3"]
        oneriler = soru_onerileri(ctx, self._iz("ihale_ara"))
        # ⚠️ "Bu iş kaça kapanır?" belirsizdir: "bu" hangi iş belli değil, model
        # yanlış ihaleyi seçebilir. Tek odak yokken çıkmamalı.
        self.assertNotIn("Bu iş kaça kapanır, ne kadar kırım yapmalıyım?", oneriler)
        self.assertIn("Bu aramayı kaydet, yenisi çıkınca haber ver", oneriler)

    def test_tek_ihale_odaginda_fiyat_ve_tekrar_onerilir(self):
        from assistant.tools.oneri import soru_onerileri

        ctx = _ctx()
        ctx.son_grup = ["2026/1"]
        oneriler = soru_onerileri(ctx, self._iz("ihale_detay"))
        self.assertIn("Bu iş kaça kapanır, ne kadar kırım yapmalıyım?", oneriler)
        self.assertIn("Bu iş her yıl açılıyor mu?", oneriler)

    def test_zaten_calisan_arac_tekrar_onerilmez(self):
        from assistant.tools.oneri import soru_onerileri

        ctx = _ctx()
        ctx.son_grup = ["2026/1"]
        oneriler = soru_onerileri(ctx, self._iz("ihale_detay", "ihale_benchmark"))
        # Fiyat analizi bu turda zaten yapıldı; tekrar önermek kullanıcıyı döngüye sokar.
        self.assertNotIn("Bu iş kaça kapanır, ne kadar kırım yapmalıyım?", oneriler)
        self.assertIn("Bu alanda pazar nasıl, kimler iş alıyor?", oneriler)

    def test_firma_ve_idare_takip_onerileri(self):
        from assistant.tools.oneri import soru_onerileri

        self.assertIn("Bu firmayı takibe al",
                      soru_onerileri(_ctx(), self._iz("firma_ara", "firma_isleri")))
        self.assertIn("Bu idareyi favorilerime ekle",
                      soru_onerileri(_ctx(), self._iz("idare_ara", "idare_profili")))

    def test_en_cok_uc_oneri(self):
        from assistant.tools.oneri import AZAMI_ONERI, soru_onerileri

        ctx = _ctx()
        ctx.son_grup = ["2026/1"]
        oneriler = soru_onerileri(
            ctx, self._iz("ihale_detay", "idare_profili", "firma_profili", "pazar_panosu"))
        self.assertLessEqual(len(oneriler), AZAMI_ONERI)
        self.assertEqual(len(set(oneriler)), len(oneriler), "öneriler tekrarlanmış")

    def test_basarisiz_arac_oneri_uretmez(self):
        from assistant.tools.oneri import soru_onerileri

        # Araç hata döndüyse üzerine öneri kurmak yanlış: veri gelmedi.
        self.assertEqual(soru_onerileri(_ctx(), [{"arac": "idare_profili", "ok": False}]), [])
