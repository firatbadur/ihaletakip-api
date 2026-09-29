"""
**Filtre alarmı + favori idare alarmı Free'ye açıldı** (2026-09-29, ürün kararı).

Önceki hâl: uç alarmlı filtre kaydını Free üyeye **403** ile reddediyordu ve iki beat
görevi de `if not user.is_premium: continue` ile Free üyeyi **sayılmadan** eliyordu.

Bu dosya dört şeyi tutar:

1. **Free üye gerçekten bildirim alıyor** — dört kod yolunun hepsinde
   (`NOTIF_BIRLESIK_BILDIRIM` açık/kapalı × filtre/idare). Bayrak bugün **kapalı**,
   yani asıl üretim yolu `_filtre_abonelik_basina` / `_idare_abonelik_basina`; yalnız
   birini test etmek yanlış güvence olurdu.
2. **Uç 403 vermiyor** — alarmlı filtre oluşturma ve alarmı açan güncelleme.
3. ⚠️⚠️ **Pro parametreli filtre Free üyede ATLANIR.** Alarm serbestleşti ama
   gelişmiş arama filtrelerinin KENDİSİ (tutar/rekabet/indirim) Pro'da kaldı →
   bildirim üretilseydi kullanıcı basınca arama ucu `premium_required` 403 döner ve
   **açılamayan bir bildirim** kalırdı. Bu testler o kapının açık kalmasını sağlar.
4. **Pro'da kalanlar Pro'da kaldı** — ihale alarmı ve takip edilen firma bildirimi.
   Karar yalnızca iki alarmı kapsıyordu; bu sınıflar regresyon bekçisidir.
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from ekap.models import Authority, Tender
from tenders.models import (
    FavoriteAuthority,
    Notification,
    SavedFilter,
    TenderAlarm,
)
from tenders.tasks import (
    check_favorite_authority_matches,
    check_saved_filter_matches,
    check_tender_alarms,
)

UC_FILTRE = "/api/v1/saved-filters/"


def _ihale(i, *, sektor="su_kanalizasyon", idare_id="", kayit_saat_once=6,
           ihale_gun_sonra=20, yaklasik_maliyet=5000):
    """Son `kayit_saat_once` saatte KAYDEDİLMİŞ, teklife açık bir ihale.

    ⚠️ `created_at` `auto_now_add` → yalnızca `update()` ile yazılabilir.
    ⚠️⚠️ `yaklasik_maliyet` **varsayılan olarak DOLU**: Pro parametre testleri
    `yaklasik_maliyet_min` kullanıyor ve NULL hiçbir aralık koşuluna girmez → maliyeti
    boş bırakılsaydı ihale zaten elenir, "bildirim yok" sonucu **atlama kuralından
    değil veri eksikliğinden** gelirdi. Diş kontrolünde tam olarak bu yakalandı:
    kural kaldırıldığında 3 test yerine yalnızca 1'i kırılıyordu.
    """
    t = Tender.objects.create(
        ikn=f"2026/{i}", ekap_id=f"e{i}", ihale_adi=f"İş {i}", sektor=sektor,
        ihale_durum=2, idare_id=idare_id,
        ilan_tarihi=timezone.now().replace(microsecond=0),
        ihale_tarihi=timezone.now() + timedelta(days=ihale_gun_sonra),
    )
    Tender.objects.filter(pk=t.pk).update(
        created_at=timezone.now() - timedelta(hours=kayit_saat_once),
        yaklasik_maliyet_num=yaklasik_maliyet)
    return t


class _Taban(TestCase):
    def setUp(self):
        cache.clear()
        self.free = User.objects.create_user(
            username="free", email="free@x.com", password="x")
        # Sözleşmeyi test seviyesinde de sabitle: "free" gerçekten Free olmalı.
        self.assertFalse(self.free.is_premium)


# ══════════════════════════════════════════════════════════════════════════════
# 1) Free üye bildirim alıyor — dört kod yolu
# ══════════════════════════════════════════════════════════════════════════════

class FreeFiltreBildirimiTest(_Taban):
    def setUp(self):
        super().setUp()
        self.sf = SavedFilter.objects.create(
            user=self.free, name="Su", alarm=True,
            filters={"sektor": ["su_kanalizasyon"]})

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=False)
    def test_ABONELIK_BASINA_yolunda_free_bildirim_alir(self):
        _ihale(1)
        _ihale(2)
        check_saved_filter_matches()
        n = Notification.objects.get()
        self.assertEqual(n.filter_id, self.sf.id)
        self.assertIn("2 ihale", n.body)

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=True)
    def test_BIRLESIK_yolunda_free_bildirim_alir(self):
        _ihale(1)
        check_saved_filter_matches()
        n = Notification.objects.get()
        self.assertEqual(n.filtre_idler, str(self.sf.id))

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=False)
    def test_ALARM_KAPALI_filtre_yine_bildirilmez(self):
        """Serbestleşen şey alarm HAKKI; `alarm=False` hâlâ "bildirim istemiyorum"."""
        self.sf.alarm = False
        self.sf.save(update_fields=["alarm"])
        _ihale(1)
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.count(), 0)


class FreeIdareBildirimiTest(_Taban):
    def setUp(self):
        super().setUp()
        Authority.objects.create(detsis_no="100", parent_detsis="0",
                                 idare_id="i100", ad="İdare 100")
        FavoriteAuthority.objects.create(user=self.free, detsis_no="100",
                                         idare_id="i100", ad="İdare 100")

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=False)
    def test_ABONELIK_BASINA_yolunda_free_bildirim_alir(self):
        _ihale(1, idare_id="i100")
        check_favorite_authority_matches()
        n = Notification.objects.get()
        self.assertEqual(n.authority_detsis, "100")

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=True)
    def test_BIRLESIK_yolunda_free_bildirim_alir(self):
        _ihale(1, idare_id="i100")
        check_favorite_authority_matches()
        n = Notification.objects.get()
        self.assertEqual(n.idare_detsis_liste, "100")


# ══════════════════════════════════════════════════════════════════════════════
# 2) Uç artık 403 vermiyor
# ══════════════════════════════════════════════════════════════════════════════

class FiltreAlarmUcuTest(_Taban):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.free)

    def test_ALARMLI_filtre_olusturma_201(self):
        r = self.client.post(UC_FILTRE, {
            "name": "Su", "filters": {"sektor": ["su_kanalizasyon"]}, "alarm": True,
        }, content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:300])
        self.assertTrue(SavedFilter.objects.get().alarm)

    def test_ALARMI_ACAN_guncelleme_200(self):
        sf = SavedFilter.objects.create(
            user=self.free, name="Su", filters={"sektor": ["su_kanalizasyon"]},
            alarm=False)
        r = self.client.patch(f"{UC_FILTRE}{sf.id}/", {"alarm": True},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:300])
        sf.refresh_from_db()
        self.assertTrue(sf.alarm)


# ══════════════════════════════════════════════════════════════════════════════
# 3) Pro parametreli filtre: bildirilen küme = AÇILABİLEN küme
# ══════════════════════════════════════════════════════════════════════════════

class ProParametreliFiltreTest(_Taban):
    """⚠️ Alarm serbest ama filtrenin İÇİNDEKİ Pro parametre değil. Bildirim
    üretilseydi kullanıcı basınca `TenderListView` 403 döner, bildirim ölü bir
    bağlantı olurdu."""

    def setUp(self):
        super().setUp()
        self.pro = User.objects.create_user(
            username="pro", email="pro@x.com", password="x",
            subscription_tier=User.Tier.PRO)

    def _filtre(self, user, **ek):
        return SavedFilter.objects.create(
            user=user, name="Tutarlı", alarm=True,
            filters={"sektor": ["su_kanalizasyon"], **ek})

    def test_KONTROL_ihale_pro_filtreye_GERCEKTEN_uyuyor(self):
        """⚠️ Bu bir sözleşme testi: aşağıdaki üç "bildirim yok" testinin **yanlış
        sebepten** geçmediğini garanti eder. Fixture ihalesi `yaklasik_maliyet_min`
        koşulunu sağlamasaydı zaten elenirdi ve atlama kuralı hiç sınanmazdı."""
        from ekap.views import apply_tender_filters

        _ihale(1)
        esleşen = apply_tender_filters(
            Tender.objects.all(),
            {"sektor": ["su_kanalizasyon"], "yaklasik_maliyet_min": "1000"})
        self.assertEqual(esleşen.count(), 1)

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=False)
    def test_FREE_uyede_pro_parametreli_filtre_ATLANIR(self):
        self._filtre(self.free, yaklasik_maliyet_min="1000")
        _ihale(1)
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.count(), 0)

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=True)
    def test_BIRLESIK_yolda_da_ATLANIR(self):
        self._filtre(self.free, yaklasik_maliyet_min="1000")
        _ihale(1)
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.count(), 0)

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=True)
    def test_ATLANAN_filtre_digerlerini_oldurmez(self):
        """Pro parametreli filtre atlanır; aynı kullanıcının sade filtresi bildirilir
        ve birleşik satırın id listesinde **yalnızca** o bulunur."""
        self._filtre(self.free, yaklasik_maliyet_min="1000")
        sade = SavedFilter.objects.create(
            user=self.free, name="Sade", alarm=True,
            filters={"sektor": ["su_kanalizasyon"]})
        _ihale(1)
        check_saved_filter_matches()
        n = Notification.objects.get()
        self.assertEqual(n.filtre_idler, str(sade.id))

    @override_settings(NOTIF_BIRLESIK_BILDIRIM=False)
    def test_PRO_uyede_pro_parametreli_filtre_BILDIRILIR(self):
        """Atlama yalnızca Free'ye özgü; Pro üye o filtreyi zaten açabiliyor."""
        sf = self._filtre(self.pro, yaklasik_maliyet_min="1000")
        _ihale(1)  # maliyeti dolu → filtre gerçekten eşleşir
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.get().filter_id, sf.id)


# ══════════════════════════════════════════════════════════════════════════════
# 4) Pro'da KALANLAR — regresyon bekçisi
# ══════════════════════════════════════════════════════════════════════════════

class ProDaKalanlarTest(_Taban):
    def test_IHALE_ALARMI_free_uyeye_bildirim_URETMEZ(self):
        t = _ihale(1)
        Tender.objects.filter(pk=t.pk).update(ihale_tarihi=timezone.now())
        TenderAlarm.objects.create(user=self.free, tender_id=t.ekap_id,
                                   tender_ikn=t.ikn, reminder_day=True)
        check_tender_alarms()
        self.assertEqual(Notification.objects.count(), 0)

    def test_IHALE_ALARMI_ucu_free_uyeye_403(self):
        self.client.force_login(self.free)
        r = self.client.post("/api/v1/alarms/", {
            "tender_id": "e1", "tender_ikn": "2026/1", "reminder_day": True,
        }, content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "premium_required")


class TeaserKaldirildiTest(TestCase):
    """`weekly_free_teaser` kaldırıldı: saydığı iki kaynak (filtre + favori idare)
    artık Free üyeye **her gün** bildiriliyor → "bu hafta N ihale kaçırdınız" yalan
    olurdu. Görev, şablonu ve beat girdisi birlikte silinmeli; biri kalırsa yalan
    geri döner."""

    def test_gorev_fonksiyonu_YOK(self):
        import tenders.tasks as t
        self.assertFalse(hasattr(t, "weekly_free_teaser"))

    def test_sablon_YOK(self):
        from tenders.services import templates
        self.assertFalse(hasattr(templates, "free_teaser"))

    def test_beat_girdisi_YOK(self):
        from config.celery import app
        self.assertNotIn("tenders-weekly-free-teaser", app.conf.beat_schedule)
