"""`core.dashboard` testleri."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

# ⚠️ Testler geliştirme Redis'ini paylaşırsa hem birbirlerinin metriklerini görürler
# (metrikler 60 sn cache'li) hem de yerel cache'i kirletirler.
IZOLE_CACHE = override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)

from core.dashboard import aylik_seri, gunluk_seri, panel_metrikleri, pro_q, sparkline_path

User = get_user_model()



@IZOLE_CACHE
class ProQTests(TestCase):
    """
    ⚠️ Panodaki "Pro" sayısı ile kullanıcının gerçekten gördüğü özellik kapısı
    (`User.is_premium`) ayrışırsa sessiz bir yalan üretilir. Bu test iki tarafı
    aynı veri üzerinde karşılaştırarak ayrışmayı yakalar — `pro_q` ya da
    `is_premium` değişince buradan patlar.
    """

    def setUp(self):
        cache.clear()
        simdi = timezone.now()
        senaryolar = [
            # (tier, expires_at, is_superuser)
            (User.Tier.FREE, None, False),
            (User.Tier.FREE, simdi + timedelta(days=30), False),
            (User.Tier.PRO, None, False),                       # süresiz Pro
            (User.Tier.PRO, simdi + timedelta(days=1), False),   # süresi gelecekte
            (User.Tier.PRO, simdi - timedelta(days=1), False),   # süresi dolmuş
            (User.Tier.FREE, None, True),                       # superuser → her zaman Pro
            (User.Tier.PRO, simdi - timedelta(days=5), True),    # superuser + dolmuş
        ]
        for i, (tier, exp, su) in enumerate(senaryolar):
            User.objects.create_user(
                username=f"u{i}",
                email=f"u{i}@ornek.com",
                password="x",
                subscription_tier=tier,
                subscription_expires_at=exp,
                is_superuser=su,
            )

    def test_pro_q_is_premium_ile_ayni_kumeyi_verir(self):
        sql_kumesi = set(User.objects.filter(pro_q()).values_list("id", flat=True))
        python_kumesi = {u.id for u in User.objects.all() if u.is_premium}
        self.assertEqual(sql_kumesi, python_kumesi)

    def test_free_kumesi_tamamlayicidir(self):
        toplam = User.objects.count()
        pro = User.objects.filter(pro_q()).count()
        free = User.objects.exclude(pro_q()).count()
        self.assertEqual(pro + free, toplam)



@IZOLE_CACHE
class SeriTests(TestCase):
    def test_gunluk_seri_eksik_gunleri_sifirla_doldurur(self):
        """DB yalnızca kayıt olan günü döndürür; doldurulmazsa x ekseni yalan söyler."""
        User.objects.create_user(username="a", email="a@ornek.com", password="x")
        seri = gunluk_seri(User.objects.all(), "date_joined", 30)
        self.assertEqual(len(seri["etiketler"]), 30)
        self.assertEqual(len(seri["degerler"]), 30)
        self.assertEqual(seri["degerler"][-1], 1)   # bugün
        self.assertEqual(seri["toplam"], 1)
        self.assertEqual(sum(seri["degerler"][:-1]), 0)

    def test_aylik_seri_kumulatif_artan(self):
        User.objects.create_user(username="a", email="a@ornek.com", password="x")
        seri = aylik_seri(User.objects.all(), "date_joined", 12)
        self.assertEqual(len(seri["etiketler"]), 12)
        self.assertEqual(seri["kumulatif"], sorted(seri["kumulatif"]))
        self.assertEqual(seri["kumulatif"][-1], 1)

    def test_sparkline_tek_noktada_bos_doner(self):
        self.assertEqual(sparkline_path([]), "")
        self.assertEqual(sparkline_path([5]), "")
        self.assertTrue(sparkline_path([1, 5, 3]).startswith("M"))

    def test_sparkline_duz_seride_bolme_hatasi_vermez(self):
        """Tüm değerler eşitse aralık 0 olur — sıfıra bölme koruması."""
        self.assertTrue(sparkline_path([4, 4, 4, 4]).startswith("M"))



@IZOLE_CACHE
class PanelMetrikleriTests(TestCase):
    def test_bos_veritabaninda_patlamaz(self):
        pano = panel_metrikleri(force_refresh=True)
        for anahtar in ("ozet", "seriler", "ekap", "grafik", "uretildi"):
            self.assertIn(anahtar, pano)
        self.assertEqual(pano["ozet"]["kullanici"]["toplam"], 0)

    def test_bugunku_kayit_sayilir(self):
        User.objects.create_user(username="a", email="a@ornek.com", password="x")
        pano = panel_metrikleri(force_refresh=True)
        self.assertEqual(pano["ozet"]["kullanici"]["bugun"], 1)
        self.assertEqual(pano["ozet"]["kullanici"]["toplam"], 1)

    def test_mau_last_seen_at_ile_sayilir(self):
        u = User.objects.create_user(username="a", email="a@ornek.com", password="x")
        pano = panel_metrikleri(force_refresh=True)
        self.assertEqual(pano["ozet"]["kullanici"]["mau"], 0)
        self.assertEqual(pano["ozet"]["kullanici"]["hic_gorulmedi"], 1)

        User.objects.filter(pk=u.pk).update(last_seen_at=timezone.now())
        pano = panel_metrikleri(force_refresh=True)
        self.assertEqual(pano["ozet"]["kullanici"]["mau"], 1)
        self.assertEqual(pano["ozet"]["kullanici"]["dau"], 1)
