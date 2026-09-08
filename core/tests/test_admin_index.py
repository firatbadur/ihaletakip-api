"""Admin anasayfasını gerçekten render edip HTML'i doğrular (test DB'de)."""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

# ⚠️ Testler geliştirme Redis'ini paylaşırsa hem birbirlerinin metriklerini görürler
# (metrikler 60 sn cache'li) hem de yerel cache'i kirletirler.
IZOLE_CACHE = override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)

User = get_user_model()



@IZOLE_CACHE
class AdminIndexRenderTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser(
            username="firat", email="a@ornek.com", password="parola123!"
        )
        for i in range(5):
            User.objects.create_user(username=f"u{i}", email=f"u{i}@o.com", password="x")
        User.objects.filter(pk=self.admin.pk).update(last_seen_at=timezone.now())

    def test_pano_render_olur(self):
        self.client.force_login(self.admin)
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        # Jazzmin'in app-link kartları GİTMELİ
        self.assertNotIn("get_side_menu", html)
        # Pano öğeleri
        for beklenen in (
            "Genel Bakış", "Aylık Etkin (MAU)", "Toplam Kullanıcı", "Pro Üye",
            "Kaydedilen İhale", "Firma Profili", "Son 30 Gün — Yeni Kayıtlar",
            "chart-kayit-30g", "chart-abonelik", "chart-etkilesim",
            'id="dashboard-data"', "Son İşlemler", "ihaletakip/dashboard", "cdnjs.cloudflare.com", "it-kpi",
        ):
            self.assertIn(beklenen, html, f"eksik: {beklenen}")
        # Sayılar gerçek

    def test_sayilar_dogru(self):
        self.client.force_login(self.admin)
        html = self.client.get("/admin/").content.decode()
        import re, json
        m = re.search(r'id="dashboard-data"[^>]*>(.*?)</script>', html, re.S)
        self.assertIsNotNone(m, "dashboard-data düğümü yok")
        veri = json.loads(m.group(1))
        self.assertEqual(len(veri["kayit_30g"]["degerler"]), 30)
        self.assertEqual(veri["kayit_30g"]["degerler"][-1], 6)
        self.assertEqual(len(veri["kayit_12ay"]["etiketler"]), 12)
        self.assertEqual(sum(x["n"] for x in veri["abonelik"]), 6)
