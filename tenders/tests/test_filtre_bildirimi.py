"""
Kayıtlı filtre bildirimi — **günlük özet** (her sabah 08:00, DÜN yayımlananlar).

İki ayrı arıza sınıfı test edilir:

1. **Sayı ile ekrandaki liste aynı olmalı.** Üretimde ölçüldü (2026-09-24, kullanıcı
   bildirdi): o günün 14 bildiriminden **12'sinde yanlış sayı**, **3'ü BOŞ liste**
   açtı. Sebep tek bir uyumsuzluktu:

       üretici (tenders.tasks)          : ilan_tarihi >= DÜN 00:00   (~36 saat)
       tüketici (notificationRouting.js): ilan_tarihi == bildirim günü (tek gün)

2. **Bildirimin kapsadığı gün AÇIKÇA taşınmalı** (`Notification.ilan_gun`).
   Görev sabah koşup **dünü** özetlediği için mobilin eski davranışı — günü
   bildirimin oluşma tarihinden tahmin etmek — bugüne düşer ve kullanıcı yine
   BOŞ liste görür. Aynı arızanın ters yönü.

⚠️ Pencerenin bir zamanlar genişletilme gerekçesi (geç dolan `ilan_tarihi`) ölçümle
çürütüldü: son 14 günün 2.229 ihalesinin **%99,6'sının** detayı aynı gün geldi.
Günlük özet gün kapandıktan sonra koştuğu için bu paya ayrıca bir gece ekler.
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from ekap.models import Tender
from tenders.models import Notification, SavedFilter
from tenders.tasks import check_saved_filter_matches


def _ihale(i, gun_once, adi="Kanalizasyon Hattı Yapım İşi"):
    """⚠️⚠️ `ilan_tarihi` **UTC gece yarısı** olarak saklanır, yerel gece yarısı DEĞİL.

    İngest `parse_ekap_datetime` kullanıyor ve o UTC döndürüyor → üretimde değer
    `2026-09-24T00:00:00+00:00` (yerelde 03:00). Fixture'ı `local_day_range(...)[0]`
    ile (yerel gece yarısı = 21:00Z önceki gün) kurmak testi **üretimde olmayan** bir
    duruma sokar ve sahte kırılma üretir — bu test yazılırken tam olarak bu yaşandı.
    """
    gun = timezone.localdate() - timedelta(days=gun_once)
    return Tender.objects.create(
        ikn=f"2026/{i}", ekap_id=f"e{i}", ihale_adi=adi, sektor="su_kanalizasyon",
        ihale_durum=2,
        ilan_tarihi=datetime(gun.year, gun.month, gun.day, tzinfo=dt_timezone.utc),
        ihale_tarihi=timezone.now() + timedelta(days=20),
    )


class FiltreGunlukOzetTest(TestCase):
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
        """Bildirim gövdesindeki sayıyı döner (yoksa None)."""
        n = self._bildirim()
        if not n:
            return None
        import re
        m = re.search(r"(\d+) ihale yayımlandı", n.body)
        return int(m.group(1)) if m else None

    # ── pencere: kapalı takvim günü (dün) ──────────────

    def test_DUNKU_ihaleler_bildirilir(self):
        """Sabah 08:00 özeti dünü kapsar: 7 dün + 5 bugün → bildirim 7 demeli."""
        for i in range(5):
            _ihale(i, 0)          # bugün (gün henüz bitmedi)
        for i in range(5, 12):
            _ihale(i, 1)          # dün
        check_saved_filter_matches()
        self.assertEqual(self._say(), 7)

    def test_BUGUNUN_ihaleleri_sayilmaz(self):
        """⚠️ Bitmemiş gün bildirilmez — yoksa sayı tur tur değişir ve kullanıcı aynı
        filtrede farklı sayılar görür (eski 10/14/18 kurgusunun şikâyeti)."""
        for i in range(3):
            _ihale(i, 0)          # yalnızca bugün
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.filter(filter_id=self.sf.id).count(), 0)

    def test_EVVELSI_GUN_sayilmaz(self):
        """Pencere bir GÜN, kayan bir aralık değil: evvelsi gün dünkü özete girmez."""
        _ihale(1, 1)              # dün
        _ihale(2, 2)              # evvelsi gün
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1, "pencere iki güne yayıldı")

    def test_dun_hic_ihale_yoksa_BILDIRIM_GITMEZ(self):
        """Üretimde 3/14 bildirim boş küme üzerinden üretilip BOŞ liste açıyordu."""
        _ihale(1, 0)              # bugün
        _ihale(2, 2)              # evvelsi gün
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.filter(filter_id=self.sf.id).count(), 0)

    # ── sayı = mobilin açacağı liste ───────────────────

    def test_sayim_MOBILIN_ACACAGI_listeyle_ayni(self):
        """Bildirimdeki sayı = filtre + `ilan_gun`. Mobil derin bağlantısı birebir bunu açar."""
        for i in range(4):
            _ihale(i, 1)
        _ihale(90, 1, adi="Alakasız Yemek Alımı")     # filtreye uymaz
        Tender.objects.filter(ekap_id="e90").update(sektor="gida_catering")
        check_saved_filter_matches()

        from ekap.views import apply_tender_filters
        gun = self._bildirim().ilan_gun.isoformat()
        mobil = apply_tender_filters(Tender.objects.all(), {
            **self.sf.filters, "ilan_tarihi_min": gun, "ilan_tarihi_max": gun,
        }).count()
        self.assertEqual(self._say(), mobil)

    def test_ILAN_GUN_bildirimde_tasinir(self):
        """⚠️⚠️ Mobil günü bildirimin oluşma tarihinden tahmin ediyordu; sabah üretilip
        dünü kapsayan bir bildirimde o tahmin BUGÜNE düşer ve boş liste açar."""
        _ihale(1, 1)
        check_saved_filter_matches()
        self.assertEqual(self._bildirim().ilan_gun,
                         timezone.localdate() - timedelta(days=1))

    def test_govde_DUN_diyor(self):
        """Gövdedeki zaman ifadesi sözleşmedir: 'bugün' yazıp dünü saymak aynı arızanın
        kılık değiştirmiş hâli olurdu."""
        _ihale(1, 1)
        check_saved_filter_matches()
        self.assertIn("dün", self._bildirim().body)

    # ── mükerrerlik ────────────────────────────────────

    def test_AYNI_GUN_IKINCI_TUR_bildirim_uretmez(self):
        """Elle tetikleme / yinelenmiş beat girdisi ikinci bildirim üretmemeli —
        ölçüldü (2026-09-23): tek kullanıcı 18 filtre bildirimi aldı, 6 filtre 2 kez."""
        for i in range(3):
            _ihale(i, 1)
        check_saved_filter_matches()
        cache.delete(f"filter:tur:{self.user.id}:{self.sf.id}")   # tur kilidini aç
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.filter(filter_id=self.sf.id).count(), 1)

    def test_taranan_ile_isaretlenen_AYNI_kume(self):
        """⚠️ Eski kod 50 işaretleyip 20 bildiriyordu → 30 ihale 7 gün boyunca
        sessizce kayboluyordu. Taranan küme ne ise işaretlenen de o olmalı."""
        for i in range(25):
            _ihale(i, 1)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 25)
        isaretli = sum(1 for t in Tender.objects.all()
                       if cache.get(f"nf:{self.sf.id}:{t.pk}") is not None)
        self.assertEqual(isaretli, 25, "bildirilmeyen ihale 'bildirildi' diye işaretlendi")

    # ── ayarlar ────────────────────────────────────────

    def test_AYAR_VARSAYILANI_bir_olmali(self):
        """⚠️ Asıl düğme `config/settings.py`'deki varsayılandır; kodda `getattr`
        yedeğini değiştirmek hiçbir şey yapmaz (ayar tanımlı olduğu için yedek hiç
        kullanılmaz). Bu test varsayılanı sessizce 0'a (bitmemiş güne) çevirmeyi yakalar."""
        from django.conf import settings
        self.assertEqual(getattr(settings, "NOTIF_FILTER_DAYS_AGO", None), 1)

    @override_settings(NOTIF_FILTER_DAYS_AGO=2)
    def test_ayar_ile_gun_kaydirilabilir(self):
        """Geri dönüş yolu kapalı değil: ayar hangi kapalı günün bildirileceğini seçer."""
        _ihale(1, 1)              # dün
        _ihale(2, 2)              # evvelsi gün
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1)
        self.assertEqual(self._bildirim().ilan_gun,
                         timezone.localdate() - timedelta(days=2))
