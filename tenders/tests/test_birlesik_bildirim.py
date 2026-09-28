"""
Birleşik bildirim — kullanıcı başına TEK satır + push, birleştirmeyi BACKEND yapar.

Akış: bildirim eşleşen **filtre/idare id'lerini** taşır → mobil bunları
`kayitli_filtreler` / `favori_idareler` olarak uca gönderir → uç OR'layıp tek ihale
listesi döndürür.

⚠️⚠️ Bu dosyanın en kritik testi `test_UC_SAYIMI_govdedeki_sayiyla_AYNI`. Bu konu üretimde
üç kez "bildirimdeki sayı ekrandaki listeyle tutmuyor" arızası üretti:
  1. üretici 36 saatlik pencereden sayıyor, tüketici tek gün gösteriyordu (2026-09-24)
  2. görev dünü sayıyor, mobil günü oluşma tarihinden tahmin ediyordu
  3. mapper pencere alanlarını düşürüyordu (2026-09-28)
Birleşik bildirimde aynı sınıf **iki yeni kılık** kazanıyor: birleşimde çift sayma ve
görev/uç koşullarının ayrışması. Testler ikisini de tutuyor.
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from ekap.models import Authority, Tender
from tenders.models import FavoriteAuthority, Notification, SavedFilter
from tenders.tasks import (
    check_favorite_authority_matches,
    check_saved_filter_matches,
)

UC = "/api/v1/ekap/tenders/"


def _ihale(i, *, kayit_saat_once=4, sektor="su_kanalizasyon", durum=2,
           ihale_gun_sonra=20, idare_id=None, ilan_gun_once=0):
    """⚠️ `created_at` `auto_now_add` → sonradan `update()` ile yazılır.

    `ilan_gun_once` KAYIT tarihinden bağımsızdır: hafta sonu senaryosunda EKAP'ın yayım
    damgası geride kalırken kayıt bu sabah düşer. İki pencere ayrışmadan bu testler
    yanlış güvence verir.
    """
    ilan = timezone.now().replace(microsecond=0) - timedelta(days=ilan_gun_once)
    t = Tender.objects.create(
        ikn=f"2026/{i}", ekap_id=f"e{i}", ihale_adi=f"İş {i}", sektor=sektor,
        ihale_durum=durum, idare_id=idare_id or "",
        ilan_tarihi=ilan,
        ihale_tarihi=timezone.now() + timedelta(days=ihale_gun_sonra),
    )
    Tender.objects.filter(pk=t.pk).update(
        created_at=timezone.now() - timedelta(hours=kayit_saat_once))
    return t


@override_settings(NOTIF_BIRLESIK_BILDIRIM=True)
class FiltreBirlesikOzetTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="pro", email="pro@x.com", password="x",
            subscription_tier=User.Tier.PRO)
        self.su = SavedFilter.objects.create(
            user=self.user, name="Su", alarm=True,
            filters={"sektor": ["su_kanalizasyon"]})
        self.yol = SavedFilter.objects.create(
            user=self.user, name="Yol", alarm=True,
            filters={"sektor": ["yol_altyapi"]})

    def _bildirim(self):
        return Notification.objects.order_by("-created_at").first()

    def _say(self):
        n = self._bildirim()
        if not n:
            return None
        import re
        m = re.search(r"(\d+) ihale yayımlandı", n.body)
        return int(m.group(1)) if m else None

    # ── tek bildirim ───────────────────────────────────

    def test_UC_FILTRE_ESLESSE_DE_TEK_bildirim(self):
        """Şikâyetin kaynağı: 8 filtresi eşleşen kullanıcı 8 bildirim alıyordu."""
        SavedFilter.objects.create(user=self.user, name="Gıda", alarm=True,
                                   filters={"sektor": ["gida_catering"]})
        _ihale(1, sektor="su_kanalizasyon")
        _ihale(2, sektor="yol_altyapi")
        _ihale(3, sektor="gida_catering")
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(self._say(), 3)

    def test_IKI_FILTREYE_uyan_ihale_BIR_sayilir(self):
        """⚠️ Birleşimde çift sayma: aynı ihale iki filtreye uyuyorsa sayı 2 olmamalı."""
        SavedFilter.objects.create(user=self.user, name="Açık", alarm=True,
                                   filters={"ihale_durum": [2]})
        _ihale(1, sektor="su_kanalizasyon", durum=2)   # her iki filtreye de uyar
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1)

    def test_filtre_idler_CSV_olarak_yazilir(self):
        _ihale(1, sektor="su_kanalizasyon")
        check_saved_filter_matches()
        idler = {int(x) for x in self._bildirim().filtre_idler.split(",")}
        self.assertEqual(idler, {self.su.id, self.yol.id})

    def test_BIRLESIK_satirda_filter_id_BOS(self):
        """⚠️ Doldurulursa eski mobil sürüm 'birleşimin sayısı + tek filtrenin listesi'
        gösterir — iki kez düzeltilmiş arızanın üçüncü baskısı."""
        _ihale(1, sektor="su_kanalizasyon")
        check_saved_filter_matches()
        n = self._bildirim()
        self.assertIsNone(n.filter_id)
        self.assertFalse(n.idare_detsis_liste)

    def test_govde_FILTRE_ADI_icermez(self):
        """Ürün gereksinimi: 'şu filtrenin bu filtrenin değil'."""
        _ihale(1, sektor="su_kanalizasyon")
        check_saved_filter_matches()
        n = self._bildirim()
        self.assertNotIn("Su", n.body)
        self.assertNotIn("Yol", n.body)
        self.assertIn("son 24 saatte", n.body)

    def test_pencere_bildirimde_tasinir(self):
        _ihale(1, sektor="su_kanalizasyon")
        check_saved_filter_matches()
        n = self._bildirim()
        self.assertIsNotNone(n.pencere_bas)
        self.assertAlmostEqual(
            (n.pencere_bit - n.pencere_bas).total_seconds() / 3600, 24, places=3)

    def test_IKINCI_TUR_bildirim_uretmez(self):
        _ihale(1, sektor="su_kanalizasyon")
        check_saved_filter_matches()
        cache.delete(f"ozet:tur:filtre:{self.user.id}")
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.count(), 1)

    # ── görev ile uç AYNI kümeyi görüyor mu ────────────

    def test_UC_SAYIMI_govdedeki_sayiyla_AYNI(self):
        """⚠️⚠️ EN KRİTİK TEST. Bildirimin taşıdığı id'ler + pencereyle uca GERÇEK istek
        atılır; `totalCount` gövdedeki sayıya eşit olmalı. Görev ile uç koşulları
        ayrışırsa (birinde iptal elemesi var diğerinde yok gibi) burası kırmızı yanar."""
        for i in range(4):
            _ihale(i, sektor="su_kanalizasyon")
        _ihale(90, sektor="yol_altyapi")
        _ihale(91, sektor="gida_catering")       # hiçbir filtreye uymaz
        check_saved_filter_matches()
        n = self._bildirim()

        self.client.force_login(self.user)
        r = self.client.get(UC, {
            "kayitli_filtreler": n.filtre_idler,
            "created_at_min": n.pencere_bas.isoformat(),
            "created_at_max": n.pencere_bit.isoformat(),
        }).json()["data"]
        self.assertEqual(r["totalCount"], self._say())
        self.assertEqual(r["totalCount"], 5)

    def test_FILTRE_BASINA_durum_kosulu_digerini_SILMEZ(self):
        """⚠️⚠️ `ihale_durum in (2,3)` koşulu filtre-BAŞINA koşulludur: bir filtre
        `ihale_durum=[4]` derken diğeri demiyorsa, koşulu birleşime çekmek birincinin
        sonucunu siler. UNION'ın gerekçesi tam olarak bu."""
        SavedFilter.objects.create(user=self.user, name="Değerlendirme", alarm=True,
                                   filters={"ihale_durum": [4]})
        _ihale(1, sektor="su_kanalizasyon", durum=2)
        _ihale(2, sektor="temizlik_hizmeti", durum=4)   # yalnız durum filtresine uyar
        check_saved_filter_matches()
        self.assertEqual(self._say(), 2, "durum koşulu birleşime çekilmiş")

    def test_TEKLIF_SURESI_gecmis_ihale_bildirilmez(self):
        """Arşiv gürültüsüne karşı tek koruma; kaldırılırsa 2019 ihaleleri 'yeni' olur."""
        _ihale(1, sektor="su_kanalizasyon")
        _ihale(2, sektor="su_kanalizasyon", ihale_gun_sonra=-5)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1)

    def test_IKINCI_TURDA_sayi_PENCERE_TOPLAMI(self):
        """⚠️ Sayı "sana yeni olanlar" DEĞİL penceredeki toplam. `len(yeni)` yazmak
        ikisini yapısal olarak ayırır: bildirim "2 yeni" derken mobilin açtığı liste
        5 gösterir (üretimde birebir yaşandı, fid=60: bildirim 1, liste 3). Dedup
        *neyi* bildireceğimizi belirler, *kaç* diyeceğimizi değil."""
        for i in range(3):
            _ihale(i, sektor="su_kanalizasyon")
        check_saved_filter_matches()
        self.assertEqual(self._say(), 3)

        cache.delete(f"ozet:tur:filtre:{self.user.id}")
        for i in range(3, 5):
            _ihale(i, sektor="su_kanalizasyon")      # penceredeki 2 YENİ ihale
        check_saved_filter_matches()
        self.assertEqual(self._say(), 5, "ikinci bildirim yalnızca yeni olanları saydı")

    def test_25_saat_once_kaydedilen_bildirilmez(self):
        _ihale(1, sektor="su_kanalizasyon")
        _ihale(2, sektor="su_kanalizasyon", kayit_saat_once=25)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1)


@override_settings(NOTIF_BIRLESIK_BILDIRIM=True)
class IdareBirlesikOzetTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="pro2", email="pro2@x.com", password="x",
            subscription_tier=User.Tier.PRO)
        for no, iid in (("100", "i100"), ("200", "i200")):
            Authority.objects.create(detsis_no=no, parent_detsis="0",
                                     idare_id=iid, ad=f"İdare {no}")
            FavoriteAuthority.objects.create(user=self.user, detsis_no=no,
                                             idare_id=iid, ad=f"İdare {no}")

    def _bildirim(self):
        return Notification.objects.order_by("-created_at").first()

    def _say(self):
        import re
        m = re.search(r"(\d+) ihale yayımladı", self._bildirim().body)
        return int(m.group(1))

    def test_IKI_IDARE_icin_TEK_bildirim(self):
        _ihale(1, idare_id="i100")
        _ihale(2, idare_id="i200")
        check_favorite_authority_matches()
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(self._say(), 2)

    def test_UC_SAYIMI_govdedeki_sayiyla_AYNI(self):
        """⚠️ `favori_idareler` koşulları SUNUCUDA uyguluyor: mobil hiçbir durum /
        teklif-verilebilirlik parametresi göndermeden sayı tutmalı. Koşulları istemciye
        bırakmak, CLAUDE.md'de adı konmuş hatadır."""
        _ihale(1, idare_id="i100")
        _ihale(2, idare_id="i200")
        _ihale(3, idare_id="i100", ihale_gun_sonra=-5)   # süresi dolmuş
        _ihale(4, idare_id="baska")                      # favori değil
        check_favorite_authority_matches()
        n = self._bildirim()

        self.client.force_login(self.user)
        r = self.client.get(UC, {
            "favori_idareler": n.idare_detsis_liste,
            "created_at_min": n.pencere_bas.isoformat(),
            "created_at_max": n.pencere_bit.isoformat(),
        }).json()["data"]
        self.assertEqual(r["totalCount"], self._say())
        self.assertEqual(r["totalCount"], 2)

    def test_HAFTA_SONU_yayim_tarihi_ESKI_olsa_da_bildirilir(self):
        """⚠️⚠️ ASIL REGRESYON. Pencere yayım tarihinde olsaydı pazartesi sabahı boş
        kalırdı: EKAP cumartesi/pazar yayın yapmıyor (26-27 Eylül'de `ilan_tarihi`
        taşıyan sıfır ihale) ama kayıtlar gece düşüyor. Kayıt penceresi bu deliğe
        bağışıktır."""
        _ihale(1, idare_id="i100", ilan_gun_once=3)   # yayım 3 gün önce, kayıt 4 saat önce
        check_favorite_authority_matches()
        self.assertEqual(self._say(), 1, "pencere yayım tarihine bağlı kalmış")

    def test_govde_IDARE_ADI_icermez(self):
        _ihale(1, idare_id="i100")
        check_favorite_authority_matches()
        self.assertNotIn("İdare 100", self._bildirim().body)


class UcGuvenligiTest(TestCase):
    """`kayitli_filtreler` parametresinin kimlik / sahiplik / Pro / cache davranışı."""

    def setUp(self):
        cache.clear()
        self.a = User.objects.create_user(username="a", email="a@x.com", password="x",
                                          subscription_tier=User.Tier.PRO)
        self.b = User.objects.create_user(username="b", email="b@x.com", password="x",
                                          subscription_tier=User.Tier.PRO)
        self.af = SavedFilter.objects.create(user=self.a, name="A", alarm=True,
                                             filters={"sektor": ["su_kanalizasyon"]})
        for i in range(3):
            _ihale(i, sektor="su_kanalizasyon")
        _ihale(50, sektor="gida_catering")

    def test_ANONIM_istek_400_ve_tum_listeyi_DONDURMEZ(self):
        """⚠️ Sessizce yok saymak, kullanıcının istediğinden DAHA FAZLA sonuç döndürürdü.
        401 mobilde oturumu temizler, 403 Pro kapısıyla karışır → 400."""
        r = self.client.get(UC, {"kayitli_filtreler": str(self.af.id)})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["success"])

    def test_BASKASININ_filtresi_sonuc_vermez(self):
        self.client.force_login(self.b)
        r = self.client.get(UC, {"kayitli_filtreler": str(self.af.id)}).json()["data"]
        self.assertEqual(r["totalCount"], 0)

    def test_SILINMIS_id_bos_liste_ve_uyari(self):
        self.client.force_login(self.a)
        r = self.client.get(UC, {"kayitli_filtreler": "999999"}).json()["data"]
        self.assertEqual(r["totalCount"], 0)
        self.assertIn("uyari", r)

    def test_SAYI_CACHEI_kullanicilar_arasi_SIZMAZ(self):
        """⚠️ `_cached_count` anahtarı kullanıcı içermiyor; filtre id'leri global PK
        olduğu için B, A'nın id setini yazabilir → listesi boş döner AMA totalCount
        A'nın cache'inden gelirdi."""
        self.client.force_login(self.a)
        ra = self.client.get(UC, {"kayitli_filtreler": str(self.af.id)}).json()["data"]
        self.assertEqual(ra["totalCount"], 3)
        self.client.force_login(self.b)
        rb = self.client.get(UC, {"kayitli_filtreler": str(self.af.id)}).json()["data"]
        self.assertEqual(rb["totalCount"], 0, "sayı başka kullanıcıya sızdı")

    def test_FILTRE_JSONUNDAKI_pro_anahtari_kapiyi_tetikler(self):
        """⚠️ Pro kapısı `set(qp)` üzerinden çalışıyordu; `kayitli_filtreler` ile Pro
        anahtarları query string'den kaybolup kapı körleşiyordu."""
        free = User.objects.create_user(username="f", email="f@x.com", password="x")
        sf = SavedFilter.objects.create(
            user=free, name="Pro", alarm=True,
            filters={"sektor": ["su_kanalizasyon"], "yaklasik_maliyet_min": "1000"})
        self.client.force_login(free)
        r = self.client.get(UC, {"kayitli_filtreler": str(sf.id)})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "premium_required")

    def test_BOS_dal_digerlerini_oldurmez(self):
        """⚠️ `apply_tender_filters` çözülmeyen `idare_detsis`te `qs.none()` döndürüyor;
        Django boş dalı birleşimden düşürmeli, diğer dal çalışmaya devam etmeli."""
        SavedFilter.objects.create(user=self.a, name="Bos", alarm=True,
                                   filters={"idare_detsis": ["yok-boyle-dugum"]})
        self.client.force_login(self.a)
        idler = ",".join(str(x) for x in
                         SavedFilter.objects.filter(user=self.a).values_list("id", flat=True))
        r = self.client.get(UC, {"kayitli_filtreler": idler}).json()["data"]
        self.assertEqual(r["totalCount"], 3)

    def test_SAYFA_2_ayrik_satirlar_ayni_totalCount(self):
        """`pk__in=<union>` mevcut sıralama/sayfalama yolundan geçmeli."""
        self.client.force_login(self.a)
        ortak = {"kayitli_filtreler": str(self.af.id), "page_size": "2"}
        s1 = self.client.get(UC, {**ortak, "page": "1"}).json()["data"]
        s2 = self.client.get(UC, {**ortak, "page": "2"}).json()["data"]
        self.assertEqual(s1["totalCount"], s2["totalCount"])
        self.assertEqual(len(s1["list"]), 2)
        self.assertTrue(
            {i["ikn"] for i in s1["list"]}.isdisjoint({i["ikn"] for i in s2["list"]}))

    def test_TAVAN_sabiti_tek_kaynaktan(self):
        """Görev 50'de keserken uç 20'de keserse sayı tıklamada tutmaz."""
        from ekap.views import AZAMI_KAYITLI_FILTRE
        import tenders.tasks as t
        kaynak = t.__file__
        with open(kaynak, encoding="utf-8") as f:
            self.assertIn("AZAMI_KAYITLI_FILTRE", f.read())
        self.assertGreater(AZAMI_KAYITLI_FILTRE, 0)


class BayrakKapaliTest(TestCase):
    """⚠️ Geri alma yolu çürümemeli: bayrak kapalıyken abonelik-başına davranış sürer."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="p", email="p@x.com", password="x",
            subscription_tier=User.Tier.PRO)
        for ad, sek in (("Su", "su_kanalizasyon"), ("Yol", "yol_altyapi")):
            SavedFilter.objects.create(user=self.user, name=ad, alarm=True,
                                       filters={"sektor": [sek]})

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=False)
    def test_bayrak_kapali_ABONELIK_BASINA_bildirim(self):
        _ihale(1, sektor="su_kanalizasyon")
        _ihale(2, sektor="yol_altyapi")
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.count(), 2)
        self.assertTrue(all(n.filter_id for n in Notification.objects.all()))

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=False)
    def test_bayrak_kapali_IDARE_penceresi_de_KAYIT_tarihinde(self):
        """⚠️ İdare penceresinin yayım tarihinden kayıt tarihine taşınması bayraktan
        BAĞIMSIZ bir düzeltmedir (hafta sonu deliği). Eski yol da bundan faydalanmalı,
        yoksa bayrak kapalıyken pazartesi sabahları hâlâ boş kalır."""
        Authority.objects.create(detsis_no="300", parent_detsis="0",
                                 idare_id="i300", ad="İdare 300")
        FavoriteAuthority.objects.create(user=self.user, detsis_no="300",
                                         idare_id="i300", ad="İdare 300")
        _ihale(9, idare_id="i300", ilan_gun_once=3)   # yayım 3 gün önce, kayıt 4 saat önce
        check_favorite_authority_matches()
        n = Notification.objects.filter(authority_detsis="300").first()
        self.assertIsNotNone(n, "eski yol yayım tarihine bağlı kalmış")
        self.assertIn("İş 9", n.body)
        self.assertNotIn("Bugün", n.body)   # pencere kayıt tarihinde, "bugün" yanlış olur
