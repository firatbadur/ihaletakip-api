"""Core modeller — ortak taban, uygulama ayarları, destek, DETSIS."""
from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """created_at / updated_at içeren soyut taban model."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AppSetting(models.Model):
    """
    Anahtar-değer uygulama ayarı (admin'den yönetilebilir).

    Firestore'daki `config/ai_service` dokümanının karşılığı.
    Örn: key="anthropic_api_key" → value="sk-ant-..."
    """

    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True)
    description = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Uygulama Ayarı"
        verbose_name_plural = "Uygulama Ayarları"

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key, default=""):
        obj = cls.objects.filter(key=key).first()
        return obj.value if obj else default


class AppConfig(TimeStampedModel):
    """
    Uygulama geneli durum ayarı — tekil kayıt (singleton, pk=1).

    Mobil uygulama açılışta (ve öne geldiğinde) `/api/v1/app-status/` ucundan
    okur:
    - `maintenance_active` açıksa uygulama yalnızca bakım ekranını gösterir;
      başka hiçbir ekrana geçilmez.
    - Cihaz sürümü ilgili platformun min sürümünün altındaysa zorunlu
      güncelleme ekranı gösterilir ve uygulama kullanılamaz.

    Admin panelinden yönetilir (Uygulama Durumu).
    """

    # ── Bakım modu ──────────────────────────────────────
    maintenance_active = models.BooleanField(
        default=False, verbose_name="Bakım modu açık"
    )
    maintenance_title = models.CharField(
        max_length=200,
        blank=True,
        default="Bakım Çalışması",
        verbose_name="Bakım başlığı",
    )
    maintenance_message = models.TextField(
        blank=True,
        default=(
            "Sistemimizde kısa süreli bir bakım çalışması yapıyoruz. "
            "Lütfen birazdan tekrar deneyin."
        ),
        verbose_name="Bakım mesajı",
    )

    # ── Zorunlu güncelleme ──────────────────────────────
    # Sürüm alanları boş bırakılırsa o platform için sürüm kontrolü yapılmaz.
    min_ios_version = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name="Min. iOS sürümü",
        help_text=(
            "Örn: 1.2.0 — bu sürümün altındaki iOS cihazlar güncellemeye "
            "zorlanır. Boş = kontrol kapalı."
        ),
    )
    min_android_version = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name="Min. Android sürümü",
        help_text=(
            "Örn: 1.2.0 — bu sürümün altındaki Android cihazlar güncellemeye "
            "zorlanır. Boş = kontrol kapalı."
        ),
    )
    ios_store_url = models.URLField(
        blank=True, default="", verbose_name="App Store bağlantısı"
    )
    android_store_url = models.URLField(
        blank=True,
        default="https://play.google.com/store/apps/details?id=com.envisoft.ihaletakip",
        verbose_name="Google Play bağlantısı",
    )
    update_title = models.CharField(
        max_length=200,
        blank=True,
        default="Güncelleme Gerekli",
        verbose_name="Güncelleme başlığı",
    )
    update_message = models.TextField(
        blank=True,
        default=(
            "Uygulamayı kullanmaya devam etmek için lütfen en son sürüme "
            "güncelleyin."
        ),
        verbose_name="Güncelleme mesajı",
    )

    class Meta:
        verbose_name = "Uygulama Durumu"
        verbose_name_plural = "Uygulama Durumu"

    def __str__(self):
        return "Uygulama Durumu"

    def save(self, *args, **kwargs):
        self.pk = 1  # tekil kayıt — her zaman aynı satır güncellenir
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # tekil kayıt silinemez

    @classmethod
    def load(cls):
        """Tekil kaydı getirir; yoksa varsayılanlarla oluşturur."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SupportTicket(TimeStampedModel):
    """Destek talebi (Firestore `supportTickets` koleksiyonu)."""

    STATUS_CHOICES = [
        ("Bekliyor", "Bekliyor"),
        ("İşlemde", "İşlemde"),
        ("Yanıtlandı", "Yanıtlandı"),
        ("Kapandı", "Kapandı"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_tickets",
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Bekliyor")
    reply = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = "Destek Talebi"
        verbose_name_plural = "Destek Talepleri"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email or 'Anonim'} — {self.status}"

# Not: DETSIS/kurum kaydı artık ekap.Authority modelinde (EKAP'tan senkronlanır).
