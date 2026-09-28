"""
Kayıtlı filtre bildirimi — **her sabah 08:00, son 24 saatte KAYDEDİLEN ihaleler**.

Üç ayrı arıza sınıfı test edilir; üçü de üretimde yaşandı:

1. **Sayı ile ekrandaki liste aynı olmalı** (2026-09-24). Üretici 36 saatlik
   pencereden sayıyor, tüketici (`notificationRouting.js`) tek gün gösteriyordu →
   o günün 14 bildiriminden **12'sinde yanlış sayı**, **3'ü BOŞ liste** açtı.

2. **Bildirimin saydığı ARALIK açıkça taşınmalı** (`pencere_bas`/`pencere_bit`).
   Mobil aralığı bildirimin oluşma tarihinden tahmin ederse saydığımız kümeyle
   açılan liste ayrışır — (1)'in ters yönü.

3. ⚠️⚠️ **HAFTA SONU DELİĞİ** (2026-09-28). Pencere `ilan_tarihi = DÜN` iken EKAP
   cumartesi/pazar yayın yapmadığı için pazar ve pazartesi sabahları **yapısal
   olarak boş** kalıyordu: 26-27 Eylül'de `ilan_tarihi` taşıyan sıfır ihale var,
   pazartesi 08:00 turu *"19 filtre, 0 bildirim"* yazdı ve kullanıcı bildirdi
   (25 Eylül'den beri hiç bildirim gitmemiş). → Referans **kayıt tarihi**
   (`Tender.created_at`): kayıtlar her gün 00:09-02:33 arasında düşüyor, yani
   08:00'de o günün tamamı sistemde. Takvimin boşluklarına bağışık.
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from ekap.models import Tender
from tenders.models import Notification, SavedFilter
from tenders.tasks import check_saved_filter_matches


def _ihale(i, kayit_saat_once, ilan_gun_once=0, adi="Kanalizasyon Hattı Yapım İşi",
           ihale_gun_sonra=20):
    """`kayit_saat_once` saat önce KAYDEDİLMİŞ bir ihale.

    ⚠️ `created_at` `auto_now_add` → `create()` sırasında verilemez, sonradan
    `update()` ile yazılır (sinyal/`save()` yolu onu yeniden ezerdi).
    ⚠️⚠️ `ilan_tarihi` **UTC gece yarısı** olarak saklanır, yerel gece yarısı DEĞİL
    (ingest `parse_ekap_datetime` kullanıyor; üretimde `...T00:00:00+00:00`, yerelde
    03:00). Fixture'ı yerel gece yarısıyla kurmak üretimde olmayan bir durum yaratır.
    """
    gun = timezone.localdate() - timedelta(days=ilan_gun_once)
    t = Tender.objects.create(
        ikn=f"2026/{i}", ekap_id=f"e{i}", ihale_adi=adi, sektor="su_kanalizasyon",
        ihale_durum=2,
        ilan_tarihi=datetime(gun.year, gun.month, gun.day, tzinfo=dt_timezone.utc),
        ihale_tarihi=timezone.now() + timedelta(days=ihale_gun_sonra),
    )
    Tender.objects.filter(pk=t.pk).update(
        created_at=timezone.now() - timedelta(hours=kayit_saat_once))
    return t


class FiltreKayitPenceresiTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="pro", email="pro@x.com", password="x",
            subscription_tier=User.Tier.PRO,
        )
        self.sf = SavedFilter.objects.create(
            user=self.user, name="İçme Suyu ve Kanalizasyon İhaleleri",
            filters={"sektor": ["su_kanalizasyon"], "ihale_durum": [2, 3]}, alarm=True,
        )

    def _bildirim(self):
        return (Notification.objects.filter(filter_id=self.sf.id)
                .order_by("-created_at").first())

    def _say(self):
        n = self._bildirim()
        if not n:
            return None
        import re
        m = re.search(r"(\d+) ihale yayımlandı", n.body)
        return int(m.group(1)) if m else None

    # ── pencere: kayan 24 saat, KAYIT tarihi ───────────

    def test_son_24_saatte_KAYDEDILEN_bildirilir(self):
        for i in range(5):
            _ihale(i, kayit_saat_once=6)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 5)

    def test_25_SAAT_once_kaydedilen_bildirilmez(self):
        _ihale(1, kayit_saat_once=2)
        _ihale(2, kayit_saat_once=25)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1, "pencere 24 saati aştı")

    def test_HAFTA_SONU_DELIGI_yok(self):
        """⚠️⚠️ ASIL REGRESYON. Pazartesi sabahı: EKAP cumartesi/pazar yayın yapmadı,
        yani `ilan_tarihi = dün` olan HİÇBİR ihale yok — ama bugünün ihaleleri gece
        01:00'de kaydedildi. Yayım gününe bakan pencere 0 bildirim üretiyordu."""
        for i in range(3):
            _ihale(i, kayit_saat_once=7, ilan_gun_once=0)   # bugün ilan, gece kaydedildi
        check_saved_filter_matches()
        self.assertEqual(self._say(), 3, "kayıt penceresi yayım gününe bağlı kalmış")

    def test_ilan_tarihi_ESKI_olsa_bile_kayit_penceresi_gecerli(self):
        """Kayıt tarihi 'bizim için ne yeni' sorusunu yanıtlar; EKAP'ın yayım damgası
        geç/ileri tarihli olabilir ve pencereyi belirlemez."""
        _ihale(1, kayit_saat_once=3, ilan_gun_once=9)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1)

    def test_ARSIV_GURULTUSU_teklif_suresi_gecmis_ihale_bildirilmez(self):
        """⚠️ Kayıt penceresinin tek zayıflığı arşiv doldurmadır (backfill eski bir
        ihaleyi bugün kaydedebilir). Koruma 'teklif verilebilir' şartıdır — bu test
        onu tutar, çünkü şart kaldırılırsa 2019 ihaleleri 'yeni' diye bildirilir."""
        _ihale(1, kayit_saat_once=2)
        _ihale(2, kayit_saat_once=2, ihale_gun_sonra=-30)   # teklif süresi dolmuş
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1, "teklif süresi geçmiş ihale bildirildi")

    def test_PENCERE_hesaplandiktan_SONRA_eklenen_ihale_sayilmaz(self):
        """⚠️ Üst sınır no-op DEĞİL. `created_at` geçmişte kalmaya mahkûm görünse de
        görev `tavan = now()` hesapladıktan **sonra** ingest yeni satır ekleyebilir
        (toplama gün boyu çalışıyor). Üst sınır olmadan görev o satırı sayar ama
        mobilin açtığı `[pencere_bas, pencere_bit]` aralığı onu dışlar → bildirimdeki
        sayı ekrandaki listeden büyük çıkar. Bu, iki kez düzelttiğimiz uyumsuzluk
        sınıfının yarış hâlidir."""
        _ihale(1, kayit_saat_once=2)
        sonradan = _ihale(2, kayit_saat_once=2)
        Tender.objects.filter(pk=sonradan.pk).update(
            created_at=timezone.now() + timedelta(hours=1))
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1, "pencere üst sınırı uygulanmadı")

    def test_penceredeki_ihale_yoksa_BILDIRIM_GITMEZ(self):
        _ihale(1, kayit_saat_once=30)
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.filter(filter_id=self.sf.id).count(), 0)

    # ── sayı = mobilin açacağı liste ───────────────────

    def test_sayim_MOBILIN_ACACAGI_listeyle_ayni(self):
        """Mobil `pencere_bas/bit`'i `created_at_min/max` olarak gönderir; küme birebir
        aynı olmalı. ⚠️ Bu API filtresi izin listesinde olmasa mobil onu sessizce
        düşürür ve liste filtrenin tüm geçmişini açardı."""
        for i in range(4):
            _ihale(i, kayit_saat_once=5)
        _ihale(90, kayit_saat_once=5, adi="Alakasız Yemek Alımı")
        Tender.objects.filter(ekap_id="e90").update(sektor="gida_catering")
        check_saved_filter_matches()

        from ekap.views import apply_tender_filters
        n = self._bildirim()
        mobil = apply_tender_filters(Tender.objects.all(), {
            **self.sf.filters,
            "created_at_min": n.pencere_bas.isoformat(),
            "created_at_max": n.pencere_bit.isoformat(),
        }).count()
        self.assertEqual(self._say(), mobil)

    def test_API_kayit_filtresi_GERCEKTEN_suzuyor(self):
        """⚠️ Sessiz başarısızlık koruması: parametre tanınmazsa filtre uygulanmaz ve
        sorgu 'her şeyi' döndürür — hata da vermez."""
        from ekap.views import apply_tender_filters
        _ihale(1, kayit_saat_once=2)
        _ihale(2, kayit_saat_once=40)
        taban = (timezone.now() - timedelta(hours=24)).isoformat()
        n = apply_tender_filters(Tender.objects.all(), {"created_at_min": taban}).count()
        self.assertEqual(n, 1, "created_at_min süzmedi")

    def test_PENCERE_bildirimde_tasinir(self):
        _ihale(1, kayit_saat_once=2)
        check_saved_filter_matches()
        n = self._bildirim()
        self.assertIsNotNone(n.pencere_bas)
        self.assertIsNotNone(n.pencere_bit)
        uzunluk = (n.pencere_bit - n.pencere_bas).total_seconds() / 3600
        self.assertAlmostEqual(uzunluk, 24, places=3)

    def test_govde_SON_24_SAAT_diyor(self):
        """Gövdedeki ifade sözleşmedir: takvim günü ("dün") demek, ölçülmüş hafta sonu
        deliğini metne taşımak olurdu — pencere takvim gününe oturmuyor."""
        _ihale(1, kayit_saat_once=2)
        check_saved_filter_matches()
        self.assertIn("son 24 saatte", self._bildirim().body)

    # ── mükerrerlik ────────────────────────────────────

    def test_IKINCI_TUR_bildirim_uretmez(self):
        for i in range(3):
            _ihale(i, kayit_saat_once=4)
        check_saved_filter_matches()
        cache.delete(f"filter:tur:{self.user.id}:{self.sf.id}")
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.filter(filter_id=self.sf.id).count(), 1)

    def test_taranan_ile_isaretlenen_AYNI_kume(self):
        """⚠️ Eski kod 50 işaretleyip 20 bildiriyordu → 30 ihale 7 gün sessizce
        kayboluyordu."""
        for i in range(25):
            _ihale(i, kayit_saat_once=4)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 25)
        isaretli = sum(1 for t in Tender.objects.all()
                       if cache.get(f"nf:{self.sf.id}:{t.pk}") is not None)
        self.assertEqual(isaretli, 25)

    # ── ayarlar ────────────────────────────────────────

    def test_AYAR_VARSAYILANI_24_olmali(self):
        """⚠️ Asıl düğme `config/settings.py`'deki varsayılandır; koddaki `getattr`
        yedeği ayar tanımlı olduğu için hiç kullanılmaz."""
        from django.conf import settings
        self.assertEqual(getattr(settings, "NOTIF_FILTER_HOURS", None), 24)

    @override_settings(NOTIF_FILTER_HOURS=48)
    def test_ayar_ile_pencere_degisir(self):
        _ihale(1, kayit_saat_once=2)
        _ihale(2, kayit_saat_once=30)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 2)
        self.assertIn("son 48 saatte", self._bildirim().body)
