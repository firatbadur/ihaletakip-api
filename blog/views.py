"""Blog okuma uçları — /api/v1/blog/... (herkese açık, yalnız yayımlanmış yazılar).

Yazma yalnızca Django admin'dendir. "Yayımlanmış" tanımı `BlogPost.objects.yayinda()`.
"""
from django.db.models import Count, Q
from django.utils import timezone
from django.views.static import serve
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
)
from rest_framework import permissions
from rest_framework.views import APIView

from core.response import api_response
from ekap.utils import normalize_tr

from .models import BlogCategory, BlogPost, mutlak_medya_url

VARSAYILAN_BOYUT = 12
AZAMI_BOYUT = 50
ILGILI_SAYISI = 3


def _iso(dt):
    # Gerçek an + ofset (`+03:00`); web sitemap'te `lastmod` olarak kullanıyor.
    return timezone.localtime(dt).isoformat() if dt else None


def yazi_satiri(p: BlogPost) -> dict:
    """Liste satırı — ⚠️ `content` BİLEREK yok (liste hafif kalsın)."""
    return {
        "slug": p.slug,
        "title": p.title,
        "summary": p.summary,
        "cover_image": mutlak_medya_url(p.cover_image.name) if p.cover_image else None,
        "cover_alt": p.cover_alt or None,
        "category": ({"name": p.category.name, "slug": p.category.slug}
                     if p.category_id else None),
        "tags": list(p.tags or []),
        "author_name": p.author_name,
        "published_at": _iso(p.published_at),
        "updated_at": _iso(p.updated_at),
        "reading_minutes": p.reading_minutes,
    }


def _sayi(deger, varsayilan, *, en_az=1, en_cok=None):
    try:
        n = int(deger)
    except (TypeError, ValueError):
        return varsayilan
    n = max(en_az, n)
    return min(en_cok, n) if en_cok else n


_LISTE_ORNEK = {
    "list": [{
        "slug": "4734-sayili-kanunda-esik-degerler-2027",
        "title": "4734 Sayılı Kanunda 2027 Eşik Değerleri",
        "summary": "Yeni yılda geçerli eşik değerler ve parasal limitler …",
        "cover_image": "https://ihale-takip.envisoft.com.tr/media/blog/3f9c1a2b.jpg",
        "cover_alt": "Hesap makinesi ve ihale dosyası",
        "category": {"name": "Mevzuat", "slug": "mevzuat"},
        "tags": ["eşik değer", "4734"],
        "author_name": "İhale Takip",
        "published_at": "2027-02-01T09:00:00+03:00",
        "updated_at": "2027-02-01T09:00:00+03:00",
        "reading_minutes": 4,
    }],
    "totalCount": 37,
    "page": 1,
}


@extend_schema(
    tags=["blog"],
    auth=[],
    operation_id="blog_posts_list",
    summary="Blog yazıları",
    description=(
        "Yayımlanmış yazılar, en yeni önce (`published_at` azalan). Taslak ve ileri "
        "tarihli (zamanlanmış) yazılar dönmez.\n\n"
        "Satırlarda **`content` yoktur** — tam metin için detay ucunu kullanın."
    ),
    parameters=[
        OpenApiParameter("page", int, default=1, description="Sayfa (1'den başlar)."),
        OpenApiParameter("page_size", int, default=VARSAYILAN_BOYUT,
                         description=f"Sayfa boyutu, en çok {AZAMI_BOYUT}."),
        OpenApiParameter("kategori", str, description="Kategori slug'ı.",
                         examples=[OpenApiExample("Mevzuat", value="mevzuat")]),
        OpenApiParameter("etiket", str, description="Etiket (Türkçe karakter duyarsız).",
                         examples=[OpenApiExample("Etiket", value="eşik değer")]),
        OpenApiParameter("q", str, description="Başlık + özet içinde arama; her kelime "
                                               "geçmeli (Türkçe karakter duyarsız)."),
    ],
    responses={200: OpenApiTypes.OBJECT},
    examples=[OpenApiExample("Liste", value=_LISTE_ORNEK, response_only=True)],
)
class BlogPostListView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        qp = request.query_params
        qs = BlogPost.objects.yayinda().select_related("category").defer("content")
        if kategori := (qp.get("kategori") or "").strip():
            qs = qs.filter(category__slug=kategori)
        if etiket := normalize_tr(qp.get("etiket")):
            qs = qs.filter(etiketler_norm__contains=f"|{etiket}|")
        # Kelime bazlı VE: "şartname kılavuzu" araya kelime girse de bulsun.
        for kelime in normalize_tr(qp.get("q")).split()[:8]:
            qs = qs.filter(arama_norm__contains=kelime)

        page = _sayi(qp.get("page"), 1)
        boyut = _sayi(qp.get("page_size"), VARSAYILAN_BOYUT, en_cok=AZAMI_BOYUT)
        toplam = qs.count()  # tablo küçük (yüzler) → önbellek gerekmez
        bas = (page - 1) * boyut
        satirlar = [yazi_satiri(p) for p in qs.order_by("-published_at", "-pk")[bas:bas + boyut]]
        return api_response(data={"list": satirlar, "totalCount": toplam, "page": page})


@extend_schema(
    tags=["blog"],
    auth=[],
    operation_id="blog_post_detail",
    summary="Blog yazısı detayı",
    description=(
        "Tam yazı: liste satırının alanları + `content` (**Markdown**), `seo_title`, "
        "`seo_description` (boşsa başlık/özet ile doldurulur) ve `related` (aynı "
        "kategoriden en çok 3 yazı).\n\n"
        "Yazı yoksa, taslaksa ya da yayın tarihi gelmemişse **404**."
    ),
    parameters=[OpenApiParameter(
        "slug", str, location=OpenApiParameter.PATH, required=True,
        examples=[OpenApiExample("Slug", value="4734-sayili-kanunda-esik-degerler-2027")],
    )],
    responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
)
class BlogPostDetailView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, slug):
        p = BlogPost.objects.yayinda().select_related("category").filter(slug=slug).first()
        if p is None:
            return api_response(message="Yazı bulunamadı.", success=False, status=404)
        veri = yazi_satiri(p)
        veri.update({
            "content": p.content,
            "seo_title": p.seo_title or p.title,
            "seo_description": p.seo_description or p.summary,
            "related": [],
        })
        if p.category_id:
            ilgili = (BlogPost.objects.yayinda().select_related("category").defer("content")
                      .filter(category_id=p.category_id).exclude(pk=p.pk)
                      .order_by("-published_at", "-pk")[:ILGILI_SAYISI])
            veri["related"] = [yazi_satiri(x) for x in ilgili]
        return api_response(data=veri)


@extend_schema(
    tags=["blog"],
    auth=[],
    operation_id="blog_categories",
    summary="Blog kategorileri",
    description="`order`'a göre sıralı. Yalnızca en az bir yayımlanmış yazısı olan "
                "kategoriler döner; `post_count` yayımlanmış yazı sayısıdır.",
    responses={200: OpenApiTypes.OBJECT},
    examples=[OpenApiExample(
        "Kategoriler", response_only=True,
        value=[{"name": "Mevzuat", "slug": "mevzuat", "post_count": 12}],
    )],
)
class BlogCategoryListView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        simdi = timezone.now()
        yayinda = Q(yazilar__status=BlogPost.Status.PUBLISHED,
                    yazilar__published_at__lte=simdi)
        qs = (BlogCategory.objects.annotate(post_count=Count("yazilar", filter=yayinda))
              .filter(post_count__gt=0).order_by("order", "name"))
        return api_response(data=[
            {"name": c.name, "slug": c.slug, "post_count": c.post_count} for c in qs
        ])


# Hash'li dosya adı (bkz. `models.kapak_yolu`) → içerik adla birlikte değişir.
_UZUN_ONBELLEK = "public, max-age=31536000, immutable"


def blog_medya(request, yol):
    """`/media/blog/<yol>` — kapak görselleri.

    ⚠️ Üretimde medyayı başka kimse sunmuyor (nginx `location /` her şeyi Django'ya
    verir, whitenoise yalnız statik). Görseller küçük ve Cloudflare `immutable`
    başlığıyla önbelleklediği için gunicorn'a düşen istek ilk görüntülemeyle sınırlı.
    ⚠️ Yalnızca `blog/` altı sunulur; `serve` yol geçişini (`../`) `safe_join` ile reddeder.
    """
    from django.conf import settings

    cevap = serve(request, f"blog/{yol}", document_root=settings.MEDIA_ROOT)
    if cevap.status_code == 200:
        cevap["Cache-Control"] = _UZUN_ONBELLEK
    return cevap
