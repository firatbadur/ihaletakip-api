"""
Kayıtlı filtre bildirimi — bildirimdeki sayı ile ekrandaki liste AYNI olmalı.

Test edilen arıza (üretimde ölçüldü 2026-09-24, kullanıcı bildirdi):
uç o günün 14 bildiriminden **12'sinde yanlış sayı** verdi ve **3'ü BOŞ liste**
açtı. Kullanıcının verdiği örnek birebir doğrulandı — "İçme Suyu ve Kanalizasyon"
filtresi *"10 ihale"* dedi, mobilin açtığı listede **5** vardı.

Sebep tek bir uyumsuzluktu:

    üretici (tenders.tasks)          : ilan_tarihi >= DÜN 00:00   (~36 saat)
    tüketici (notificationRouting.js): ilan_tarihi == bildirim günü (tek gün)

Mobil zaten tek gün varsayımıyla yazılmıştı (dosyadaki yorum: *"Backend filtre
alarmını yalnızca `ilan_tarihi = o gün` olan eşleşmeler için atar"*); backend o
sözleşmeyi pencereyi genişletirken bozmuştu.

⚠️ Pencerenin genişletilme gerekçesi (geç dolan `ilan_tarihi`) ölçümle çürütüldü:
son 14 günün 2.229 ihalesinin **%99,6'sının** detayı aynı gün geldi, hepsi 18:00
turundan önce. Ertesi güne sarkan yalnızca 9 ihale (%0,3).
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from ekap.models import Tender
from ekap.utils import local_day_range
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


class FiltreBildirimPenceresiTest(TestCase):
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

    def _say(self):
        """Bildirim gövdesindeki sayıyı döner (yoksa None)."""
        n = Notification.objects.filter(filter_id=self.sf.id).order_by("-created_at").first()
        if not n:
            return None
        import re
        m = re.search(r"bugün (\d+) ihale", n.body)
        return int(m.group(1)) if m else None

    def test_DUNKU_ihaleler_sayilmaz(self):
        """Kullanıcının bildirdiği vaka: 5 bugün + 7 dün → bildirim 5 demeli."""
        for i in range(5):
            _ihale(i, 0)          # bugün
        for i in range(5, 12):
            _ihale(i, 1)          # dün
        check_saved_filter_matches()
        self.assertEqual(self._say(), 5, "pencere dünü de sayıyor")

    def test_bugun_hic_ihale_yoksa_BILDIRIM_GITMEZ(self):
        """Üretimde 3/14 bildirim yalnızca dünkü ihalelerle üretilip BOŞ liste açıyordu."""
        for i in range(3):
            _ihale(i, 1)          # yalnızca dün
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.filter(filter_id=self.sf.id).count(), 0)

    def test_sayim_MOBILIN_ACACAGI_listeyle_ayni(self):
        """Bildirimdeki sayı = filtre + o gün. Mobil derin bağlantısı birebir bunu açar."""
        for i in range(4):
            _ihale(i, 0)
        _ihale(90, 0, adi="Alakasız Yemek Alımı")   # filtreye uymaz
        Tender.objects.filter(ekap_id="e90").update(sektor="gida_catering")
        check_saved_filter_matches()

        from ekap.views import apply_tender_filters
        gun = timezone.localdate().isoformat()
        mobil = apply_tender_filters(Tender.objects.all(), {
            **self.sf.filters, "ilan_tarihi_min": gun, "ilan_tarihi_max": gun,
        }).count()
        self.assertEqual(self._say(), mobil)

    def test_ikinci_tur_GUNUN_TOPLAMINI_soyler(self):
        """⚠️ Sayı 'sana yeni olanlar' DEĞİL günün toplamıdır: 14:00 turu '3 yeni'
        derken liste günün 8'ini gösteriyordu (üretimde fid=60: bildirim 1, liste 3)."""
        for i in range(3):
            _ihale(i, 0)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 3)

        cache.delete(f"filter:tur:{self.user.id}:{self.sf.id}")   # tur kilidini aç
        for i in range(3, 8):
            _ihale(i, 0)                                          # gün içinde 5 yeni
        check_saved_filter_matches()
        self.assertEqual(self._say(), 8, "ikinci bildirim yalnızca yeni olanları saydı")

    def test_yeni_ihale_yoksa_IKINCI_BILDIRIM_GITMEZ(self):
        """Dedup ihaleye bağlı: aynı ihaleler için ikinci tur bildirim üretmemeli."""
        for i in range(3):
            _ihale(i, 0)
        check_saved_filter_matches()
        cache.delete(f"filter:tur:{self.user.id}:{self.sf.id}")
        check_saved_filter_matches()
        self.assertEqual(Notification.objects.filter(filter_id=self.sf.id).count(), 1)

    def test_taranan_ile_isaretlenen_AYNI_kume(self):
        """⚠️ Eski kod 50 işaretleyip 20 bildiriyordu → 30 ihale 7 gün boyunca
        sessizce kayboluyordu. Taranan küme ne ise işaretlenen de o olmalı."""
        for i in range(25):
            _ihale(i, 0)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 25)
        isaretli = sum(1 for t in Tender.objects.all()
                       if cache.get(f"nf:{self.sf.id}:{t.pk}") is not None)
        self.assertEqual(isaretli, 25, "bildirilmeyen ihale 'bildirildi' diye işaretlendi")

    def test_AYAR_VARSAYILANI_sifir_olmali(self):
        """⚠️ Asıl düğme `config/settings.py`'deki varsayılandır; kodda `getattr`
        yedeğini değiştirmek hiçbir şey yapmaz (ayar tanımlı olduğu için yedek hiç
        kullanılmaz). Bu test varsayılanı sessizce 1'e çevirmeyi yakalar."""
        from django.conf import settings
        self.assertEqual(getattr(settings, "NOTIF_LOOKBACK_DAYS", None), 0)

    @override_settings(NOTIF_LOOKBACK_DAYS=1)
    def test_ayar_ile_pencere_genisletilebilir(self):
        """Geri dönüş yolu kapalı değil — ama mobil gün kısıtı da değişmeli."""
        _ihale(1, 0)
        _ihale(2, 1)
        check_saved_filter_matches()
        self.assertEqual(self._say(), 2)

    def test_GELECEK_tarihli_ilan_bildirilmez(self):
        """⚠️ Pencerenin ÜST SINIRI. `ilan_tarihi` detaydaki ilan listesinin en erken
        tarihinden türetiliyor (`_publish_date_from_ilanlar`) ve ileri tarihli
        olabiliyor. Üst sınır olmadan henüz yayımlanmamış bir ihale "bugün
        yayımlandı" diye bildirilir."""
        _ihale(1, 0)            # bugün
        _ihale(2, -1)           # YARIN tarihli
        check_saved_filter_matches()
        self.assertEqual(self._say(), 1, "gelecek tarihli ilan pencereye sızdı")
