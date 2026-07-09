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
