"""Blog — model kuralları, okuma uçları, web revalidate sinyali."""
import io
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from blog.models import BlogCategory, BlogPost, kapak_dogrula, turkce_slug

LISTE = "/api/v1/blog/posts/"


def _yazi(**kw):
    varsayilan = dict(title="Yazı", summary="Özet", content="metin",
                      status=BlogPost.Status.PUBLISHED)
    varsayilan.update(kw)
    return BlogPost.objects.create(**varsayilan)


def _png(boyut=(4, 4), bicim="PNG"):
    from PIL import Image

    tampon = io.BytesIO()
    Image.new("RGB", boyut, "white").save(tampon, bicim)
    return tampon.getvalue()


class SlugTest(TestCase):
    def test_turkce_karakterler_ascii(self):
        self.assertEqual(turkce_slug("Kılavuz: İhale Şartnamesi Çözümü ĞÜÖ"),
                         "kilavuz-ihale-sartnamesi-cozumu-guo")

    def test_baslik_bos_slugdan_uretilir_ve_benzersiz(self):
        a = _yazi(title="4734 Sayılı Kanun")
        b = _yazi(title="4734 Sayılı Kanun")
        self.assertEqual(a.slug, "4734-sayili-kanun")
        self.assertEqual(b.slug, "4734-sayili-kanun-2")

    def test_yayimlanmis_yazinin_slugi_degismez(self):
        p = BlogPost.objects.get(pk=_yazi(title="Sabit").pk)
        p.slug = "baska"
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_taslakta_slug_degisebilir(self):
        p = BlogPost.objects.get(pk=_yazi(title="Taslak", status="draft").pk)
        p.slug = "yeni-adres"
        p.full_clean()  # hata yok

    def test_yayin_tarihi_ve_okuma_suresi(self):
        p = _yazi(content="kelime " * 401)
        self.assertIsNotNone(p.published_at)
        self.assertEqual(p.reading_minutes, 3)
        self.assertEqual(_yazi(content="").reading_minutes, 1)

    def test_ham_html_reddedilir(self):
        for kotu in ("<script>alert(1)</script>", '<img src=x onerror="a()">',
                     "<iframe src=//x></iframe>", "[t](javascript:alert(1))"):
            with self.subTest(kotu=kotu):
                with self.assertRaises(ValidationError):
                    BlogPost(title="t", summary="s", content=kotu).full_clean()
        BlogPost(title="t", summary="s", slug="t", content="**kalın** [a](https://x)").full_clean()

    def test_kapak_varsa_alt_metin_zorunlu(self):
        p = BlogPost(title="t", summary="s", slug="t", content="c")
        p.cover_image.name = "blog/x.png"
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("cover_alt", ctx.exception.message_dict)

    def test_kapak_bicim_ve_boyut(self):
        kapak_dogrula(SimpleUploadedFile("a.png", _png()))
        with self.assertRaises(ValidationError):
            kapak_dogrula(SimpleUploadedFile("a.gif", _png(bicim="GIF")))
        buyuk = SimpleUploadedFile("a.png", b"0" * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValidationError):
            kapak_dogrula(buyuk)

    def test_etiket_temizligi(self):
        p = _yazi(tags=[" eşik  değer ", "EŞİK DEĞER", "4734", ""])
        self.assertEqual(p.tags, ["eşik değer", "4734"])


@override_settings(WEB_REVALIDATE_SECRET="", PUBLIC_BASE_URL="https://api.ornek")
class UcTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mevzuat = BlogCategory.objects.create(name="Mevzuat", slug="mevzuat", order=1)
        cls.rehber = BlogCategory.objects.create(name="Rehber", slug="rehber", order=0)
        BlogCategory.objects.create(name="Boş", slug="bos", order=2)
        simdi = timezone.now()
        for i in range(15):
            _yazi(title=f"Mevzuat {i}", category=cls.mevzuat,
                  published_at=simdi - timedelta(days=i), tags=["Eşik Değer"])
        cls.rehber_yazi = _yazi(title="Şartname Okuma Kılavuzu", category=cls.rehber,
                                summary="İhale dokümanı nasıl okunur")
        cls.taslak = _yazi(title="Taslak", status="draft", category=cls.mevzuat)
        cls.ileri = _yazi(title="Gelecek", category=cls.mevzuat,
                          published_at=simdi + timedelta(days=1))

    def test_taslak_ve_ileri_tarihli_gorunmez(self):
        d = self.client.get(LISTE, {"page_size": 50}).json()["data"]
        sluglar = {r["slug"] for r in d["list"]}
        self.assertEqual(d["totalCount"], 16)
        self.assertNotIn(self.taslak.slug, sluglar)
        self.assertNotIn(self.ileri.slug, sluglar)
        for p in (self.taslak, self.ileri):
            r = self.client.get(f"{LISTE}{p.slug}/")
            self.assertEqual(r.status_code, 404)
            self.assertEqual(r.json()["message"], "Yazı bulunamadı.")

    def test_kategori_sayfalama(self):
        d = self.client.get(LISTE, {"kategori": "mevzuat", "page": 2,
                                    "page_size": 12}).json()["data"]
        self.assertEqual(d["totalCount"], 15)
        self.assertEqual(d["page"], 2)
        self.assertEqual([r["title"] for r in d["list"]], ["Mevzuat 12", "Mevzuat 13", "Mevzuat 14"])

    def test_page_size_tavani_ve_varsayilan(self):
        self.assertEqual(len(self.client.get(LISTE).json()["data"]["list"]), 12)
        self.assertEqual(len(self.client.get(LISTE, {"page_size": 999}).json()["data"]["list"]), 16)

    def test_listede_content_yok(self):
        satir = self.client.get(LISTE).json()["data"]["list"][0]
        self.assertNotIn("content", satir)
        self.assertRegex(satir["published_at"], r"[+-]\d\d:\d\d$|Z$")

    def test_turkce_arama_ve_etiket(self):
        d = self.client.get(LISTE, {"q": "SARTNAME kilavuzu"}).json()["data"]
        self.assertEqual([r["slug"] for r in d["list"]], [self.rehber_yazi.slug])
        d = self.client.get(LISTE, {"etiket": "esik deger"}).json()["data"]
        self.assertEqual(d["totalCount"], 15)

    def test_detay_seo_ve_ilgili(self):
        p = BlogPost.objects.get(title="Mevzuat 0")
        d = self.client.get(f"{LISTE}{p.slug}/").json()["data"]
        self.assertEqual(d["content"], "metin")
        self.assertEqual(d["seo_title"], "Mevzuat 0")
        self.assertEqual(d["seo_description"], "Özet")
        self.assertEqual(len(d["related"]), 3)
        self.assertNotIn(p.slug, {r["slug"] for r in d["related"]})
        self.assertNotIn(self.taslak.slug, {r["slug"] for r in d["related"]})

    def test_kategoriler_yalniz_yayinli(self):
        d = self.client.get("/api/v1/blog/categories/").json()["data"]
        self.assertEqual(d, [{"name": "Rehber", "slug": "rehber", "post_count": 1},
                             {"name": "Mevzuat", "slug": "mevzuat", "post_count": 15}])

    def test_kapak_url_mutlak_https(self):
        p = BlogPost.objects.get(title="Mevzuat 0")
        BlogPost.objects.filter(pk=p.pk).update(cover_image="blog/abc.jpg", cover_alt="a")
        d = self.client.get(f"{LISTE}{p.slug}/").json()["data"]
        self.assertEqual(d["cover_image"], "https://api.ornek/media/blog/abc.jpg")


class MedyaTest(TestCase):
    def setUp(self):
        self.kok = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.kok)

    def test_kapak_sunulur_uzun_onbellek(self):
        import os

        os.makedirs(os.path.join(self.kok, "blog"))
        with open(os.path.join(self.kok, "blog", "a.png"), "wb") as f:
            f.write(_png())
        with override_settings(MEDIA_ROOT=self.kok):
            r = self.client.get("/media/blog/a.png")
            self.assertEqual(r.status_code, 200)
            self.assertIn("immutable", r["Cache-Control"])
            self.assertEqual(self.client.get("/media/blog/../x").status_code, 404)
            self.assertEqual(self.client.get("/media/blog/yok.png").status_code, 404)


@override_settings(WEB_REVALIDATE_URL="https://web.ornek/api/revalidate",
                   WEB_REVALIDATE_SECRET="sir")
class RevalidateTest(TestCase):
    def _cagrilar(self, islem):
        with mock.patch("requests.post") as post, \
                self.captureOnCommitCallbacks(execute=True):
            post.return_value.status_code = 200
            islem()
        return post

    def test_yayimlaninca_cagrilir(self):
        post = self._cagrilar(lambda: _yazi(title="Yeni Yazı"))
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"], {"tag": "blog", "slug": "yeni-yazi"})
        self.assertEqual(post.call_args.kwargs["headers"]["X-Revalidate-Secret"], "sir")
        self.assertEqual(post.call_args.kwargs["timeout"], 5)

    def test_taslak_kaydi_cagirmaz(self):
        self.assertFalse(self._cagrilar(lambda: _yazi(status="draft")).called)

    def test_yayindan_kaldirma_ve_silme_cagirir(self):
        p = BlogPost.objects.get(pk=_yazi(title="Kalkacak").pk)

        def kaldir():
            p.status = "draft"
            p.save()

        self.assertTrue(self._cagrilar(kaldir).called)
        q = BlogPost.objects.get(pk=_yazi(title="Silinecek").pk)
        self.assertTrue(self._cagrilar(q.delete).called)

    def test_web_kapaliyken_kayit_basarili(self):
        with mock.patch("requests.post", side_effect=ConnectionError("kapalı")), \
                self.captureOnCommitCallbacks(execute=True):
            p = _yazi(title="Web Kapalı")
        self.assertTrue(BlogPost.objects.filter(pk=p.pk).exists())

    def test_zamanlanmis_gorev(self):
        from blog.tasks import zamanlanmis_yazilari_bildir

        simdi = timezone.now()
        with mock.patch("requests.post"):
            _yazi(title="Az önce", published_at=simdi - timedelta(minutes=5))
            _yazi(title="Eski", published_at=simdi - timedelta(days=2))
            _yazi(title="Sonra", published_at=simdi + timedelta(hours=1))
        with mock.patch("requests.post") as post:
            post.return_value.status_code = 200
            sonuc = zamanlanmis_yazilari_bildir()
        self.assertEqual(sonuc, {"aday": 1, "gonderilen": 1})

    @override_settings(WEB_REVALIDATE_SECRET="")
    def test_sir_yoksa_istek_atilmaz(self):
        self.assertFalse(self._cagrilar(lambda: _yazi(title="Sırsız")).called)
