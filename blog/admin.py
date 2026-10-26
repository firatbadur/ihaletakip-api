"""Blog admin — yazılar yalnızca buradan yazılıp yayımlanır."""
from django import forms
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html

from .models import AZAMI_ETIKET, BlogCategory, BlogPost, medya_yolu


@admin.register(BlogCategory)
class BlogCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "order", "yazi_sayisi")
    list_editable = ("order",)
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)

    @admin.display(description="yayındaki yazı")
    def yazi_sayisi(self, obj):
        return obj.yazilar.yayinda().count()


class BlogPostForm(forms.ModelForm):
    # ⚠️ JSONField'ın varsayılan widget'ı `["a","b"]` yazdırırdı; editör için virgüllü
    # metin. Temizlik/tekilleştirme modelde (`etiketleri_temizle`) — tek kaynak.
    tags = forms.CharField(
        label="etiketler", required=False,
        help_text=f"Virgülle ayırın, en fazla {AZAMI_ETIKET}. Örn: eşik değer, 4734",
        widget=forms.TextInput(attrs={"style": "width: 40em"}),
    )

    class Meta:
        model = BlogPost
        fields = "__all__"
        widgets = {
            "summary": forms.Textarea(attrs={"rows": 3}),
            "content": forms.Textarea(attrs={"rows": 30, "style": "font-family: monospace"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.tags:
            self.initial["tags"] = ", ".join(self.instance.tags)

    def clean_tags(self):
        return BlogPost.etiketleri_temizle(self.cleaned_data.get("tags") or "")


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    form = BlogPostForm
    list_display = ("title", "category", "durum", "published_at", "updated_at")
    list_filter = ("status", "category")
    search_fields = ("title", "summary")
    date_hierarchy = "published_at"
    readonly_fields = ("reading_minutes", "created_at", "updated_at", "kapak_onizleme")
    fieldsets = (
        (None, {"fields": ("title", "slug", "summary", "content")}),
        ("Kapak", {"fields": ("cover_image", "kapak_onizleme", "cover_alt")}),
        ("Sınıflandırma", {"fields": ("category", "tags", "author_name")}),
        ("Yayın", {
            "fields": ("status", "published_at"),
            "description": "Yayın tarihi boşsa ilk yayımlanışta şimdiki an yazılır. "
                           "İleri bir tarih verilirse yazı o ana kadar görünmez.",
        }),
        ("SEO", {"fields": ("seo_title", "seo_description"), "classes": ("collapse",)}),
        ("Bilgi", {"fields": ("reading_minutes", "created_at", "updated_at"),
                   "classes": ("collapse",)}),
    )

    def _yayinlanmis(self, obj):
        return bool(obj and obj.onceden_yayindaydi())

    def get_readonly_fields(self, request, obj=None):
        alanlar = list(super().get_readonly_fields(request, obj))
        # ⚠️ Yayımlanmış yazının slug'ı URL'dir; değişirse paylaşılmış bağlantılar ve
        # Google dizini kırılır. Model `clean()` de aynı kuralı uygular.
        if self._yayinlanmis(obj):
            alanlar.append("slug")
        return alanlar

    def get_prepopulated_fields(self, request, obj=None):
        # Salt okunur alan prepopulate edilemez (form'da yok → KeyError).
        return {} if self._yayinlanmis(obj) else {"slug": ("title",)}

    @admin.display(description="durum", ordering="status")
    def durum(self, obj):
        if obj.status != BlogPost.Status.PUBLISHED:
            return "Taslak"
        if obj.published_at and obj.published_at > timezone.now():
            return "Zamanlandı"
        return "Yayında"

    @admin.display(description="önizleme")
    def kapak_onizleme(self, obj):
        if not obj or not obj.cover_image:
            return "—"
        return format_html('<img src="{}" style="max-width:320px;max-height:180px">',
                           medya_yolu(obj.cover_image.name))
