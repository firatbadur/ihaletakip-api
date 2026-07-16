"""Kullanıcı modeli — Firestore users/{uid} dokümanının karşılığı."""
from django.contrib.auth.models import AbstractUser
from django.db import models

from .managers import UserManager


class User(AbstractUser):
    """
    Genişletilmiş kullanıcı.

    AbstractUser alanları (username, email, first/last name, is_staff,
    is_active, date_joined ...) korunur; admin girişi `username` ile yapılır.
    Sosyal giriş kullanıcıları için ek alanlar eklenmiştir.
    """

    class Provider(models.TextChoices):
        EMAIL = "email", "E-posta"
        GOOGLE = "google", "Google"
        APPLE = "apple", "Apple"

    class Tier(models.TextChoices):
        FREE = "free", "Ücretsiz"
        PRO = "pro", "Pro"

    email = models.EmailField("e-posta", unique=True)
    display_name = models.CharField("görünen ad", max_length=255, blank=True)
    photo_url = models.URLField("profil foto", max_length=500, blank=True)
    provider = models.CharField(
        max_length=10, choices=Provider.choices, default=Provider.EMAIL
    )
    provider_uid = models.CharField(
        "sağlayıcı UID", max_length=255, blank=True, db_index=True
    )
    preferences = models.JSONField("tercihler", default=dict, blank=True)
    fcm_token = models.CharField("FCM token", max_length=500, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    # ── Abonelik ───────────────────────────────────────────
    # Premium (Pro) özellikler `is_premium` üzerinden kapılanır (bkz. accounts.premium).
    # Katman yalnızca admin/ödeme entegrasyonu tarafından değiştirilir; API'de read-only.
    subscription_tier = models.CharField(
        "abonelik katmanı",
        max_length=10,
        choices=Tier.choices,
        default=Tier.FREE,
    )
    subscription_expires_at = models.DateTimeField(
        "abonelik bitişi",
        null=True,
        blank=True,
        help_text="Boşsa süresiz Pro; doluysa bu tarihten sonra Free'ye düşer.",
    )

    objects = UserManager()

    class Meta:
        verbose_name = "Kullanıcı"
        verbose_name_plural = "Kullanıcılar"

    def __str__(self):
        return self.display_name or self.email or self.username

    @property
    def is_premium(self) -> bool:
        """
        Kullanıcı Pro (premium) özelliklere erişebiliyor mu?

        Superuser'lar her zaman premium sayılır (dahili test/kullanım). Aksi halde
        katman `pro` olmalı ve varsa `subscription_expires_at` gelecekte olmalıdır.
        """
        if self.is_superuser:
            return True
        if self.subscription_tier != self.Tier.PRO:
            return False
        exp = self.subscription_expires_at
        if exp is None:
            return True
        from django.utils import timezone

        return exp > timezone.now()

    def save(self, *args, **kwargs):
        # display_name boşsa email'in yerel kısmından türet
        if not self.display_name and self.email:
            self.display_name = self.email.split("@")[0]
        super().save(*args, **kwargs)
