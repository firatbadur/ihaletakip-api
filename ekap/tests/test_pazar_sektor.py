"""
Pazar panosu — **sektör ekseni** + OKAS ekseninin geriye dönük uyumluluğu.

Değişimin gerekçesi üretim ölçümüdür (2026-09-24): OKAS ekseninde `okas_bucket`
doluluğu 2026'da **%78,7**, sektör **%98,3** → panonun **4. en büyük kalemi
"Sınıflandırılmamış"**tı (18.635 sözleşme) ve listenin başı *"BI. ve BII. Grubu
işlerin dışındaki bina işleri"* gibi bürokratik OKAS etiketleriydi. Onboarding
ekranı zaten sektör taksonomisini kullanıyor.

⚠️ **Mobil DEĞİŞMEDEN çalışmalı.** İstemci (`src/api/v1/market.js`) `okas_bucket`
alanını **opak anahtar** olarak okuyup `/ekap/market/{anahtar}/` yoluna koyuyor;
alan adı bu yüzden korundu. Ayrıca `MarketBucket` ekranındaki "bu gruptaki
ihaleleri gör" düğmesi anahtarı `okas_kod` filtresi olarak gönderiyor → sektör
kodu gelince arama **sessizce boş** dönmemeli.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from ekap import market as market_mod
from ekap.models import Contract, Contractor, MarketStat, Tender


class PazarSektorTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        firma = Contractor.objects.create(kanonik_ad="TEST A.Ş.", kanonik_anahtar="test as")
        t = Tender.objects.create(ikn="2025/1", ekap_id="e1", ihale_adi="Yemek")
        tarih = timezone.now()
        for i, (sektor, bucket, bedel) in enumerate([
            ("gida_catering", "5532", "1000.00"),
            ("gida_catering", "5532", "2000.00"),
            ("insaat_yapim", "4521", "9000.00"),
            ("", "", "500.00"),          # ⚠️ sınıflandırılmamış: GERÇEK veri
        ]):
            Contract.objects.create(
                tender=t, ekap_sozlesme_id=f"s{i}", yuklenici=firma,
                yuklenici_adi="TEST A.Ş.", sozlesme_tarihi=tarih,
                sozlesme_bedeli_num=Decimal(bedel), sektor=sektor, okas_bucket=bucket,
                il_id=34,
            )
        market_mod.refresh_market_stats()
        cls.yil = tarih.year

    def test_iki_eksen_de_yazilir(self):
        boyutlar = set(MarketStat.objects.values_list("boyut", flat=True))
        self.assertEqual(boyutlar, {"sektor", "okas"})

    def test_varsayilan_eksen_sektordur(self):
        veri, hata = market_mod.genel_bakis(self.yil)
        self.assertIsNone(hata)
        self.assertEqual(veri["boyut"], "sektor")
        kodlar = [g["okas_bucket"] for g in veri["is_gruplari"]]
        self.assertIn("gida_catering", kodlar)
        self.assertNotIn("5532", kodlar)

    def test_sektor_adi_insan_okunur(self):
        veri, _ = market_mod.genel_bakis(self.yil)
        adlar = {g["okas_bucket"]: g["ad"] for g in veri["is_gruplari"]}
        self.assertEqual(adlar["gida_catering"], "Gıda ve Yemek Hizmeti")
        self.assertEqual(adlar[""], market_mod.SINIFLANDIRILMAMIS)

    def test_siniflandirilmamis_DUSURULMEZ(self):
        """⚠️ Boş sektör gerçek veridir; sessizce düşürmek toplamları bozar."""
        veri, _ = market_mod.genel_bakis(self.yil)
        self.assertIn("", [g["okas_bucket"] for g in veri["is_gruplari"]])

    def test_okas_ekseni_istenerek_alinir(self):
        veri, _ = market_mod.genel_bakis(self.yil, boyut="okas")
        self.assertEqual(veri["boyut"], "okas")
        self.assertIn("5532", [g["okas_bucket"] for g in veri["is_gruplari"]])

    def test_tanimsiz_boyut_varsayilana_duser(self):
        veri, _ = market_mod.genel_bakis(self.yil, boyut="zamazingo")
        self.assertEqual(veri["boyut"], "sektor")

    # ── Drill-down ────────────────────────────────────────────────────────────
    def test_sektor_detayi(self):
        veri, hata = market_mod.grup_detayi("gida_catering")
        self.assertIsNone(hata)
        self.assertEqual(veri["boyut"], "sektor")
        self.assertEqual(veri["ad"], "Gıda ve Yemek Hizmeti")
        self.assertEqual(veri["yillara_gore"][-1]["sozlesme_sayisi"], 2)

    def test_ESKI_OKAS_DERIN_BAGLANTISI_CALISIR(self):
        """⚠️ Mobil önbelleğinde ve paylaşılmış linklerde 4 haneli kodlar var;
        eksen koddan çözülür, istemcinin bilmesi gerekmez."""
        veri, hata = market_mod.grup_detayi("5532")
        self.assertIsNone(hata)
        self.assertEqual(veri["boyut"], "okas")
        self.assertEqual(veri["yillara_gore"][-1]["sozlesme_sayisi"], 2)

    def test_detay_kirilimlari_dogru_eksenden_gelir(self):
        veri, _ = market_mod.grup_detayi("insaat_yapim")
        self.assertEqual([i["il_id"] for i in veri["iller"]], [34])
        self.assertEqual(veri["iller"][0]["sozlesme_sayisi"], 1)
        self.assertEqual(veri["yogunlasma"]["firma_sayisi"], 1)

    def test_bilinmeyen_kod_404(self):
        veri, hata = market_mod.grup_detayi("boyle_bir_sektor_yok")
        self.assertIsNone(veri)
        self.assertIsNotNone(hata)


class OkasKodSektorYonlendirmeTest(TestCase):
    """Mobilin 'bu gruptaki ihaleleri gör' düğmesi sektör kodu gönderiyor."""

    @classmethod
    def setUpTestData(cls):
        from ekap.models import OkasItem
        cls.gida = Tender.objects.create(ikn="2025/10", ekap_id="g1",
                                         ihale_adi="Yemek", sektor="gida_catering")
        cls.insaat = Tender.objects.create(ikn="2025/11", ekap_id="g2",
                                           ihale_adi="Bina", sektor="insaat_yapim")
        OkasItem.objects.create(tender=cls.insaat, kodu="45210000")

    def _ara(self, **params):
        from ekap.views import apply_tender_filters
        return set(apply_tender_filters(Tender.objects.all(), params)
                   .values_list("ekap_id", flat=True))

    def test_sektor_kodu_okas_kod_olarak_gelirse_sektore_yonlenir(self):
        self.assertEqual(self._ara(okas_kod="gida_catering"), {"g1"})

    def test_sayisal_okas_kodu_hala_okas_kalemine_bakar(self):
        self.assertEqual(self._ara(okas_kod="4521"), {"g2"})

    def test_karisik_liste_iki_dali_da_calistirir(self):
        self.assertEqual(self._ara(okas_kod="gida_catering,4521"), {"g1", "g2"})

    def test_taninmayan_harfli_deger_bos_doner_PATLAMAZ(self):
        self.assertEqual(self._ara(okas_kod="boyle_sektor_yok"), set())
