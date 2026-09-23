"""ekap admin — ihale verisi ve senkron gözlemi."""
from django.contrib import admin
from django.db.models import Prefetch
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .constants import SEKTORLER
from .models import (
    Announcement,
    Authority,
    City,
    Contract,
    Contractor,
    ContractorAlias,
    ContractorMembership,
    ContractSection,
    Keyword,
    KeywordBatch,
    OkasCode,
    OkasItem,
    SyncCheckpoint,
    SyncRun,
    Tender,
    TenderKeyword,
    TenderNamePattern,
    TenderDate,
)


def _kw_ad(kw):
    """Gösterim metni — `metin_ham` boşsa kanonik `metin`e düşer."""
    return kw.metin_ham or kw.metin


def _kw_ihale_url(keyword_id):
    """O keyword'e sahip ihalelerin changelist bağlantısı (bkz. `KeywordFilter`)."""
    return f"{reverse('admin:ekap_tender_changelist')}?{KeywordFilter.parameter_name}={keyword_id}"


class SektorFilter(admin.SimpleListFilter):
    """
    Sektör filtresi — seçenekler **sabit taksonomiden** gelir, DB'den değil.

    ⚠️ Django'nun varsayılanı (`list_filter = ["sektor"]` → `AllValuesFieldListFilter`)
    her sayfa açılışında o kolonda `SELECT DISTINCT` koşar; `ekap_tender` 1M,
    `ekap_contract` 1,4M satır. Taksonomi zaten kapalı ve koddan biliniyor → sorguya
    gerek yok.
    ⚠️ Ayrıca varsayılan filtre ham kodu basar (`saglik_tibbi_malzeme`); burada
    Türkçe ad gösterilir.
    ⚠️ `parameter_name` **"sektor" olmalı**: sektör özeti ekranı bu ada göre
    `?sektor=<kod>` bağlantısı üretiyor.
    """

    title = "sektör"
    parameter_name = "sektor"
    # Boş sektörü de seçilebilir yapan sentetik değer — `?sektor=` (boş) Django
    # tarafından "filtre yok" sayıldığı için ayrı bir işaret gerekiyor.
    BOS = "__bos__"

    def lookups(self, request, model_admin):
        return [(self.BOS, "(sektörsüz)")] + [(k, v) for k, v in SEKTORLER.items()]

    def queryset(self, request, queryset):
        deger = self.value()
        if not deger:
            return queryset
        if deger == self.BOS:
            return queryset.filter(sektor="")
        return queryset.filter(sektor=deger)


class KeywordFilter(admin.SimpleListFilter):
    """
    "Bu anahtar kelimeye sahip ihaleler" filtresi.

    ⚠️ **Seçenek listesi ÜRETİLMEZ.** 125 bin keyword var; `AllValuesFieldListFilter`
    ya da dolu bir `lookups()` kenar çubuğuna 125 bin satır basmaya kalkardı. Bu filtre
    yalnızca **bağlantıyla** kullanılır (keyword listesinden ya da ihale satırındaki
    keyword'e tıklayarak) ve seçili değilken kendini hiç göstermez: `lookups()` boş
    dönünce Django `has_output()` üzerinden filtreyi gizler.
    ⚠️ Seçiliyken **tek** seçenek döndürülür — kenar çubuğunda "Tümü / <keyword>"
    görünür, yani filtreyi kaldırma yolu var. Boş liste döndürmek filtreyi gizler ve
    kullanıcıyı filtreden çıkamaz hâle sokardı.
    ⚠️ Tek keyword'e filtrelendiği için JOIN satır çoğaltmaz → `.distinct()` gerekmez
    (bkz. CLAUDE.md → "`.distinct()` KULLANMAYIN").
    """

    title = "anahtar kelime"
    parameter_name = "kw"

    def lookups(self, request, model_admin):
        deger = self.value()
        if not deger:
            return []
        kw = Keyword.objects.filter(pk=deger).only("metin", "metin_ham").first()
        return [(deger, _kw_ad(kw) if kw else f"#{deger}")]

    def queryset(self, request, queryset):
        deger = self.value()
        if not deger:
            return queryset
        return queryset.filter(keyword_baglari__keyword_id=deger)


@admin.display(description="Sektör", ordering="sektor")
def sektor_adi(obj):
    """Ham kod yerine Türkçe ad (`saglik_tibbi_malzeme` → `Tıbbi Sarf Malzeme`)."""
    if not obj.sektor:
        return "—"
    return SEKTORLER.get(obj.sektor, obj.sektor)


class TenderDateInline(admin.TabularInline):
    model = TenderDate
    extra = 0


class OkasItemInline(admin.TabularInline):
    model = OkasItem
    extra = 0


class AnnouncementInline(admin.TabularInline):
    model = Announcement
    extra = 0
    fields = ["ilan_tip", "ilan_tarihi", "baslik", "istekli_adi"]


class ContractInline(admin.TabularInline):
    model = Contract
    extra = 0
    fields = ["yuklenici_adi", "sozlesme_bedeli", "yaklasik_maliyet", "sozlesme_tarih"]


class TenderKeywordInline(admin.TabularInline):
    """
    İhalenin AI keyword'leri — **salt okunur**.

    ⚠️ Satırlar makine üretimi: kaynak `TenderNamePattern.keyword_ids` ve yayma görevi
    (`propagate_tender_keywords`). Elle eklenen/silinen bir bağ kalıp sözlüğüyle
    sessizce ayrışır ve kimse fark etmez → ekleme/değiştirme/silme kapalı.
    Düzeltme gerekiyorsa kalıp sözlüğünden yapılmalı.
    """

    model = TenderKeyword
    extra = 0
    fields = ["keyword_baglantisi", "derece", "df", "pasif"]
    readonly_fields = fields
    can_delete = False
    verbose_name_plural = "Anahtar kelimeler (AI)"

    def get_queryset(self, request):
        # ⚠️ Sıralama `kullanim_sayisi` ARTAN: benzerlik sorgusunun (`probe_keywordleri`)
        # kullandığı sıra budur — en düşük df = en ayırt edici. Ekranda da aynı sırayı
        # görmek, "neden bu ihale şuna benzer çıktı" sorusunu cevaplanabilir kılar.
        return (super().get_queryset(request)
                .select_related("keyword")
                .order_by("keyword__kullanim_sayisi"))

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Anahtar kelime")
    def keyword_baglantisi(self, obj):
        return format_html('<a href="{}">{}</a>',
                           _kw_ihale_url(obj.keyword_id), _kw_ad(obj.keyword))

    @admin.display(description="Derece")
    def derece(self, obj):
        return obj.keyword.derece

    @admin.display(description="Kaç ihalede (df)")
    def df(self, obj):
        return f"{obj.keyword.kullanim_sayisi:,}".replace(",", ".")

    @admin.display(description="Pasif", boolean=True)
    def pasif(self, obj):
        return obj.keyword.pasif


@admin.register(Tender)
class TenderAdmin(admin.ModelAdmin):
    list_display = ["ikn", "ihale_adi_kisa", "keywordler", "ihale_il_adi", "ihale_tip",
                    sektor_adi, "ihale_durum", "detail_synced_at", "sync_status"]
    list_filter = ["ihale_tip", "ihale_durum", "sync_status", "e_ihale", SektorFilter,
                   KeywordFilter]
    search_fields = ["ikn", "ekap_id", "ihale_adi", "idare_adi"]
    readonly_fields = ["created_at", "updated_at", "list_synced_at", "detail_synced_at", "detail_raw", "list_raw"]
    inlines = [TenderKeywordInline, TenderDateInline, OkasItemInline,
               AnnouncementInline, ContractInline]
    date_hierarchy = "ihale_tarihi"
    # ⚠️ `ekap_tender`'da `COUNT(*)` YASAK (bkz. CLAUDE.md → Anasayfa panosu: pano bu
    # yüzden `reltuples` kullanıyor). Django varsayılanı filtre uygulandığında
    # filtrelenmiş sayımın YANINDA bir de **filtresiz** `COUNT(*)` koşar ("N / toplam M")
    # — 1M satırda bedava değil ve hiçbir teşhise yaramıyor.
    show_full_result_count = False

    def get_queryset(self, request):
        # ⚠️ Prefetch OLMADAN `keywordler` kolonu satır başına bir sorgu atar (sayfa
        # başına 50 ekstra sorgu — belgeli "sessiz N+1" tuzağının aynısı).
        # Sayfalama slice'tan SONRA çalıştığı için yalnızca görünen satırlar çekilir.
        return super().get_queryset(request).prefetch_related(
            Prefetch("keyword_baglari",
                     queryset=(TenderKeyword.objects
                               .select_related("keyword")
                               .order_by("keyword__kullanim_sayisi"))))

    @admin.display(description="İhale Adı")
    def ihale_adi_kisa(self, obj):
        return (obj.ihale_adi or "")[:70]

    @admin.display(description="Anahtar kelimeler")
    def keywordler(self, obj):
        """
        İlk 4 keyword (en ayırt ediciden başlayarak) + kalanın sayısı.

        ⚠️ Boş olması arıza DEĞİL: ihalelerin ~%4,6'sında keyword yok — adı 2 anlamlı
        token'dan kısa (`kalip_hash` boş) ya da model "bu ad hiçbir şey söylemiyor"
        demiş (`durum="skipped"`). Uydurulmamış olması doğrudur.
        """
        baglar = list(obj.keyword_baglari.all())
        if not baglar:
            return format_html('<span style="opacity:.5">—</span>')
        govde = format_html_join(
            format_html(' <span style="opacity:.4">·</span> '),
            '<a href="{}">{}</a>',
            ((_kw_ihale_url(b.keyword_id), _kw_ad(b.keyword)) for b in baglar[:4]))
        if len(baglar) > 4:
            govde = format_html('{} <span style="opacity:.5">+{}</span>',
                                govde, len(baglar) - 4)
        return govde


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = [
        "tender", "yuklenici_adi", "sozlesme_bedeli_num", "yaklasik_maliyet_num",
        "indirim_orani", "sozlesme_tarihi", "yuklenici",
    ]
    list_filter = ["yaklasik_maliyet_kaynak", "ihale_tip", SektorFilter]
    search_fields = ["tender__ikn", "yuklenici_adi", "ekap_sozlesme_id"]
    raw_id_fields = ["tender", "yuklenici"]
    date_hierarchy = "sozlesme_tarihi"


class ContractorAliasInline(admin.TabularInline):
    model = ContractorAlias
    extra = 0
    fields = ["ham_ad", "kaynak", "son_gorulme"]
    readonly_fields = ["son_gorulme"]


class ContractorMembershipInline(admin.TabularInline):
    """Ortak girişimin üyeleri."""

    model = ContractorMembership
    fk_name = "ortak_girisim"
    extra = 0
    fields = ["uye", "sira", "pilot", "guven", "kaynak_metin"]
    raw_id_fields = ["uye"]


@admin.register(Contractor)
class ContractorAdmin(admin.ModelAdmin):
    list_display = [
        "kanonik_ad_kisa", "kind", "sozlesme_sayisi", "ihale_sayisi", "idare_sayisi",
        "toplam_sozlesme_bedeli", "ortalama_indirim_orani", "il_adi", "son_sozlesme_tarihi",
    ]
    # `uyeleri_cozumlendi=False` → ortak girişim üyeleri güvenle ayrıştırılamadı,
    # elle inceleme bekliyor (bkz. contractors.split_joint_venture yazma politikası).
    list_filter = ["kind", "uyeleri_cozumlendi", "tuzel_tip"]
    search_fields = ["kanonik_ad", "kanonik_anahtar", "arama_norm", "aliaslar__ham_ad"]
    readonly_fields = [
        "kanonik_anahtar", "arama_norm", "sozlesme_sayisi", "ihale_sayisi", "idare_sayisi",
        "toplam_sozlesme_bedeli", "ilk_sozlesme_tarihi", "son_sozlesme_tarihi",
        "ortalama_indirim_orani", "indirim_orani_ornek_sayisi", "uye_sayisi",
        "ortak_girisim_sayisi", "agrega_guncelleme", "ilk_gorulme", "updated_at",
    ]
    inlines = [ContractorAliasInline, ContractorMembershipInline]

    @admin.display(description="Yüklenici")
    def kanonik_ad_kisa(self, obj):
        return (obj.kanonik_ad or "")[:60]


@admin.register(ContractorAlias)
class ContractorAliasAdmin(admin.ModelAdmin):
    list_display = ["ham_ad", "contractor", "kaynak", "son_gorulme"]
    list_filter = ["kaynak"]
    search_fields = ["ham_ad", "ham_ad_norm", "contractor__kanonik_ad"]
    raw_id_fields = ["contractor"]


@admin.register(ContractorMembership)
class ContractorMembershipAdmin(admin.ModelAdmin):
    list_display = ["ortak_girisim", "uye", "sira", "pilot", "guven"]
    list_filter = ["guven", "pilot"]
    search_fields = ["ortak_girisim__kanonik_ad", "uye__kanonik_ad"]
    raw_id_fields = ["ortak_girisim", "uye"]


@admin.register(OkasCode)
class OkasCodeAdmin(admin.ModelAdmin):
    list_display = ["kod", "adi"]
    search_fields = ["kod", "adi", "adi_eng"]


@admin.register(Authority)
class AuthorityAdmin(admin.ModelAdmin):
    list_display = ["detsis_no", "ad", "idare_id", "parent_detsis", "has_items", "seviye"]
    search_fields = ["detsis_no", "ad", "idare_id"]
    list_filter = ["has_items", "seviye"]


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ["ekap_il_id", "plaka", "ad", "is_big_city"]
    search_fields = ["ad"]
    list_filter = ["is_big_city"]


@admin.register(SyncCheckpoint)
class SyncCheckpointAdmin(admin.ModelAdmin):
    list_display = ["name", "cursor_skip", "oldest_date", "newest_date", "done", "updated_at"]


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ["task", "started_at", "finished_at", "status", "items", "errors"]
    list_filter = ["task", "status"]
    readonly_fields = ["task", "started_at", "finished_at", "status", "items", "errors", "note"]


admin.site.register(ContractSection)


# ── Anahtar kelime katmanı ──────────────────────────────

@admin.register(Keyword)
class KeywordAdmin(admin.ModelAdmin):
    list_display = ["metin", "metin_ham", "derece", "kullanim_sayisi", "pasif",
                    "ihaleler"]
    list_filter = ["derece", "pasif"]
    search_fields = ["metin", "metin_ham"]
    readonly_fields = ["kullanim_sayisi", "pasif", "created_at"]
    ordering = ["-kullanim_sayisi"]

    @admin.display(description="İhaleler")
    def ihaleler(self, obj):
        """
        ⚠️ `kullanim_sayisi` (df) **yayma anındaki** sayımdır (`refresh_keyword_df`
        günceller), bağlantının açacağı listenin canlı sayısı değil. İkisi arasında
        küçük bir fark görülmesi normaldir.
        """
        return format_html('<a href="{}">{} ihale</a>', _kw_ihale_url(obj.pk),
                           f"{obj.kullanim_sayisi:,}".replace(",", "."))


@admin.register(KeywordBatch)
class KeywordBatchAdmin(admin.ModelAdmin):
    """Batch izleme — ⚠️ maliyet takibinin tek yeri."""

    list_display = ["batch_id", "durum", "kalip_sayisi", "basarili", "hatali",
                    "maliyet", "created_at", "islendi_at"]
    list_filter = ["durum", "model"]
    search_fields = ["batch_id"]
    readonly_fields = [f.name for f in KeywordBatch._meta.fields]

    @admin.display(description="Maliyet ($)")
    def maliyet(self, obj):
        from django.conf import settings
        return round((obj.input_tokens * settings.KEYWORD_FIYAT_IN
                      + obj.output_tokens * settings.KEYWORD_FIYAT_OUT) / 1_000_000, 2)


@admin.register(TenderNamePattern)
class TenderNamePatternAdmin(admin.ModelAdmin):
    """
    Kalıp sözlüğü. ⚠️ `durum` filtresi teşhisin ana aracı:
    `pending` birikiyorsa dispatch çalışmıyor, `queued` birikiyorsa batch takılmış,
    `skipped` çoksa model "bu ad hiçbir şey söylemiyor" diyor (jenerik adlar).
    """

    list_display = ["kalip_kisa", "ihale_sayisi", "durum", sektor_adi, "guven",
                    "keyword_adet", "islendi_at"]
    list_filter = ["durum", SektorFilter, "model"]
    search_fields = ["kalip_norm", "ornek_ad", "kalip_hash"]
    readonly_fields = ["kalip_hash", "kalip_norm", "ornek_ad", "ihale_sayisi",
                       "keywordler", "keyword_ids", "guven", "batch", "model",
                       "islendi_at"]
    ordering = ["-ihale_sayisi"]

    @admin.display(description="Kalıp")
    def kalip_kisa(self, obj):
        return obj.kalip_norm[:70]

    @admin.display(description="Keyword")
    def keyword_adet(self, obj):
        return len(obj.keyword_ids or [])

    @admin.display(description="Anahtar kelimeler")
    def keywordler(self, obj):
        """
        `keyword_ids` ham id listesidir (`[12,489,…]`) ve ekranda hiçbir şey ifade
        etmiyordu. Burada metinlere çözülür; ham liste de altta duruyor (bir id
        çözülemezse — silinmiş keyword — fark edilebilsin diye).
        """
        idler = obj.keyword_ids or []
        if not idler:
            return "—"
        kelimeler = {k.pk: k for k in Keyword.objects.filter(pk__in=idler)
                     .only("metin", "metin_ham")}
        return format_html_join(
            format_html(' <span style="opacity:.4">·</span> '),
            '<a href="{}">{}</a>',
            ((_kw_ihale_url(i), _kw_ad(kelimeler[i]) if i in kelimeler else f"#{i} (yok)")
             for i in idler))
