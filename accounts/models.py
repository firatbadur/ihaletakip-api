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

    # ── İptal izi (RevenueCat webhook'undan yazılır, API'de read-only) ──────
    # Katman (`subscription_tier`) "şu an Pro mu?" sorusunu yanıtlar; iptal edildiğinde
    # kullanıcı dönem sonuna kadar Pro KALIR, dolayısıyla katmana bakarak iptali göremeyiz.
    # Bu alanlar ayrı tutulur ki admin "iptal etti ama süresi devam ediyor" ile
    # "iptal etti, süresi doldu"yu ayırt edebilsin.
    subscription_cancelled_at = models.DateTimeField(
        "abonelik iptal tarihi",
        null=True,
        blank=True,
        help_text="RevenueCat CANCELLATION/EXPIRATION event'i geldiği an. "
        "Yeni satın alma / iptalden dönüş (UNCANCELLATION) ile temizlenir.",
    )
    subscription_cancel_reason = models.CharField(
        "iptal nedeni",
        max_length=32,
        blank=True,
        help_text="RevenueCat cancel_reason / expiration_reason (UNSUBSCRIBE, "
        "BILLING_ERROR, CUSTOMER_SUPPORT ...).",
    )
    subscription_period_type = models.CharField(
        "dönem tipi",
        max_length=16,
        blank=True,
        help_text="Son event'in dönem tipi: TRIAL (ücretsiz deneme), NORMAL, INTRO. "
        "Deneme iptali ile ücretli iptali ayırt eder.",
    )
    subscription_last_event = models.CharField(
        "son RC event'i",
        max_length=40,
        blank=True,
        help_text="RevenueCat'ten gelen son webhook event tipi (teşhis için).",
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

    @property
    def is_cancelled(self) -> bool:
        """Kullanıcı aboneliğini iptal etti mi (süresi hâlâ devam ediyor olabilir)?"""
        return self.subscription_cancelled_at is not None

    @property
    def is_trial_cancelled(self) -> bool:
        """İptal edilen abonelik ücretsiz deneme miydi?"""
        return self.is_cancelled and self.subscription_period_type == "TRIAL"

    def save(self, *args, **kwargs):
        # display_name boşsa email'in yerel kısmından türet
        if not self.display_name and self.email:
            self.display_name = self.email.split("@")[0]
        super().save(*args, **kwargs)
