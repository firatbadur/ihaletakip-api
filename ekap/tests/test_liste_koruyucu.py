"""
`upsert_tender_from_list(koruyucu=True)` — kısmi liste kaynağı veri silmemeli.

⚠️⚠️ Üretim arızası (2026-09-22): mobil liste yanıtı **altı alan** doldurup gerisini
hiç vermiyor (`mobil/adapt.liste_satirindan`), ama liste upsert'i o alanları koşulsuz
yazıyordu → **her keşif turu** detay senkronunun yazdığı `ihale_durum`u ve açıklama
metinlerini siliyordu. Ölçüm: mobil kaynaklı 1.991 satırın 1.990'ında `ihale_durum`
NULL. Ürün etkisi: mobil uygulamanın "Katılıma Açık" filtresi (`ihale_durum=2`) bu
ihaleleri **hiç göstermiyordu** ve `DURUM_SONUCLANMIS` üstüne kurulu tazeleme/alarm
mantığı kördü.

⚠️ `_LISTE_EZMEZ` bunu yakalamıyordu çünkü yalnızca `None` değerleri süzüyor; mobilin
hiç vermediği alanlar `defaults`'a `""`, `0` ve `False` olarak giriyordu — yani
"boş değerin tipi" arızayı gizliyordu. Bu testler o gizlenmeyi kapatır.
"""

from django.test import TestCase

from ekap.models import Tender
from ekap.mobil import adapt
from ekap.sync import _LISTE_KAYNAK_ANAHTARI, upsert_tender_from_list


def _dolu_tender(**fazla):
    """Detay senkronunun doldurduğu hâli taklit eder."""
    alanlar = dict(
        ikn="2026/1", ekap_id="mobil:2026-1", ihale_adi="İş",
        ihale_durum=2, ihale_durum_aciklama="İhale İlanı Yayımlanmış, Katılıma Açık",
        ihale_usul=1, ihale_usul_aciklama="İhale Usulü: Açık",
        ihale_tip=1, ihale_tipi_aciklama="Mal",
        dokuman_sayisi=3, ilan_var_mi=True,
    )
    alanlar.update(fazla)
    return Tender.objects.create(**alanlar)


class KoruyucuListeUpsertTest(TestCase):
    """Mobil liste satırı (altı alan) mevcut veriyi ezmemeli."""

    def setUp(self):
        self.tender = _dolu_tender()
        # `liste_satirindan`'ın gerçek çıktısı — kaynak sözleşmesi buradan gelir.
        self.satir = adapt.liste_satirindan(
            {
                "ikn": "2026/1",
                "ihaleAdi": "İş (güncel)",
                "idareAdi": "İdare",
                "idareIlAdi": "ANKARA",
                "ihaleTarihi": "01.10.2026 10:00",
                "ihaleTipi": 1,
            },
            "mobil:2026-1",
        )

    def test_durum_silinmez(self):
        """⚠️ Asıl arıza: her keşif turu durumu NULL'lıyordu."""
        upsert_tender_from_list(self.satir, koruyucu=True)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.ihale_durum, 2)
        self.assertEqual(
            self.tender.ihale_durum_aciklama,
            "İhale İlanı Yayımlanmış, Katılıma Açık",
        )

    def test_bos_string_ve_sifir_alanlar_da_korunur(self):
        """
        ⚠️ `_LISTE_EZMEZ` yalnızca `None`'ı süzüyordu; arızayı gizleyen tam olarak
        `""`/`0`/`False` üreten alanlardı.
        """
        upsert_tender_from_list(self.satir, koruyucu=True)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.ihale_usul_aciklama, "İhale Usulü: Açık")
        self.assertEqual(self.tender.ihale_tipi_aciklama, "Mal")
        self.assertEqual(self.tender.dokuman_sayisi, 3)
        self.assertTrue(self.tender.ilan_var_mi)

    def test_verilen_alanlar_yine_guncellenir(self):
        """Koruma, gerçekten gelen veriyi engellememeli."""
        upsert_tender_from_list(self.satir, koruyucu=True)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.ihale_adi, "İş (güncel)")
        self.assertEqual(self.tender.ihale_il_adi, "ANKARA")
        self.assertEqual(self.tender.ihale_tip, 1)

    def test_koruyucu_kapaliyken_v2_davranisi_korunur(self):
        """
        ⚠️ v2 listesi bu alanları gerçekten dolduruyor → orada boş değer "gerçekten
        boş" demektir ve yazılmalıdır. Koruma v2 yolunu değiştirmemeli.
        """
        upsert_tender_from_list(self.satir, koruyucu=False)
        self.tender.refresh_from_db()
        self.assertIsNone(self.tender.ihale_durum)
        self.assertEqual(self.tender.ihale_durum_aciklama, "")


class KaynakHaritasiTest(TestCase):
    """Harita, liste upsert'inin yazdığı alanlarla uyumlu kalmalı."""

    def test_mobil_listenin_vermedigi_alanlar_haritada(self):
        """
        ⚠️ Yeni bir alan `defaults`'a eklenip haritaya eklenmezse arıza **sessizce**
        geri döner: alan koruyucu modda yine ezilir.
        """
        mobil_anahtarlar = set(
            adapt.liste_satirindan({"ikn": "2026/2"}, "mobil:2026-2")
        )
        for alan in ("ihale_durum", "ihale_durum_aciklama", "ihale_usul_aciklama",
                     "ihale_tipi_aciklama", "dokuman_sayisi", "ilan_var_mi"):
            with self.subTest(alan=alan):
                self.assertIn(alan, _LISTE_KAYNAK_ANAHTARI)
                kaynaklar = _LISTE_KAYNAK_ANAHTARI[alan]
                self.assertFalse(
                    mobil_anahtarlar & set(kaynaklar),
                    f"{alan} mobil listede var sanılıyor — harita yanlış",
                )


class IlanVarMiTuretmeTest(TestCase):
    """Adapter `ilanVarMi`yi ilan HTML'inden türetir (yalnızca pozitif yön)."""

    def test_ilan_html_varsa_bayrak_konur(self):
        govde = adapt.detaydan("2026/3", {
            "ihaleAdi": "İş", "idareAdi": "İdare",
            "ilanHtml": "<p>İhale ilanı</p>",
            "ilanSekli": "İhale İlanı",
        })
        self.assertTrue(govde["item"].get("ilanVarMi"))

    def test_ilan_yoksa_anahtar_konmaz(self):
        """⚠️ `False` yazmak, v2'den gelmiş `True`'yu silmek olurdu."""
        govde = adapt.detaydan("2026/4", {"ihaleAdi": "İş", "idareAdi": "İdare"})
        self.assertNotIn("ilanVarMi", govde["item"])


class OnYeterlikDurumuTest(TestCase):
    """
    "Ön yeterlik henüz yapılmamış" → **Katılıma Açık (2)**.

    ⚠️ Ürün kararı (2026-09-22): "Belli İstekliler Arasında" usulünde ihale iki
    aşamalıdır ve "henüz yapılmamış" demek başvuruların hâlâ alındığı anlamına gelir
    → firma açısından ihale katılıma açıktır. EKAP'ın bu aşama için ayrı bir sayısal
    kodu arşivde hiç görülmedi, bu yüzden kod boş bırakılıyordu ve 13 açık ihale
    "Katılıma Açık" filtresinin dışında kalıyordu.
    """

    def test_on_yeterlik_katilima_acik(self):
        for metin in ("Ön yeterlik henüz yapılmamış",
                      "ÖN YETERLİK HENÜZ YAPILMAMIŞ",
                      "Ön Yeterlik Henüz Yapılmamıştır"):
            with self.subTest(metin=metin):
                self.assertEqual(adapt.durum_kodu(metin), 2)

    def test_on_yeterlik_tamamlanmis_acik_SAYILMAZ(self):
        """
        ⚠️ Eşleme YALNIZCA "henüz" hâli içindir: ön yeterlik **değerlendirmesi
        tamamlanmış** ihale artık katılıma açık değildir (davet aşaması). Geniş bir
        ("on yeterlik", 2) kuralı bunu da açık gösterip filtreyi sessizce yanlışlardı
        — bu test o genişlemeyi engeller.
        """
        self.assertIsNone(adapt.durum_kodu("Ön yeterlik değerlendirmesi tamamlanmış"))
        self.assertIsNone(adapt.durum_kodu("Ön yeterlik sonuçlandı"))
