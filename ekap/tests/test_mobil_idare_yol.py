"""
Ata-yolu ile idare eşleştirmesi — `ekap.mobil.idare._yol_kademesi`.

⚠️ **Neden gerekli:** mobil API `idare_id` vermiyor VE idare adını **ata yolunu
birleştirerek** tek string olarak döndürüyor. Birebir ad eşleşmesi bu yüzden
ihalelerin çoğunda yapısal olarak tutmuyordu — üretimde son 10 günün mobil
kaynaklı ihalelerinin yalnızca %30,5'inde `idare_id` doluydu; kullanıcı bunu
"idareye tıklanmıyor, o idareyi seçince ihale çıkmıyor" olarak bildirdi
(İKN 2026/1789437).

Fixture, o ihalenin **gerçek** DETSIS zinciridir (üretimden alındı) ve ara düğüm
atlama davranışını birebir yeniden üretir.
"""
from django.core.cache import cache
from django.test import TestCase

from ekap.mobil import idare
from ekap.models import Authority
from ekap.utils import normalize_tr

# (detsis_no, parent_detsis, ad, idare_id) — üretim verisi
AGAC = [
    ("11513791", "0",        "TEKİRDAĞ BÜYÜKŞEHİR BELEDİYE BAŞKANLIĞI", ""),
    ("31616290", "11513791", "TEKİRDAĞ SU VE KANALİZASYON İDARESİ GENEL MÜDÜRLÜĞÜ", ""),
    # ⚠️ Bu ara düğüm mobil adında GÖRÜNMEZ — eşleştirme onu atlayabilmeli.
    ("13575828", "31616290", "GENEL MÜDÜR YARDIMCILIĞI 1", ""),
    ("63229239", "13575828", "TİCARET İŞLERİ DAİRESİ BAŞKANLIĞI", "91644"),
    ("18148693", "63229239", "İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ", "91646"),
    # Başka bir kurumun AYNI adlı şubesi — önek doğrulaması bunu elemeli.
    ("50000000", "0",        "KAYSERİ BÜYÜKŞEHİR BELEDİYE BAŞKANLIĞI", ""),
    ("50000001", "50000000", "İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ", "77777"),
]

MOBIL_AD = ("TEKİRDAĞ SU VE KANALİZASYON İDARESİ GENEL MÜDÜRLÜĞÜ "
            "TİCARET İŞLERİ DAİRESİ BAŞKANLIĞI İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ")


class YolKademesiTest(TestCase):
    def setUp(self):
        for detsis, parent, ad, iid in AGAC:
            Authority.objects.create(detsis_no=detsis, parent_detsis=parent, ad=ad,
                                     ad_norm=normalize_tr(ad), idare_id=iid)
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_birlesik_yol_cozulur(self):
        """Üretim vakası: mobilin birleşik adı doğru alt birime gitmeli."""
        self.assertEqual(idare.coz(MOBIL_AD), ("91646", "yol"))

    def test_ara_dugum_atlanabilir(self):
        """⚠️ Mobil ad "GENEL MÜDÜR YARDIMCILIĞI 1"i hiç içermiyor — katı yol
        eşitliği bu vakayı kaçırırdı."""
        self.assertNotIn("yardimcilig", normalize_tr(MOBIL_AD))
        self.assertEqual(idare.coz(MOBIL_AD)[0], "91646")

    def test_ayni_adli_sube_onek_ile_ayrilir(self):
        """Aynı ad iki kurumda var; ayırt eden şey ÖNEKTEKİ ata adlarıdır."""
        self.assertEqual(
            idare.coz("KAYSERİ BÜYÜKŞEHİR BELEDİYE BAŞKANLIĞI İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ"),
            ("77777", "yol"),
        )

    def test_onek_ata_degilse_YAZILMAZ(self):
        """⚠️ Uydurma bir üst kurum adı eşleşmeyi geçememeli."""
        self.assertEqual(
            idare.coz("ANKARA BÜYÜKŞEHİR BELEDİYE BAŞKANLIĞI İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ"),
            ("", ""),
        )

    def test_ata_sirasi_bozuksa_YAZILMAZ(self):
        """⚠️ Ata atlamaya izin var, SIRA bozmaya yok — yoksa kurum karışır."""
        self.assertEqual(
            idare.coz("TİCARET İŞLERİ DAİRESİ BAŞKANLIĞI "
                      "TEKİRDAĞ SU VE KANALİZASYON İDARESİ GENEL MÜDÜRLÜĞÜ "
                      "İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ"),
            ("", ""),
        )

    def test_oneksiz_belirsiz_ad_BOS_birakilir(self):
        """Ata bilgisi yoksa iki kurum arasında seçim yazı turadır."""
        self.assertEqual(idare.coz("İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ"), ("", ""))

    def test_en_uzun_sonek_tercih_edilir(self):
        """⚠️ Kısa sonek önce denenseydi ara düğüm (91644) kazanırdı."""
        self.assertEqual(idare.coz(MOBIL_AD)[0], "91646")

    def test_ara_dugum_kendi_adiyla_cozulur(self):
        self.assertEqual(
            idare.coz("TEKİRDAĞ SU VE KANALİZASYON İDARESİ GENEL MÜDÜRLÜĞÜ "
                      "TİCARET İŞLERİ DAİRESİ BAŞKANLIĞI"),
            ("91644", "yol"),
        )

    def test_tam_ad_kademesi_onceliklidir(self):
        """Regresyon: tek düğümlü idareler eskisi gibi `tam` ile çözülmeli."""
        Authority.objects.create(detsis_no="9", parent_detsis="0", ad="ÜNYE BELEDİYE BAŞKANLIĞI",
                                 ad_norm=normalize_tr("ÜNYE BELEDİYE BAŞKANLIĞI"), idare_id="24192")
        self.assertEqual(idare.coz("Ünye Belediye Başkanlığı"), ("24192", "tam"))
