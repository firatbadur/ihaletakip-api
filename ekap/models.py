"""
EKAP verisi modelleri.

Çekirdek `Tender` + çocuk tablolar (tarihler, OKAS kalemleri, ilanlar, sözleşmeler,
kısımlar) + lookup ingest (OkasCode, Authority) + City + senkron bookkeeping.
Alan envanteri mobil ekran tüketiminden çıkarıldı (bkz. plan dosyası).
"""
from django.contrib.postgres.indexes import GinIndex
from django.db import models


# ── Lookup: Şehir ──────────────────────────────────────
class City(models.Model):
    """İl — EKAP `ihaleIlIdList` `ekap_il_id`'yi bekler (plaka değil)."""

    ekap_il_id = models.IntegerField(unique=True, db_index=True)
    plaka = models.IntegerField()
    ad = models.CharField(max_length=100)
    is_big_city = models.BooleanField(default=False)

    class Meta:
        verbose_name = "İl"
        verbose_name_plural = "İller"
        ordering = ["ad"]

    def __str__(self):
        return self.ad


# ── Lookup ingest: OKAS & DETSIS kurum ─────────────────
class OkasCode(models.Model):
    """OKAS ihtiyaç kalemi kodu (EKAP GetAll'dan periyodik çekilir)."""

    kod = models.CharField(max_length=255, unique=True, db_index=True)
    adi = models.CharField(max_length=500, db_index=True)
    adi_eng = models.CharField(max_length=500, blank=True)
    # Türkçe+aksan-duyarsız arama (normalize_tr(adi)); senkronda doldurulur.
    adi_norm = models.CharField(max_length=500, blank=True, db_index=True)

    class Meta:
        verbose_name = "OKAS Kodu"
        verbose_name_plural = "OKAS Kodları"
        ordering = ["kod"]

    def __str__(self):
        return f"{self.kod} — {self.adi}"


class Authority(models.Model):
    """
    DETSIS kurum/idare **ağaç** düğümü (EKAP DetsisAgaci'ndan periyodik çekilir).

    EKAP `DetsisAgaci` bir lazy-tree'dir; tüm ağaç 2 istekle çekilir
    (kökler `parent=0`, tüm alt düğümler `parent>0`). Alan eşlemesi:
      - `detsis_no`      ← `detsisNo`  (ağaç anahtarı, **benzersiz**)
      - `parent_detsis`  ← `parentIdareKimlikKodu` (üst düğümün detsisNo'su; kök = "")
      - `idare_id`       ← `idareId`   (ihale filtre anahtarı = `Tender.idare_id`;
                                        dal/gruplama düğümlerinde boş)
      - `has_items`      ← `hasItems`  (çocuğu var mı → mobilde expand chevron'u)
      - `seviye`         ← `seviye`

    Not: ağaç bağı `detsis_no` ile kurulur ama ihale filtresi `idare_id` iler.
    Bir üst düğüm seçilince alt birimlerin ihaleleri de gelsin diye
    `apply_tender_filters` `idare_detsis`'i tüm alt `idare_id`'lere genişletir
    (bkz. `ekap.detsis_tree.descendant_idare_ids`).
    """

    detsis_no = models.CharField(max_length=64, unique=True, db_index=True)
    ad = models.CharField(max_length=500, db_index=True)
    # Türkçe+aksan-duyarsız arama (normalize_tr(ad)); senkronda doldurulur.
    ad_norm = models.CharField(max_length=500, blank=True, db_index=True)
    parent_detsis = models.CharField(max_length=64, blank=True, db_index=True)
    idare_id = models.CharField(max_length=64, blank=True, db_index=True)
    has_items = models.BooleanField(default=False)
    seviye = models.IntegerField(null=True, blank=True)

    class Meta:
        verbose_name = "Kurum (DETSIS)"
        verbose_name_plural = "Kurumlar (DETSIS)"
        ordering = ["ad"]

    def __str__(self):
        return self.ad


# ── Çekirdek: İhale ────────────────────────────────────
class Tender(models.Model):
    """EKAP ihalesi — liste + detay ortak çekirdek."""

    class SyncStatus(models.TextChoices):
        PENDING = "pending", "Bekliyor"
        OK = "ok", "Tamam"
        ERROR = "error", "Hata"

    # Kimlik
    ekap_id = models.CharField(max_length=255, unique=True, db_index=True)  # item.id
    ikn = models.CharField(max_length=100, unique=True, db_index=True)

    # Temel bilgiler
    ihale_adi = models.TextField(blank=True)
    idare_adi = models.CharField(max_length=500, blank=True)
    # Türkçe+aksan-duyarsız arama (normalize_tr); senkronda doldurulur. `q`, `ihale_adi`,
    # `idare_adi` filtreleri bu sütunlar üzerinden çalışır (Türkçe-i güvenli).
    ihale_adi_norm = models.TextField(blank=True)
    idare_adi_norm = models.CharField(max_length=500, blank=True)
    idare_id = models.CharField(max_length=255, blank=True, db_index=True)
    ihale_il_adi = models.CharField(max_length=100, blank=True)
    il_id = models.IntegerField(null=True, blank=True, db_index=True)
    ilce_adi = models.CharField(max_length=100, blank=True)

    # Tarihler (ham + parse)
    ihale_tarih_saat = models.CharField(max_length=255, blank=True)
    ihale_tarihi = models.DateTimeField(null=True, blank=True, db_index=True)
    ilan_tarihi = models.DateTimeField(null=True, blank=True, db_index=True)

    # Sınıflandırma
    ihale_tip = models.IntegerField(null=True, blank=True, db_index=True)  # 1-4
    ihale_tipi_aciklama = models.CharField(max_length=200, blank=True)
    ihale_usul = models.IntegerField(null=True, blank=True)  # 1-4
    ihale_usul_aciklama = models.CharField(max_length=200, blank=True)
    ihale_durum = models.IntegerField(null=True, blank=True, db_index=True)
    ihale_durum_aciklama = models.CharField(max_length=200, blank=True)
    ihale_kapsam_aciklama = models.CharField(max_length=200, blank=True)
    yasa_kapsami = models.IntegerField(null=True, blank=True, db_index=True)  # 1=4734,2=Dışı,3=İstisna

    # İhale özellikleri (ihaleOzellikList etiketleri: E_IHALE, KISMI_TEKLIF_VEREBILIR, ...)
    # Gelişmiş filtreler bu liste üzerinden çalışır (JSONField __contains). Detaydan doldurulur.
    ozellikler = models.JSONField(default=list, blank=True)

    # Bayraklar / sayılar
    e_ihale = models.BooleanField(default=False)
    dokuman_sayisi = models.IntegerField(default=0)
    ilan_var_mi = models.BooleanField(default=False)

    # Detay alanları
    isin_yapilacagi_yer = models.TextField(blank=True)
    ihale_yeri = models.TextField(blank=True)
    itirazen_sikayet_basvuru_bedeli = models.CharField(max_length=100, blank=True)
    ust_idare = models.CharField(max_length=500, blank=True)
    idare_telefon = models.CharField(max_length=100, blank=True)
    idare_fax = models.CharField(max_length=100, blank=True)

    # İptal
    iptal_tarihi = models.CharField(max_length=255, blank=True)
    iptal_nedeni = models.TextField(blank=True)
    iptal_madde = models.CharField(max_length=200, blank=True)

    # Senkron bookkeeping
    list_synced_at = models.DateTimeField(null=True, blank=True)
    detail_synced_at = models.DateTimeField(null=True, blank=True, db_index=True)
    sync_status = models.CharField(
        max_length=10, choices=SyncStatus.choices, default=SyncStatus.PENDING, db_index=True
    )
    sync_error = models.TextField(blank=True)
    detail_raw = models.JSONField(null=True, blank=True)
    list_raw = models.JSONField(null=True, blank=True)

    # Yüklenici/sonuç çözümlemesi (bkz. ekap/contractors.py, ekap/sonuc_ilani.py)
    contractors_synced_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # İhalenin TAMAMININ yaklaşık maliyeti — yalnızca Sonuç İlanı'ndan gelir
    yaklasik_maliyet_num = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True
    )
    # Sözleşmesi olup henüz Sonuç İlanı yayımlanmamış → detayı daha sık tazele
    sonuc_ilani_eksik = models.BooleanField(default=False, db_index=True)

    # ── Sözleşme özeti (denormalize) ──────────────────────────────────────────
    # `sync_contracts_from_raw` zaten bu satırı güncelliyor ve değerler elinde →
    # doldurmak BEDAVA (ek `detail_raw` okuması yok, `sync_contractors` süpürmesi
    # arşivi zaten geziyor).
    # Amaç: "sonuçlanmış mı / sözleşme bedeli şu aralıkta mı" filtreleri korelasyonlu
    # `Exists(Contract...)` alt sorgusu yerine **düz, indekslenebilir Tender kolonu**
    # olsun. `Contract`'a bağlı bir OR dalı, aynı tabloda olmadığı için diğer dalların
    # indekslerini de devre dışı bırakıyordu (bkz. CLAUDE.md → "OR'un HER dalı").
    # ⚠️ `db_index=True` BİLEREK YOK. Django onu `AddField` içinde düz `CREATE INDEX`
    # olarak üretir → 1M satırlık canlı tabloda ACCESS EXCLUSIVE kilidi. Bu kolonlar
    # ayrıca süpürme boyunca ~1M kez UPDATE edilecek; önce kurulan indeks şişerdi.
    # Sıra: kolon (bu migration) → doldurma → `AddIndexConcurrently` (Adım 2, filtreler
    # geldiğinde). Bkz. 0007_search_indexes ve migrations/_pg_ops.py.
    sozlesme_sayisi = models.IntegerField(default=0)
    toplam_sozlesme_bedeli = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True
    )

    # ── Bakanlık/üst kurum (idare.enUstIdare*) ────────────────────────────────
    # ⚠️ `ust_idare` bu bilgiyi TAŞIMIYOR: `ustIdare` doluysa (ör. "BAKAN
    # YARDIMCILIKLARI") kazanıyor ve gerçek bakanlık adı atılıyordu (bkz. sync.py).
    # `en_ust_idare_kod`, DETSIS alt ağacını `descendant_idare_ids()` ile on binlerce
    # `idare_id`'ye açmak yerine **tek indeksli eşitliğe** indirger.
    en_ust_idare_kod = models.CharField(max_length=16, blank=True)  # indeks: bkz. yukarı
    en_ust_idare_adi = models.CharField(max_length=500, blank=True)

    # ── Sinyal kolonları (detail_raw'dan türetilir — bkz. sync.apply_pro_fields) ──
    # EKAP'a hiç istek atılmaz; hepsi arşivdeki ham JSONB'den okunur.

    # Birincil OKAS = `ihtiyacKalemiOkasList[0].kodu`. ⚠️ `ihaleBilgi.okas` DEĞİL — o alan
    # aynı listenin `", ".join(koduAdi)` hâli, yeni bilgi taşımaz. Değeri denormalizasyon:
    # çok satırlı `OkasItem` yerine tek değerli, gruplanabilir kolon.
    # ⚠️ Kod uzunluğu SABİT DEĞİL (canlı veride 8 ve 9 hane bir arada) → max_length geniş.
    okas_ana_kod = models.CharField(max_length=16, blank=True)
    okas_ana_adi = models.CharField(max_length=500, blank=True)
    okas_bucket = models.CharField(max_length=4, blank=True)  # okas_ana_kod[:4], pazar grain'i
    # Dürüstlük: 2+ ise "birincil OKAS" bir yaklaşıklıktır, ihale birden çok kalem içeriyor.
    okas_kalem_sayisi = models.IntegerField(default=0)

    # Katılan istekli sayısı (`tebligatAlanIstekliList` uzunluğu).
    # ⚠️ Liste DEĞERLENDİRME SONRASI dolar: açık ihalede daima boş gelir. Boş liste
    # "sıfır istekli" DEĞİL "henüz bilinmiyor" demektir → 0 değil NULL yazılır.
    # `teklif_sayisi`den farkı: o yalnızca Sonuç İlanı ayrıştırılabilmişse dolu.
    istekli_sayisi = models.IntegerField(null=True, blank=True)

    # `islemlerKuralSeti` bayrakları — ÜÇ DEĞERLİ. NULL = "detay ayrıştırılmadı".
    # ⚠️ Çıkarımda `k.get(key, False)` KULLANMAYIN: bilinmeyeni "hayır"a çevirir ve
    # filtre `itiraz yok` derken aslında `bilgi yok` olanları da toplar.
    itirazen_sikayet_var = models.BooleanField(null=True, blank=True)
    idareye_sikayet_var = models.BooleanField(null=True, blank=True)
    sikayet_dilekce_var = models.BooleanField(null=True, blank=True)
    # Teklif stratejisi için kritik: en düşük fiyat tek başına kazandırmıyor demek.
    fiyat_disi_unsur_var = models.BooleanField(null=True, blank=True)
    e_eksiltme_yapilacak = models.BooleanField(null=True, blank=True)
    # ⚠️ Örneklenen 4 ihalenin DÖRDÜNDE de True çıktı → anlamı "düzeltme ilanı yayımlandı"
    # olmayabilir (belki "yayımlanabilir"). Ürün özelliği yapmadan önce prod'da dağılımını
    # ölçün; şimdilik yalnızca saklanıyor.
    duzeltme_ilani_var = models.BooleanField(null=True, blank=True)

    kismi_ihale = models.BooleanField(null=True, blank=True)
    ilansiz_mi = models.BooleanField(null=True, blank=True)

    # Tekrar eden ihale serisi anahtarı (bkz. ekap/series.py). Burada üretilir çünkü
    # arşivin `detail_raw`'ı BİR KEZ okunmalı; sonradan eklemek tek bir varchar için
    # 40 GB TOAST üzerinde ikinci bir haftalar süren gece taraması demektir.
    seri_anahtar = models.CharField(max_length=40, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "İhale"
        verbose_name_plural = "İhaleler"
        ordering = ["-ihale_tarihi"]
        indexes = [
            models.Index(fields=["ihale_tip", "ihale_durum"]),
            # ── Pro filtreleri (0015, prod EXPLAIN'iyle kanıtlandı) ──────────────
            # ⚠️ Sıralama kolonu bileşiğin **İKİNCİ** elemanı: baş kolon yapılsaydı
            # `ORDER BY tarih DESC LIMIT N` + seçici filtre tuzağının kendisi olurdu.
            # Eşitlik baş kolonda olunca dar bir aralık taranır ve sıralı çıktı bedava
            # gelir. Ölçüm öncesi: `okas_ana_kod` filtresi 11 GB'lık tabloyu seq scan
            # ediyordu (benchmark ucu 4,5 sn).
            models.Index(fields=["okas_ana_kod", "-ihale_tarihi"], name="ekap_tender_okas_tarih_idx"),
            models.Index(fields=["en_ust_idare_kod", "-ihale_tarihi"], name="ekap_tender_bakanlik_tarih_idx"),
            # Haftalık seri tespiti `GROUP BY seri_anahtar, idare_id` yapıyor; sıralı
            # indeks taraması diske taşan external merge sort'u (3×20 MB) kaldırır.
            models.Index(fields=["seri_anahtar", "idare_id"], name="ekap_tender_seri_idare_idx"),
            # NOT: `Index(fields=["-ilan_tarihi"])` KALDIRILDI — `ilan_tarihi` zaten
            # `db_index=True` (yukarıda) ve Postgres btree'yi geriye doğru tarayabildiği
            # için birebir kopyaydı. Backfill sürekli INSERT/UPDATE attığından iki kat
            # indeks yazma maliyeti ödeniyordu.
            # ── Metin araması (bkz. apply_tender_filters `q`/`ihale_adi`/`idare_adi`) ──
            # `__contains` → `LIKE '%…%'`; baştan joker olduğu için **düz B-tree işe
            # yaramaz**, trigram GIN şart. `0005_search_norm` bu sütunları eklerken
            # kardeş modellere (`Authority.ad_norm`, `OkasCode.adi_norm`) `db_index`
            # verip buraya vermemişti → 500k satırda her arama seq scan'di.
            # ⚠️ Trigram indeksi **en az 3 karakter** ister; 1-2 harflik sorgu yine
            # seq scan'e düşer (davranış doğru kalır, yalnızca yavaştır).
            GinIndex(
                fields=["ihale_adi_norm"],
                opclasses=["gin_trgm_ops"],
                name="ekap_tender_ihaleadi_trgm",
            ),
            GinIndex(
                fields=["idare_adi_norm"],
                opclasses=["gin_trgm_ops"],
                name="ekap_tender_idareadi_trgm",
            ),
            # ⚠️ İKN de trigram ister: `q` filtresi üç koşulu OR'lar ve Postgres BitmapOr'u
            # ancak **her dal** indekslenebiliyorsa kurar. İKN dalı indekssiz kalsaydı
            # yukarıdaki iki indeks de kullanılamaz, sorgu yine seq scan olurdu.
            # (`unique=True` btree'si `LIKE '%…%'` için işe yaramaz; `_like`
            # varchar_pattern_ops indeksi yalnızca `startswith`'i karşılar.)
            GinIndex(
                fields=["ikn"],
                opclasses=["gin_trgm_ops"],
                name="ekap_tender_ikn_trgm",
            ),
            # `ozellikler__contains=[tag]` → JSONB `@>`; jsonb_path_ops containment için
            # varsayılan opclass'tan küçük ve hızlıdır.
            GinIndex(
                fields=["ozellikler"],
                opclasses=["jsonb_path_ops"],
                name="ekap_tender_ozellik_gin",
            ),
            # ── Filtre + sıralama bileşikleri ──
            # Liste ucu daima `-ihale_tarihi` (ya da `-ilan_tarihi`) ile sıralar; tek
            # kolonluk filtre indeksi bulduğu satırları sonra sort etmek zorunda kalıyor.
            models.Index(fields=["ihale_durum", "-ihale_tarihi"]),
            models.Index(fields=["il_id", "-ihale_tarihi"]),
            models.Index(fields=["idare_id", "-ihale_tarihi"]),
            # `ihale_usul` filtreleniyor ama hiç indekslenmemişti.
            models.Index(fields=["ihale_usul"]),
            # ── Detayı hiç çekilmemiş ihaleler (KISMİ indeks) ──
            # ⚠️ `sync_contractors.enqueue_missing_detail` ve `refresh_stale`
            # `WHERE detail_synced_at IS NULL ORDER BY <tarih> DESC LIMIT N` sorar.
            # Bu sıralama+LIMIT'i gören planlayıcı tarih indeksini geriye tarayıp
            # filtrelemeyi seçiyordu; eşleşen satırlar seyrek olduğu için 50 satır
            # bulmak **çağrı başına 78 sn** sürüyordu (pg_stat_statements'ta 6.3 saat
            # toplam DB zamanı — arama sorgularının buffer cache'ini boşaltan asıl fail).
            # Kısmi indeks yalnızca detayı eksik satırları tutar → küçük ve tam eşleşir.
            models.Index(
                fields=["-ihale_tarihi"],
                name="ekap_tender_detayeksik_iht",
                condition=models.Q(detail_synced_at__isnull=True),
            ),
            models.Index(
                fields=["-ilan_tarihi"],
                name="ekap_tender_detayeksik_ilt",
                condition=models.Q(detail_synced_at__isnull=True),
            ),
        ]

    def __str__(self):
        return f"{self.ikn} — {self.ihale_adi[:60]}"


class TenderDate(models.Model):
    """ihaleTarihSaatList elemanı."""

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="tarihler")
    etiket = models.CharField(max_length=100)
    deger = models.CharField(max_length=100, blank=True)

    class Meta:
        verbose_name = "İhale Tarihi"
        verbose_name_plural = "İhale Tarihleri"


class OkasItem(models.Model):
    """ihtiyacKalemiOkasList elemanı (ihaleye özel)."""

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="okas_kalemleri")
    kodu = models.CharField(max_length=255, blank=True)
    adi = models.CharField(max_length=500, blank=True)
    # Türkçe+aksan-duyarsız arama (normalize_tr(adi)); senkronda doldurulur.
    # ⚠️ Kardeşleri (`Tender.ihale_adi_norm`, `Authority.ad_norm`, `OkasCode.adi_norm`)
    # baştan normalize sütunluydu; burası **atlanmıştı** ve filtre `adi__icontains`
    # kullanıyordu. Sonucu iki katmanlıydı: (1) Türkçe küçük harfle arama boş dönüyordu,
    # (2) `icontains` → `UPPER(adi) LIKE …` ürettiği için kolonun üstündeki fonksiyon
    # trigram indeksini **kullanılamaz** kılıyordu (denetimde `idx_scan = 0`, 75 MB ölü).
    adi_norm = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "İhale OKAS Kalemi"
        verbose_name_plural = "İhale OKAS Kalemleri"
        indexes = [
            # `okas_kod` filtresi `kodu__startswith` ile arar (Exists alt sorgusu içinde).
            # `tender` FK indeksi tek başına yetmiyordu → tüm OkasItem taranıyordu.
            models.Index(fields=["kodu", "tender"]),
            # `okas_adi` filtresi `adi_norm__contains` → trigram (bkz. Tender'daki aynı
            # gerekçe). ⚠️ Trigram `adi`da DEĞİL `adi_norm`da olmalı: sorgu normalize
            # sütunu tarar, ham sütundaki indeks hiç kullanılmazdı.
            GinIndex(fields=["adi_norm"], opclasses=["gin_trgm_ops"],
                     name="ekap_okasitem_adinorm_trgm"),
        ]


class Announcement(models.Model):
    """ilanList elemanı."""

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="ilanlar")
    ekap_ilan_id = models.CharField(max_length=255, blank=True, db_index=True)
    # ilanList[].sozlesmeId → Sonuç İlanı'nı (ilanTip=4) sözleşmeye bağlayan JOIN anahtarı
    ekap_sozlesme_id = models.CharField(max_length=64, blank=True, db_index=True)
    ilan_tip = models.IntegerField(null=True, blank=True)  # 1-5,10
    ilan_tarihi = models.DateTimeField(null=True, blank=True)
    baslik = models.CharField(max_length=500, blank=True)
    veri_html = models.TextField(blank=True)
    istekli_adi = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "İlan"
        verbose_name_plural = "İlanlar"
        ordering = ["-ilan_tarihi"]


# ── Yüklenici (firma) kaydı ────────────────────────────
# EKAP payload'ında VKN/vergi no/TCKN YOKTUR → kimlik, ünvanın kanonikleştirilmiş
# hâlidir (`ekap.contractors.canonical_key`). Ham yazımlar `ContractorAlias`'ta izlenir.

class Contractor(models.Model):
    """Sözleşme imzalayan yüklenici — firma, şahıs ya da ortak girişim."""

    class Kind(models.TextChoices):
        FIRMA = "firma", "Firma"
        SAHIS = "sahis", "Şahıs"
        ORTAK_GIRISIM = "ortak_girisim", "Ortak Girişim"

    # Kimlik
    kanonik_ad = models.CharField(max_length=500, db_index=True)      # gösterim
    kanonik_anahtar = models.CharField(max_length=500, unique=True)   # KİMLİK
    # ad + anahtar + alias'lar tek aranabilir blob. 1000 char sınırı bilinçli: normalize_tr
    # ASCII'ye katladığı için ≤2000 byte → Postgres btree 2704-byte sınırının altında.
    arama_norm = models.CharField(max_length=1000, blank=True, db_index=True)
    kind = models.CharField(
        max_length=20, choices=Kind.choices, default=Kind.FIRMA, db_index=True
    )
    tuzel_tip = models.CharField(max_length=20, blank=True)  # ltdsti|as|kollektif|…

    # Sonuç İlanı'ndan zenginleştirme (opsiyonel — her sözleşmede ilan olmayabilir)
    uyruk = models.CharField(max_length=100, blank=True)
    adres = models.TextField(blank=True)
    il_adi = models.CharField(max_length=100, blank=True, db_index=True)
    il_id = models.IntegerField(null=True, blank=True, db_index=True)

    # ── Denormalize agregalar ──
    # Kural: firmalar arası SIRALAMA anahtarları denormalize (milyonlarca sözleşmede
    # istek başına GROUP BY tam tarama demek); tek firma kırılımları canlı hesaplanır.
    sozlesme_sayisi = models.IntegerField(default=0, db_index=True)
    # ⚠️ `ihale_sayisi` AYRI tutulur: kısımlı ihalede bir firma 3 kısım alırsa
    # 3 sözleşme / 1 ihale olur; sözleşme sayısını "kaç ihale aldı" diye göstermek yanlış.
    ihale_sayisi = models.IntegerField(default=0)
    idare_sayisi = models.IntegerField(default=0)
    toplam_sozlesme_bedeli = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True
    )
    ilk_sozlesme_tarihi = models.DateTimeField(null=True, blank=True)
    son_sozlesme_tarihi = models.DateTimeField(null=True, blank=True, db_index=True)
    ortalama_indirim_orani = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True
    )
    # Yaklaşık maliyet kapsamı kısmi olduğu için ortalama TEK BAŞINA yanıltıcı →
    # örnek sayısı her zaman yanında taşınır.
    indirim_orani_ornek_sayisi = models.IntegerField(default=0)
    uye_sayisi = models.IntegerField(default=0)            # kind=ortak_girisim için
    ortak_girisim_sayisi = models.IntegerField(default=0)  # üye firma için
    uyeleri_cozumlendi = models.BooleanField(default=True)  # False → elle çözüm bekliyor
    agrega_guncelleme = models.DateTimeField(null=True, blank=True)

    ilk_gorulme = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Yüklenici"
        verbose_name_plural = "Yükleniciler"
        ordering = ["-sozlesme_sayisi"]
        indexes = [
            models.Index(fields=["-sozlesme_sayisi"]),
            models.Index(fields=["-toplam_sozlesme_bedeli"]),
            models.Index(fields=["kind", "-sozlesme_sayisi"]),
            models.Index(fields=["-son_sozlesme_tarihi"]),
            # ── Firma araması: `arama_norm__contains` → LIKE '%…%' ──
            # `db_index=True` btree'si baştan jokerli LIKE'ta işe yaramaz, trigram şart.
            GinIndex(
                fields=["arama_norm"],
                opclasses=["gin_trgm_ops"],
                name="ekap_contractor_arama_trgm",
            ),
            # ── Firma listesinin ORDER BY'ı ──
            # ⚠️ `ContractorListView` `F(alan).desc(nulls_last=True), "kanonik_ad"` ile
            # sıralar. Yukarıdaki düz `-alan` indeksleri Postgres'te DESC **NULLS FIRST**
            # demektir → pathkey eşleşmez, planlayıcı onları sıralama için KULLANAMAZ ve
            # her istekte tüm tabloyu sort eder. İkincil anahtar `kanonik_ad` da tek
            # kolonluk indeksle karşılanamaz. Aşağıdakiler sorgunun tam karşılığıdır.
            models.Index(
                models.F("sozlesme_sayisi").desc(nulls_last=True),
                "kanonik_ad",
                name="ekap_ctr_soz_ad_idx",
            ),
            models.Index(
                models.F("toplam_sozlesme_bedeli").desc(nulls_last=True),
                "kanonik_ad",
                name="ekap_ctr_bedel_ad_idx",
            ),
            models.Index(
                models.F("son_sozlesme_tarihi").desc(nulls_last=True),
                "kanonik_ad",
                name="ekap_ctr_tarih_ad_idx",
            ),
        ]

    def __str__(self):
        return self.kanonik_ad[:80]


class ContractorAlias(models.Model):
    """Bir yüklenicinin gördüğümüz ham yazımları (+ ingest cache)."""

    class Kaynak(models.TextChoices):
        SOZLESME = "sozlesme", "Sözleşme yüklenici adı"
        KISIM = "kisim", "Kısım yüklenici adı"
        ILAN = "ilan", "Sonuç ilanı istekli adı"
        MANUEL = "manuel", "Elle"

    contractor = models.ForeignKey(
        Contractor, on_delete=models.CASCADE, related_name="aliaslar"
    )
    ham_ad = models.CharField(max_length=500)
    # GLOBAL unique — hem O(1) ingest cache anahtarı, hem "bir yazım tam olarak bir
    # firmaya gider" DB-seviyesi garantisi.
    ham_ad_norm = models.CharField(max_length=500, unique=True)
    kaynak = models.CharField(
        max_length=20, choices=Kaynak.choices, default=Kaynak.SOZLESME
    )
    ilk_gorulme = models.DateTimeField(auto_now_add=True)
    son_gorulme = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Yüklenici Yazımı"
        verbose_name_plural = "Yüklenici Yazımları"

    def __str__(self):
        return self.ham_ad[:80]


class ContractorMembership(models.Model):
    """Ortak girişim ↔ üye firma bağı."""

    class Guven(models.TextChoices):
        YUKSEK = "yuksek", "Yüksek"
        ORTA = "orta", "Orta"
        DUSUK = "dusuk", "Düşük"

    ortak_girisim = models.ForeignKey(
        Contractor, on_delete=models.CASCADE, related_name="uyelikler"
    )
    uye = models.ForeignKey(
        Contractor, on_delete=models.CASCADE, related_name="ortakliklar"
    )
    sira = models.IntegerField(default=0)
    pilot = models.BooleanField(default=False)
    kaynak_metin = models.CharField(max_length=500, blank=True)
    guven = models.CharField(max_length=10, choices=Guven.choices, default=Guven.YUKSEK)

    class Meta:
        verbose_name = "Ortak Girişim Üyeliği"
        verbose_name_plural = "Ortak Girişim Üyelikleri"
        constraints = [
            models.UniqueConstraint(fields=["ortak_girisim", "uye"], name="uniq_jv_uye")
        ]
        indexes = [models.Index(fields=["uye"])]


class Contract(models.Model):
    """sozlesmeBilgiList elemanı."""

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="sozlesmeler")
    # sozlesmeBilgiList[].id — kararlı EKAP anahtarı. Upsert bunun üzerinden yapılır;
    # olmadan her detay senkronunda satırlar silinip yeniden yaratılıyordu (PK churn).
    # ⚠️ `db_index` YOK (0017'de düşürüldü, 138 MB): `_bulk_upsert_children` satırları
    # `tender.sozlesmeler.all()` ile çekip **Python'da** eşleştirir; DB'ye hiç
    # `WHERE ekap_sozlesme_id = …` sorgusu gitmez. Denetimde `idx_scan = 0` çıktı.
    ekap_sozlesme_id = models.CharField(max_length=64, blank=True)
    yuklenici_adi = models.CharField(max_length=500, blank=True)
    # ⚠️ `db_index` YOK (0017'de düşürüldü, 64 MB): metin araması aşağıdaki
    # `ekap_contract_yukadi_trgm` (GIN trigram) üzerinden gider; `%…%` deseninde
    # btree zaten işe yaramaz.
    yuklenici_adi_norm = models.CharField(max_length=500, blank=True)
    yuklenici = models.ForeignKey(
        Contractor, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sozlesmeler",
    )
    sozlesme_tarih = models.CharField(max_length=255, blank=True)  # ham string
    sozlesme_tarihi = models.DateTimeField(null=True, blank=True, db_index=True)
    sozlesme_bedeli = models.CharField(max_length=100, blank=True)
    sozlesme_bedeli_num = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    en_dusuk_teklif = models.CharField(max_length=100, blank=True)
    en_dusuk_teklif_num = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    en_yuksek_teklif = models.CharField(max_length=100, blank=True)
    en_yuksek_teklif_num = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    # ⚠️ `yaklasik_maliyet` (string) EKAP tarafında BOZUK üretilir → sayısala çevrilmez.
    # `yaklasik_maliyet_num` yalnızca Sonuç İlanı'ndan doldurulur, yoksa NULL kalır.
    yaklasik_maliyet = models.CharField(max_length=100, blank=True)
    yaklasik_maliyet_num = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    yaklasik_maliyet_kaynak = models.CharField(max_length=20, blank=True)  # "sonuc_ilani"|""
    fesih_string = models.CharField(max_length=255, blank=True)
    tasfiye_transfer_string = models.CharField(max_length=255, blank=True)

    # Sonuç İlanı türevleri
    tender_yaklasik_maliyet_num = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True
    )
    teklif_sayisi = models.IntegerField(null=True, blank=True)
    gecerli_teklif_sayisi = models.IntegerField(null=True, blank=True)
    dokuman_indiren_sayisi = models.IntegerField(null=True, blank=True)
    indirim_orani = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    ekap_ilan_id = models.CharField(max_length=64, blank=True)  # izlenebilirlik

    # Bu satırı İLK KEZ ne zaman gördüğümüz (EKAP'ın imza tarihi DEĞİL).
    # ⚠️ `Contract` `TimeStampedModel`'den türemiyor, yani `created_at` YOK; rakip alarmı
    # ("takip ettiğim firma yeni iş aldı") "yeni keşfedilen sözleşme"yi başka türlü
    # bilemez. `sozlesme_tarihi` bu iş için KULLANILAMAZ: Sonuç İlanı imzadan aylar sonra
    # yayımlanabildiği için eski tarihli bir sözleşme bugün keşfedilebiliyor.
    # Eski satırlarda NULL kalır = "eski" sayılır (geriye dönük doldurma anlamsız olurdu:
    # onları ne zaman gördüğümüz kayıtlı değil).
    # ⚠️ `db_index` YOK ve gerekmiyor: rakip alarmı bu kolonu tek başına değil,
    # `yuklenici_id__in=<takip edilen birkaç firma>` ile birlikte sorguluyor. O yordam
    # çok seçici ve mevcut `(yuklenici, -sozlesme_tarihi)` indeksi tarafından karşılanıyor;
    # `ilk_gorulme` sadece heap filtresi olarak kalıyor. 881k satırlık tabloda gereksiz
    # bir indeks kurmak hem yazma maliyeti hem de migration'da ACCESS EXCLUSIVE kilidi
    # demekti. İhtiyaç EXPLAIN'le kanıtlanırsa CONCURRENTLY eklenir.
    ilk_gorulme = models.DateTimeField(null=True, blank=True)

    # İngest-kopyası denormalizasyonlar — firma sorgularından Tender JOIN'ini çıkarır.
    # Milyonlarca sözleşme satırında bu JOIN, indeks taraması ile nested loop farkıdır.
    idare_id = models.CharField(max_length=255, blank=True, db_index=True)
    il_id = models.IntegerField(null=True, blank=True, db_index=True)
    ihale_tip = models.IntegerField(null=True, blank=True, db_index=True)
    # ⚠️ Bunlar **fiyat istihbaratının** (benchmark) darboğazıydı. Ölçüm 2026-08-13:
    # benzerlik merdiveni `tender__okas_ana_kod` üzerinden JOIN yapıyordu ve planlayıcı
    # 11 GB'lık `ekap_tender`'da **tam Seq Scan** seçiyordu (487.244 satır elendi,
    # 1,7 GB okundu) → uç başına **4,5 saniye**. Kopyayla sorgu tamamen `ekap_contract`
    # (1 GB) içinde kalır. `db_index` YOK: aşağıdaki bileşik indeksler (kod + tarih)
    # zaten baş kolonu kapsıyor, tek kolonluk btree fazladan yazma maliyeti olurdu.
    okas_ana_kod = models.CharField(max_length=16, blank=True)
    okas_bucket = models.CharField(max_length=4, blank=True)
    en_ust_idare_kod = models.CharField(max_length=16, blank=True)

    class Meta:
        verbose_name = "Sözleşme"
        verbose_name_plural = "Sözleşmeler"
        indexes = [
            models.Index(fields=["yuklenici", "-sozlesme_tarihi"]),
            models.Index(fields=["-sozlesme_tarihi"]),
            # ── Benchmark benzerlik merdiveni (0015) ────────────────────────────
            # Kademe 1-3 (idare / il / ülke) hepsi `okas_ana_kod` eşitliğiyle başlar;
            # idare/il o dar aralıkta ucuz heap filtresidir (kod başına ~300 sözleşme).
            models.Index(fields=["okas_ana_kod", "-sozlesme_tarihi"], name="ekap_contract_okas_tarih_idx"),
            # Kademe 4: OKAS grubu + ihale türü. (Kademe 4b — OKAS'ı olmayan %19 —
            # mevcut `idare_id` indeksini kullanır, ek indeks gerekmez.)
            models.Index(fields=["okas_bucket", "ihale_tip", "-sozlesme_tarihi"], name="ekap_contract_bucket_tip_idx"),
            # İdare profilinin bakanlık kapsamı — artık `tender__` JOIN'i yapmadan.
            models.Index(fields=["en_ust_idare_kod", "-sozlesme_tarihi"], name="ekap_contract_bakanlik_idx"),
            # İhale listesindeki `yuklenici` metin filtresi (Exists alt sorgusu) bu
            # kolonda `%…%` LIKE yapıyor; `db_index=True` btree'si yetmez → trigram.
            GinIndex(
                fields=["yuklenici_adi_norm"],
                opclasses=["gin_trgm_ops"],
                name="ekap_contract_yukadi_trgm",
            ),
        ]

    def __str__(self):
        return f"{self.yuklenici_adi[:50]} — {self.sozlesme_bedeli}"


class ContractSection(models.Model):
    """
    kisimItemDto.kisimList elemanı.

    ⚠️ `en_dusuk_teklif` / `en_yuksek_teklif` / `yaklasik_maliyet` string'leri EKAP
    tarafında **bozuk ölçekte** üretilir (float'ın ondalık noktası silinip yeniden
    gruplanır → 10×/100× şişme). Sayısal karşılıkları YOKTUR ve eklenmemelidir;
    API'de de gösterilmezler. Yalnızca kanıt olarak saklanırlar.
    """

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="kisimlar")
    ekap_kisim_id = models.CharField(max_length=64, blank=True, db_index=True)
    kisim_adi = models.CharField(max_length=500, blank=True)
    en_dusuk_teklif = models.CharField(max_length=100, blank=True)
    en_yuksek_teklif = models.CharField(max_length=100, blank=True)
    yaklasik_maliyet = models.CharField(max_length=100, blank=True)

    class Meta:
        verbose_name = "Sözleşme Kısmı"
        verbose_name_plural = "Sözleşme Kısımları"


# ── Senkron durumu / log ───────────────────────────────
class RecurringTenderSeries(models.Model):
    """
    Tekrar eden ihale serisi — aynı idarenin yıldan yıla açtığı aynı iş.

    Kamu alımlarının büyük kısmı yıllık tekrarlar ("2024 YILI TEKSTİL … ALIMI" →
    "2025 YILI TEKSTİL … ALIM İŞİ"). Arşiv bunu görebiliyor, yani kullanıcı **ilan
    çıkmadan önce** haberdar olabilir ve hazırlanabilir. Rakiplerin sunamadığı bir şey.

    Seri anahtarı ingest'te üretilir (`ekap/series.py`, `sync.apply_pro_fields`); bu tablo
    yalnızca o anahtar üzerinde **gruplama sonucunu** saklar — metin karşılaştırması yok.
    """

    class Periyot(models.TextChoices):
        YILLIK = "yillik", "Yıllık"
        ALTI_AYLIK = "6_aylik", "6 Aylık"
        UC_AYLIK = "3_aylik", "3 Aylık"
        AYLIK = "aylik", "Aylık"
        DUZENSIZ = "duzensiz", "Düzensiz"

    seri_anahtar = models.CharField(max_length=40, db_index=True)
    idare_id = models.CharField(max_length=255, db_index=True)
    idare_adi = models.CharField(max_length=500, blank=True)
    en_ust_idare_kod = models.CharField(max_length=16, blank=True, db_index=True)
    il_id = models.IntegerField(null=True, blank=True, db_index=True)
    okas_ana_kod = models.CharField(max_length=16, blank=True, db_index=True)
    okas_ana_adi = models.CharField(max_length=500, blank=True)
    ihale_tip = models.IntegerField(null=True, blank=True)
    # Hata ayıklama/şeffaflık: serinin hangi iskelet üzerinden kurulduğu
    iskelet = models.CharField(max_length=300, blank=True)
    ornek_ihale_adi = models.TextField(blank=True)   # en son üyenin adı

    ihale_sayisi = models.IntegerField(default=0)
    ilk_ilan = models.DateTimeField(null=True, blank=True)
    son_ilan = models.DateTimeField(null=True, blank=True)
    son_ekap_id = models.CharField(max_length=255, blank=True)

    periyot_gun = models.IntegerField(null=True, blank=True)   # aralıkların medyanı
    sapma_gun = models.IntegerField(null=True, blank=True)     # standart sapma
    periyot_tip = models.CharField(max_length=12, choices=Periyot.choices, blank=True)
    guven = models.CharField(max_length=10, blank=True)        # yuksek|orta|dusuk

    beklenen_ilan_tarihi = models.DateField(null=True, blank=True)
    beklenen_ay = models.CharField(max_length=7, blank=True)   # "2026-11"
    # Seri hâlâ canlı mı: son ilandan bu yana 2 periyottan fazla geçtiyse False.
    aktif = models.BooleanField(default=True)

    ortalama_bedel = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    son_bedel = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    ortalama_indirim = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    indirim_ornek = models.IntegerField(default=0)

    guncelleme = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tekrar Eden İhale Serisi"
        verbose_name_plural = "Tekrar Eden İhale Serileri"
        ordering = ["-ihale_sayisi"]
        constraints = [
            models.UniqueConstraint(
                fields=["seri_anahtar", "idare_id"], name="uniq_recurring_series"
            )
        ]
        indexes = [
            # "Önümüzdeki 90 günde beklenen" sorgusu — yalnızca aktif seriler.
            models.Index(
                fields=["beklenen_ilan_tarihi"],
                condition=models.Q(aktif=True),
                name="ekap_seri_beklenen_idx",
            ),
            models.Index(fields=["idare_id", "-son_ilan"], name="ekap_seri_idare_idx"),
        ]

    def __str__(self):
        return f"{self.idare_adi[:40]} · {self.iskelet[:40]} ({self.ihale_sayisi})"


class SyncCheckpoint(models.Model):
    """Backfill/recent imleçleri — nereye kadar çekildiğini tutar."""

    name = models.CharField(max_length=50, unique=True)
    oldest_date = models.DateTimeField(null=True, blank=True)
    newest_date = models.DateTimeField(null=True, blank=True)
    cursor_skip = models.IntegerField(default=0)
    done = models.BooleanField(default=False)
    extra = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Senkron İmleci"
        verbose_name_plural = "Senkron İmleçleri"

    def __str__(self):
        return f"{self.name} (skip={self.cursor_skip}, done={self.done})"


class SyncRun(models.Model):
    """Her toplama görevi çalışmasının kaydı (gözlem/hata takibi)."""

    task = models.CharField(max_length=100, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="running")  # running/ok/error
    items = models.IntegerField(default=0)
    errors = models.IntegerField(default=0)
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = "Senkron Çalışması"
        verbose_name_plural = "Senkron Çalışmaları"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.task} @ {self.started_at:%Y-%m-%d %H:%M} [{self.status}]"


# ── Pazar panosu (Adım 6) ──────────────────────────────
class MarketStat(models.Model):
    """
    Pazar panosu özeti — grain **(yıl, OKAS iş grubu)**. Gece yenilenir.

    ⚠️ **Grain planın öngördüğünden DAR ve bu bilinçli.** Plan
    `(yil, okas_bucket, il_id, ihale_tip)` = 143.105 satır öngörüyordu; prod ölçümü
    (2026-08-13) gösterdi ki `il_id`/`ihale_tip` kırılımlarını materialize etmeye
    gerek yok — o sorgular **canlı 85-103 ms**. Yavaş olan yalnızca **yıl boyunu
    tarayan** şekillerdi:
        pano ana ekranı (yıl → iş grubu sıralaması)  1.488 ms
        yıl özeti                                      668 ms
        tek grubun yıllara göre seyri                  419 ms
    Bu grain üçünü de karşılar ve tablo ~20 bin satır (planın yedide biri) kalır.
    Küçük tablo = kısa yenileme, az bayatlık, az bakım.

    ⚠️ **`okas_bucket=""` GERÇEK VERİDİR** ("Sınıflandırılmamış"): sözleşmelerin
    214.079'unda OKAS yok. Sessizce düşürmek pazar toplamlarını yanlış gösterirdi.
    Bu yüzden boş string sentinel olarak KULLANILAMAZ — yıl toplamları ayrı modelde.

    ⚠️ **Ortalamalar `(toplam, ornek)` çifti olarak saklanır**, hazır ortalama değil:
    yalnızca böyle satırlar arası doğru birleşir (`Σtoplam/Σornek`). Hazır ortalamayı
    ortalamak ağırlıksız ve yanlış olurdu.
    ⚠️ **`count(DISTINCT firma)` BU MODELDE YOK** — satırlar arası toplanamaz ve
    burada tutulsa "yılın toplam firma sayısı" diye toplanmaya davetiye çıkarırdı.
    Yıl bazlı tekil sayılar `MarketYearStat`'ta, kendi kesin grain'inde durur.
    """

    yil = models.IntegerField(db_index=True)
    okas_bucket = models.CharField(max_length=4, blank=True)
    # Temsili grup adı (OkasCode'da 4 haneli karşılık varsa o, yoksa gruptaki en sık
    # kalem adı). Sunum içindir; filtreleme her zaman `okas_bucket` ile yapılır.
    ad = models.CharField(max_length=500, blank=True)

    sozlesme_sayisi = models.IntegerField(default=0)
    toplam_bedel = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    # Σ indirim_orani ve kaç sözleşmede bilindiği — bkz. yukarıdaki (toplam, ornek) kuralı
    indirim_toplam = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    indirim_ornek = models.IntegerField(default=0)
    teklif_toplam = models.BigIntegerField(null=True, blank=True)
    teklif_ornek = models.IntegerField(default=0)

    guncelleme = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Pazar İstatistiği"
        verbose_name_plural = "Pazar İstatistikleri"
        ordering = ["-yil", "-toplam_bedel"]
        constraints = [
            models.UniqueConstraint(fields=["yil", "okas_bucket"], name="ekap_marketstat_uq"),
        ]
        indexes = [
            # Pano ana ekranı: bir yılın gruplarını bedele göre sırala
            models.Index(fields=["yil", "-toplam_bedel"], name="ekap_market_yil_bedel_idx"),
            # Drill-down: tek grubun yıllara göre seyri
            models.Index(fields=["okas_bucket", "yil"], name="ekap_market_bucket_yil_idx"),
        ]

    def __str__(self):
        return f"{self.yil} · {self.okas_bucket or 'Sınıflandırılmamış'}"


class MarketYearStat(models.Model):
    """
    Yıl bazlı pazar özeti — grain **(yıl)**, ~17 satır.

    ⚠️ **Neden ayrı model:** `count(DISTINCT yuklenici)` ve `count(DISTINCT idare)`
    satırlar arası **TOPLANAMAZ** — 20 iş grubunda görünen aynı firma bir kez
    sayılmalıdır. `MarketStat` satırlarından türetmek sessiz bir yalan olurdu
    (plan R10: "medyan ve distinct sayılar yalnızca tek satır katkı verdiğinde döner").
    Bu yüzden tekil sayılar **kendi kesin grain'inde** saklanır.
    Canlı hesabı 668 ms sürüyordu (bir yılın ~161 bin satırı) → materialize edildi.
    """

    yil = models.IntegerField(unique=True)
    sozlesme_sayisi = models.IntegerField(default=0)
    toplam_bedel = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    indirim_toplam = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    indirim_ornek = models.IntegerField(default=0)
    teklif_toplam = models.BigIntegerField(null=True, blank=True)
    teklif_ornek = models.IntegerField(default=0)
    firma_tekil = models.IntegerField(null=True, blank=True)
    idare_tekil = models.IntegerField(null=True, blank=True)

    guncelleme = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Pazar Yıl Özeti"
        verbose_name_plural = "Pazar Yıl Özetleri"
        ordering = ["-yil"]

    def __str__(self):
        return f"{self.yil} — {self.sozlesme_sayisi} sözleşme"
