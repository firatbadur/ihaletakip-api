"""Blog botu yazma ucu — anahtar, tekrar güvenliği, doğrulama."""
import io
import json
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from blog.models import BlogCategory, BlogPost

UC = "/api/v1/blog/bot/posts/"
ANAHTAR = "test-anahtari"


def _jpeg(ad="kapak.jpg"):
    from PIL import Image

    tampon = io.BytesIO()
    Image.new("RGB", (16, 9), "white").save(tampon, "JPEG")
    return SimpleUploadedFile(ad, tampon.getvalue(), content_type="image/jpeg")


def _govde(**kw):
    v = {
        "external_id": "kimlik-1",
        "title": "Ankara'da Yapım İhaleleri",
        "slug": "ankara-yapim-ihaleleri",
        "summary": "Ankara'daki yapım ihalelerinin seyri.",
        "content": "## Kaç ihale yapıldı?\n\nMetin.",
        "cover_alt": "Bir inşaat alanı",
        "tags": ["yapım ihalesi"],
    }
    v.update(kw)
    return v


@override_settings(BLOG_BOT_KEY=ANAHTAR)
class BlogBotUcuTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.medya = tempfile.mkdtemp()
        cls._ayar = override_settings(MEDIA_ROOT=cls.medya)
        cls._ayar.enable()

    @classmethod
    def tearDownClass(cls):
        cls._ayar.disable()
        shutil.rmtree(cls.medya, ignore_errors=True)
        super().tearDownClass()

    def _gonder(self, govde=None, *, anahtar=ANAHTAR, kapak=True):
        veri = {"payload": json.dumps(govde if govde is not None else _govde())}
        if kapak:
            veri["cover"] = _jpeg()
        return self.client.post(UC, veri, HTTP_X_BLOG_BOT_KEY=anahtar)

    def test_yazi_hemen_yayimlanir(self):
        r = self._gonder()
        self.assertEqual(r.status_code, 201, r.content)
        p = BlogPost.objects.get(external_id="kimlik-1")
        self.assertTrue(p.yayinda_mi())
        self.assertEqual(r.json()["data"]["slug"], "ankara-yapim-ihaleleri")
        self.assertEqual(p.author_name, "İhale Takip")
        self.assertEqual(self.client.get(f"/api/v1/blog/posts/{p.slug}/").status_code, 200)

    def test_ayni_kimlik_ikinci_yazi_acmaz(self):
        self._gonder()
        r = self._gonder(_govde(title="Değişmiş başlık"))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(BlogPost.objects.count(), 1)
        self.assertEqual(BlogPost.objects.get().title, "Ankara'da Yapım İhaleleri")

    def test_baska_yazinin_slugu_409(self):
        self._gonder()
        r = self._gonder(_govde(external_id="kimlik-2"))
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(BlogPost.objects.count(), 1)

    def test_anahtar_yok_ya_da_yanlis(self):
        self.assertEqual(self.client.post(UC, {}).status_code, 403)
        self.assertEqual(self._gonder(anahtar="yanlis").status_code, 403)
        self.assertFalse(BlogPost.objects.exists())

    @override_settings(BLOG_BOT_KEY="")
    def test_anahtar_tanimsizsa_uc_kapali(self):
        self.assertEqual(self._gonder(anahtar="").status_code, 403)

    def test_yasak_html_reddedilir(self):
        r = self._gonder(_govde(content="<script>alert(1)</script>"))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertFalse(BlogPost.objects.exists())

    def test_kapak_zorunlu(self):
        self.assertEqual(self._gonder(kapak=False).status_code, 400)

    def test_bozuk_kapak_reddedilir(self):
        r = self.client.post(
            UC,
            {"payload": json.dumps(_govde()),
             "cover": SimpleUploadedFile("kapak.jpg", b"gorsel degil", "image/jpeg")},
            HTTP_X_BLOG_BOT_KEY=ANAHTAR,
        )
        self.assertEqual(r.status_code, 400, r.content)

    def test_bozuk_payload_400(self):
        r = self.client.post(UC, {"payload": "{", "cover": _jpeg()}, HTTP_X_BLOG_BOT_KEY=ANAHTAR)
        self.assertEqual(r.status_code, 400)

    def test_seo_sinirlari(self):
        self.assertEqual(self._gonder(_govde(seo_title="x" * 61)).status_code, 400)
        self.assertEqual(self._gonder(_govde(seo_description="x" * 156)).status_code, 400)

    def test_kategori(self):
        BlogCategory.objects.get_or_create(slug="analiz", defaults={"name": "Analiz"})
        r = self._gonder(_govde(category_slug="analiz"))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(BlogPost.objects.get().category.slug, "analiz")
        r = self._gonder(_govde(external_id="k2", slug="baska", category_slug="yok"))
        self.assertEqual(r.status_code, 400)
