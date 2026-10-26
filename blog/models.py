"""
Blog — web portalının (ihaletakip.com) "Blog" sekmesi.

Yazılar **yalnızca Django admin'den** yazılır; web portalı salt okur
(`/api/v1/blog/...`). Mobil uygulama bu tablolara hiç dokunmaz.

⚠️ **"Yayımlanmış" tanımının tek kaynağı `BlogPost.objects.yayinda()`**:
`status == published` **ve** `published_at <= now()`. İleri tarihli yazı
"zamanlanmış"tır ve hiçbir uçta görünmez; uçlar bu tanımı ayrı ayrı yazarsa
zamanla ayrışır.
"""
import hashlib
import math
import os
import re

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from ekap.utils import normalize_tr

AZAMI_ETIKET = 10
AZAMI_ETIKET_UZUNLUK = 40
AZAMI_KAPAK_BAYT = 2 * 1024 * 1024
KAPAK_BICIMLERI = {"JPEG", "PNG", "WEBP"}
DAKIKADA_KELIME = 200

# ⚠️ Web, Markdown'ı HTML'e çevirip AYRICA temizliyor; bu doğrulama ikinci kat.
# Hedef kaza/kopyala-yapıştır (bir siteden gömme kodu yapıştırmak), kararlı bir
# saldırgan değil — yazma yetkisi zaten yalnızca admin'de.
_YASAK_HTML = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|form|style)\b"   # yasak etiketler
    r"|<[^>]*\son[a-z]+\s*="                                # on* öznitelikleri
    r"|\]\(\s*javascript:"                                  # [x](javascript:…)
    r"|<[^>]*=\s*['\"]?\s*javascript:",                     # href="javascript:…"
    re.IGNORECASE,
)


def medya_yolu(ad: str) -> str:
    """Depodaki dosya adı → kök-göreli yol (`/media/blog/…`).

    ⚠️ `MEDIA_URL` bu projede `"media/"` (baştaki `/` yok) → `FieldFile.url` göreli
    bir yol döndürür ve admin'de sayfaya göre çözülüp kırılır. Yol burada kurulur.
    """
    from django.conf import settings

    return "/" + settings.MEDIA_URL.strip("/") + "/" + ad.lstrip("/")


def mutlak_medya_url(ad: str) -> str | None:
    """Web'in `<img>`'de doğrudan kullanacağı **mutlak https** adres.

    ⚠️ `request.build_absolute_uri` KULLANILMAZ: portal API'ye Cloudflare'i atlayıp
    doğrudan nginx'ten geliyor ve o yolda `X-Forwarded-Proto` taşınmıyor → adres
    `http://` çıkardı. Kanonik taban ayardan gelir (`PUBLIC_BASE_URL`).
    """
    if not ad:
        return None
    from django.conf import settings

    return settings.PUBLIC_BASE_URL.rstrip("/") + medya_yolu(ad)


def turkce_slug(metin: str, azami: int = 180) -> str:
    """Başlıktan ASCII slug: ğ→g, ş→s, ı/İ→i, ö→o, ü→u, ç→c.

    ⚠️ Düz `slugify` YETMEZ: "İ" NFKD ile "I + nokta"ya ayrılır ve "ı" hiç ASCII
    karşılığı bulamayıp **silinir** ("Kılavuz" → "klavuz"). Önce `normalize_tr`
    (arama katmanının ortak katlaması) uygulanır.
    """
    return slugify(normalize_tr(metin))[:azami].strip("-")


def kapak_dogrula(dosya):
    # ⚠️ Yalnızca YENİ yüklemeyi doğrula: kayıtlı bir görsel her düzenlemede yeniden
    # açılırsa, depodan silinmiş/taşınmış bir dosya yazıyı düzenlenemez hâle getirirdi.
    if getattr(dosya, "_committed", False):
        return
    if dosya.size and dosya.size > AZAMI_KAPAK_BAYT:
        raise ValidationError("Kapak görseli en fazla 2 MB olabilir.")
    from PIL import Image  # ağır import lazy (bkz. Kodlama Kuralları)

    try:
        konum = dosya.tell()
        bicim = Image.open(dosya).format
        dosya.seek(konum)
    except Exception as e:  # noqa: BLE001 — bozuk dosya da aynı mesajı alır
        raise ValidationError("Kapak görseli okunamadı.") from e
    # ⚠️ Uzantı yetmez: `.jpg` uzantılı bir GIF/SVG de yüklenebilirdi.
    if bicim not in KAPAK_BICIMLERI:
        raise ValidationError("Kapak görseli JPEG, PNG ya da WebP olmalıdır.")


def kapak_yolu(instance, filename):
    """`blog/<içerik-hash>.<uzantı>` — hash'li ad, `immutable` önbelleği güvenli kılar.

    Aynı adla yeni bir görsel yüklenirse adres de değişir; Cloudflare/tarayıcıda bir
    yıllık önbellekte duran eski görsel asla "güncel" sanılmaz.
    """
    uzanti = os.path.splitext(filename)[1].lower().lstrip(".") or "jpg"
    if uzanti == "jpeg":
        uzanti = "jpg"
    h = hashlib.sha256()
    dosya = instance.cover_image
    try:
        dosya.seek(0)
        for parca in dosya.chunks():
            h.update(parca)
        dosya.seek(0)
    except Exception:  # noqa: BLE001 — okunamazsa rastgele değil, zaman damgalı ad
        h.update(f"{filename}:{timezone.now().timestamp()}".encode())
    return f"blog/{h.hexdigest()[:24]}.{uzanti}"


class BlogCategory(models.Model):
    name = models.CharField("ad", max_length=80)
    slug = models.SlugField("slug", max_length=80, unique=True,
                            help_text="ASCII, küçük harf, tireli. URL'de kullanılır.")
    order = models.PositiveSmallIntegerField("sıra", default=0,
                                             help_text="Menü sırası (küçük önce).")

    class Meta:
        verbose_name = "Blog kategorisi"
        verbose_name_plural = "Blog kategorileri"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = turkce_slug(self.name, 80)
        super().save(*args, **kwargs)


class BlogPostQuerySet(models.QuerySet):
    def yayinda(self, simdi=None):
        """Yayımlanmış yazılar — tüm uçların ortak tanımı."""
        return self.filter(status=BlogPost.Status.PUBLISHED,
                           published_at__lte=simdi or timezone.now())


class BlogPost(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Taslak"
        PUBLISHED = "published", "Yayımlandı"

    title = models.CharField("başlık", max_length=160)
    slug = models.SlugField(
        "slug", max_length=180, unique=True, blank=True,
        help_text="Boş bırakılırsa başlıktan üretilir. ⚠️ Yayımlandıktan sonra "
                  "DEĞİŞTİRİLEMEZ — yazının adresidir.",
    )
    summary = models.CharField("özet", max_length=300,
                               help_text="Liste kartında ve meta açıklamada görünür.")
    content = models.TextField(
        "içerik", help_text="Markdown. Ham HTML (script, iframe, on* öznitelikleri) "
                            "kabul edilmez.")
    cover_image = models.ImageField(
        "kapak görseli", upload_to=kapak_yolu, null=True, blank=True,
        validators=[FileExtensionValidator(["jpg", "jpeg", "png", "webp"]),
                    kapak_dogrula],
        help_text="Önerilen 1200×630. JPEG/PNG/WebP, en fazla 2 MB.",
    )
    cover_alt = models.CharField("kapak alt metni", max_length=200, blank=True,
                                 help_text="Kapak varsa zorunlu (erişilebilirlik + SEO).")
    category = models.ForeignKey(
        BlogCategory, on_delete=models.PROTECT, null=True, blank=True,
        related_name="yazilar", verbose_name="kategori",
    )
    tags = models.JSONField("etiketler", default=list, blank=True)
    author_name = models.CharField("yazar", max_length=100, default="İhale Takip")
    status = models.CharField("durum", max_length=12, choices=Status.choices,
                              default=Status.DRAFT, db_index=True)
    published_at = models.DateTimeField(
        "yayın tarihi", null=True, blank=True, db_index=True,
        help_text="İlk yayımlanışta otomatik dolar. İleri tarih = zamanlanmış yazı.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    seo_title = models.CharField("SEO başlığı", max_length=60, blank=True,
                                 help_text="Boşsa başlık kullanılır.")
    seo_description = models.CharField("SEO açıklaması", max_length=155, blank=True,
                                       help_text="Boşsa özet kullanılır.")
    reading_minutes = models.PositiveSmallIntegerField("okuma süresi (dk)", default=1,
                                                       editable=False)
    # Türkçe-duyarsız arama/etiket sütunları (bkz. CLAUDE.md "Türkçe arama
    # normalizasyonu") — `icontains` İ/ı'da yanılır.
    arama_norm = models.TextField(blank=True, editable=False)
    etiketler_norm = models.CharField(max_length=600, blank=True, editable=False)

    objects = BlogPostQuerySet.as_manager()

    class Meta:
        verbose_name = "Blog yazısı"
        verbose_name_plural = "Blog yazıları"
        ordering = ["-published_at", "-pk"]
        indexes = [models.Index(fields=["status", "-published_at"],
                                name="blog_yayin_idx")]

    def __str__(self):
        return self.title

    @classmethod
    def from_db(cls, db, field_names, values):
        obj = super().from_db(db, field_names, values)
        # Kaydetmeden önceki hâl: slug kilidi ve "yayından kaldırıldı mı" kararı
        # (revalidate) buna bakar.
        obj._onceki = {"status": obj.__dict__.get("status"),
                       "published_at": obj.__dict__.get("published_at"),
                       "slug": obj.__dict__.get("slug")}
        return obj

    # ── yardımcılar ──
    def yayinda_mi(self, simdi=None) -> bool:
        return (self.status == self.Status.PUBLISHED and self.published_at is not None
                and self.published_at <= (simdi or timezone.now()))

    def onceden_yayindaydi(self, simdi=None) -> bool:
        o = getattr(self, "_onceki", None)
        return bool(o and o["status"] == self.Status.PUBLISHED and o["published_at"]
                    and o["published_at"] <= (simdi or timezone.now()))

    @staticmethod
    def etiketleri_temizle(ham) -> list[str]:
        if isinstance(ham, str):
            ham = ham.split(",")
        sonuc, gorulen = [], set()
        for e in ham or []:
            e = " ".join(str(e).split())[:AZAMI_ETIKET_UZUNLUK]
            if e and normalize_tr(e) not in gorulen:
                gorulen.add(normalize_tr(e))
                sonuc.append(e)
        return sonuc

    def clean(self):
        hatalar = {}
        if self.cover_image and not (self.cover_alt or "").strip():
            hatalar["cover_alt"] = "Kapak görseli için alt metin zorunludur."
        if self.content and _YASAK_HTML.search(self.content):
            hatalar["content"] = ("İçerikte izin verilmeyen HTML var (script, iframe, "
                                  "on* öznitelikleri ya da javascript: bağlantısı).")
        if len(self.etiketleri_temizle(self.tags)) > AZAMI_ETIKET:
            hatalar["tags"] = f"En fazla {AZAMI_ETIKET} etiket girilebilir."
        o = getattr(self, "_onceki", None)
        if (o and self.onceden_yayindaydi() and self.slug and o["slug"]
                and self.slug != o["slug"]):
            hatalar["slug"] = "Yayımlanmış bir yazının adresi (slug) değiştirilemez."
        if hatalar:
            raise ValidationError(hatalar)

    def _benzersiz_slug(self, aday: str) -> str:
        aday = aday or "yazi"
        slug, n = aday, 2
        while BlogPost.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            ek = f"-{n}"
            slug = f"{aday[:180 - len(ek)]}{ek}"
            n += 1
        return slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._benzersiz_slug(turkce_slug(self.title))
        self.tags = self.etiketleri_temizle(self.tags)[:AZAMI_ETIKET]
        if self.status == self.Status.PUBLISHED and self.published_at is None:
            self.published_at = timezone.now()
        kelime = len(re.findall(r"\w+", self.content or ""))
        self.reading_minutes = max(1, math.ceil(kelime / DAKIKADA_KELIME))
        self.arama_norm = normalize_tr(f"{self.title} {self.summary}")
        self.etiketler_norm = (
            "|" + "|".join(normalize_tr(t) for t in self.tags) + "|" if self.tags else "")
        super().save(*args, **kwargs)
