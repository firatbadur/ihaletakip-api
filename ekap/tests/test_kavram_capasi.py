"""
Kavram çapası — benzer iş seçiminin tutarlılığı.

Test edilen arıza (üretimde ölçüldü 2026-09-23): bir ihalenin kendi keyword'leri,
ait olduğu işin **güvenilmez bir örneğidir**. Adında "sürekli atıksu izleme" geçen
86 ihalede (birebir aynı iş) 40 farklı keyword vardı ve hiçbiri ailenin yarısını
kapsamıyordu → hangi ihaleden bakıldığına göre farklı "benzer işler" kümesi, farklı
fiyat analizi. İndirim medyanı aynı iş için **%2,8 ile %35,6** arasında değişiyordu.

Çapa, komşulukta en çok ZENGİNLEŞEN (lift) kavramı bulup benzerliği ona kurar.

⚠️ Buradaki kurgu gerçek arızanın küçültülmüş hâlidir: hedef ihale ailenin
**çoğunluk terimini taşımaz**, elinde yalnızca jenerik `atiksu` ve kenarda kalan
`izleme sistemi` vardır. Eşik tabanlı eski yol bu ihaleyi aileye bağlayamaz.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings

from ekap import keywords as kw
from ekap.models import Keyword, Tender, TenderKeyword


def _ihale(no, ad):
    return Tender.objects.create(ikn=f"2025/{no}", ekap_id=f"e{no}", ihale_adi=ad)


class KavramCapasiTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Aile: aynı iş, AI farklı terimler vermiş.
        cls.aile = []
        for i in range(12):
            cls.aile.append(_ihale(100 + i, f"Sürekli Atıksu İzleme Sistemi {i}"))
        # Gürültü: `atiksu` paylaşan ama alakasız işler.
        cls.gurultu = [_ihale(300 + i, f"Atıksu Arıtma Tesisi İşletme {i}")
                       for i in range(20)]
        # ⚠️ Arka plan ŞART: çapayı gürültüden ayıran şey N değil **df ORANIDIR**
        # (üretimde `atiksu` df=1.074 ↔ `atiksu izleme sistemi` df=28 → 38 kat).
        # Küçük bir kurguda jenerik terimin df'i yeterince büyümez, göreli lift
        # eşiği onu eleyemez ve test gerçeği yansıtmaz.
        cls.arkaplan = Tender.objects.bulk_create(
            [Tender(ikn=f"2019/{i}", ekap_id=f"b{i}", ihale_adi=f"Atıksu işi {i}")
             for i in range(300)])
        cls.hedef = _ihale(999, "Sürekli Atıksu İzleme Sistemi (SAİS) Kabini")

        def kelime(metin, df):
            return Keyword.objects.create(
                metin=metin, metin_ham=metin, derece=len(metin.split()),
                kullanim_sayisi=df, pasif=False)

        # Aile terimleri: nadir ve ayırt edici.
        cls.k_izleme = kelime("atiksu izleme", 7)
        cls.k_sistem = kelime("atiksu izleme sistemi", 5)
        cls.k_izl_sis = kelime("izleme sistemi", 6)
        # Jenerik: aileyi de gürültüyü de arka planı da kapsıyor.
        cls.k_atiksu = kelime("atiksu", 333)
        cls.k_aritma = kelime("atiksu aritma", 20)

        def bagla(tender, *kelimeler):
            TenderKeyword.objects.bulk_create(
                [TenderKeyword(tender=tender, keyword=k) for k in kelimeler])

        for i, t in enumerate(cls.aile):
            # Ailenin terimi tutarsız: yarısı "atiksu izleme", yarısı "… sistemi".
            bagla(t, cls.k_izleme if i % 2 else cls.k_sistem, cls.k_atiksu)
        for t in cls.gurultu:
            bagla(t, cls.k_aritma, cls.k_atiksu)
        TenderKeyword.objects.bulk_create(
            [TenderKeyword(tender=t, keyword=cls.k_atiksu) for t in cls.arkaplan])
        # ⚠️ Hedef ihale ailenin çoğunluk terimini TAŞIMIYOR.
        bagla(cls.hedef, cls.k_izl_sis, cls.k_atiksu)

    def setUp(self):
        cache.clear()

    def _gruplar(self):
        return kw.kavram_gruplari(self.hedef.pk)

    @override_settings(KEYWORD_CAPA_ENABLED=True, KEYWORD_CAPA_MIN_TOHUM=3, KEYWORD_CAPA_TOHUM=8,
                       KEYWORD_CAPA_MIN_KAPSAM=0.20, KEYWORD_CAPA_LIFT_ORAN=0.10)
    def test_capa_ailenin_terimini_bulur(self):
        """Çapa, jenerik `atiksu`yu değil ailenin ayırt edici terimini seçmeli."""
        bulunan, agirlik = kw._aday_taramasi(self.hedef.pk, self._gruplar())
        capalar = kw.capa_kavramlari(self.hedef.pk, bulunan, agirlik)
        adlar = {ad for _, ad, _ in capalar}
        self.assertTrue(adlar, "çapa hiç bulunamadı")
        self.assertTrue(
            adlar & {"atiksu izleme", "atiksu izleme sistemi"},
            f"ailenin terimi çapa seçilmedi: {adlar}")
        # ⚠️ Jenerik terim çapa OLMAMALI: gürültüyü de kapsar, lift'i düşüktür.
        self.assertNotIn("atiksu", adlar)

    @override_settings(KEYWORD_CAPA_ENABLED=True, KEYWORD_CAPA_MIN_TOHUM=3, KEYWORD_CAPA_TOHUM=8,
                       KEYWORD_CAPA_MIN_KAPSAM=0.20, KEYWORD_CAPA_LIFT_ORAN=0.10)
    def test_capa_aileyi_getirir_gurultuyu_getirmez(self):
        idler, capalar = kw.capali_benzer_idler(self.hedef.pk, self._gruplar(), 2000)
        self.assertTrue(capalar, "çapa yolu devreye girmedi")
        bulunan = set(idler)
        aile_pk = {t.pk for t in self.aile}
        self.assertEqual(len(bulunan & aile_pk), len(aile_pk),
                         "aile üyelerinin tamamı bulunamadı")
        self.assertFalse(bulunan & {t.pk for t in self.gurultu},
                         "arıtma tesisi işleri benzer sayıldı")

    @override_settings(KEYWORD_CAPA_ENABLED=False)
    def test_capa_kapaliyken_eski_yol_aileyi_KACIRIR(self):
        """
        ⚠️ **Testin dişi burada.** Geri alma düğmesi çalışır ama eski yol ailenin
        yalnızca kendi terimini paylaşan yarısını bulur: hedefin keyword'ü
        `izleme sistemi` olduğu için `atiksu izleme` diyen üyeler kümeye giremez.
        Üretimdeki tutarsızlığın ta kendisi — çapa açıkken (yukarıdaki test)
        ailenin TAMAMI geliyor.
        """
        idler, capalar = kw.capali_benzer_idler(self.hedef.pk, self._gruplar(), 2000)
        self.assertEqual(capalar, [])
        aile_pk = {t.pk for t in self.aile}
        eski_kapsam = len(set(idler) & aile_pk)
        self.assertLess(eski_kapsam, len(aile_pk),
                        "eski yol beklenmedik şekilde ailenin tamamını buldu")

    def test_uretken_olmayan_kavram_esigi_belirlemez(self):
        """
        ⚠️ df=1 bir keyword hiçbir aday üretemez ama en yüksek IDF'e sahiptir;
        eşiği o belirlerse kademe 0 aday döndürüp sessizce ölür (İKN 2021/432357).
        """
        tek = Keyword.objects.create(metin="atiksu aritim revizyonu",
                                     metin_ham="atiksu aritim revizyonu",
                                     derece=3, kullanim_sayisi=1, pasif=False)
        TenderKeyword.objects.create(tender=self.hedef, keyword=tek)
        cache.clear()
        gruplar = kw.kavram_gruplari(self.hedef.pk)
        idler = kw.benzer_ihale_idleri(self.hedef.pk, gruplar, 2000)
        self.assertTrue(idler, "df=1 kavram yüzünden kademe boş döndü")
