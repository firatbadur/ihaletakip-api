"""
EKAP verisi modelleri.

Çekirdek `Tender` + çocuk tablolar (tarihler, OKAS kalemleri, ilanlar, sözleşmeler,
kısımlar) + lookup ingest (OkasCode, Authority) + City + senkron bookkeeping.
Alan envanteri mobil ekran tüketiminden çıkarıldı (bkz. plan dosyası).
"""
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

    class Meta:
        verbose_name = "OKAS Kodu"
        verbose_name_plural = "OKAS Kodları"
        ordering = ["kod"]

    def __str__(self):
        return f"{self.kod} — {self.adi}"


class Authority(models.Model):
    """DETSIS kurum/idare kaydı (EKAP DetsisAgaci'ndan periyodik çekilir)."""

    detsis_id = models.CharField(max_length=255, unique=True, db_index=True)
    ad = models.CharField(max_length=500, db_index=True)
    ust_idare = models.CharField(max_length=500, blank=True)
    idare_kod = models.CharField(max_length=255, blank=True, db_index=True)

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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "İhale"
        verbose_name_plural = "İhaleler"
        ordering = ["-ihale_tarihi"]
        indexes = [
            models.Index(fields=["ihale_tip", "ihale_durum"]),
            models.Index(fields=["-ilan_tarihi"]),
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

    class Meta:
        verbose_name = "İhale OKAS Kalemi"
        verbose_name_plural = "İhale OKAS Kalemleri"


class Announcement(models.Model):
    """ilanList elemanı."""

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="ilanlar")
    ekap_ilan_id = models.CharField(max_length=255, blank=True, db_index=True)
    ilan_tip = models.IntegerField(null=True, blank=True)  # 1-5,10
    ilan_tarihi = models.DateTimeField(null=True, blank=True)
    baslik = models.CharField(max_length=500, blank=True)
    veri_html = models.TextField(blank=True)
    istekli_adi = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "İlan"
        verbose_name_plural = "İlanlar"
        ordering = ["-ilan_tarihi"]


class Contract(models.Model):
    """sozlesmeBilgiList elemanı."""

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="sozlesmeler")
    yuklenici_adi = models.CharField(max_length=500, blank=True)
    sozlesme_tarih = models.CharField(max_length=255, blank=True)
    sozlesme_bedeli = models.CharField(max_length=100, blank=True)
    sozlesme_bedeli_num = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    en_dusuk_teklif = models.CharField(max_length=100, blank=True)
    en_dusuk_teklif_num = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    en_yuksek_teklif = models.CharField(max_length=100, blank=True)
    en_yuksek_teklif_num = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    yaklasik_maliyet = models.CharField(max_length=100, blank=True)
    yaklasik_maliyet_num = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    fesih_string = models.CharField(max_length=255, blank=True)
    tasfiye_transfer_string = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "Sözleşme"
        verbose_name_plural = "Sözleşmeler"


class ContractSection(models.Model):
    """kisimItemDto.kisimList elemanı."""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="kisimlar")
    kisim_adi = models.CharField(max_length=500, blank=True)
    en_dusuk_teklif = models.CharField(max_length=100, blank=True)
    en_yuksek_teklif = models.CharField(max_length=100, blank=True)
    yaklasik_maliyet = models.CharField(max_length=100, blank=True)

    class Meta:
        verbose_name = "Sözleşme Kısmı"
        verbose_name_plural = "Sözleşme Kısımları"


# ── Senkron durumu / log ───────────────────────────────
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
