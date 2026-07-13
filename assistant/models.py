"""
İhale Asistanı modelleri.

CompanyProfile        — kullanıcının firma profili + AI üretimi profil haritası
TenderRecommendation  — günlük eşleştirme sonucu ihale önerileri
ChatConversation      — sohbet oturumu (her yeni sohbet ayrı konuşma)
ChatMessage           — asistan sohbet geçmişi
"""
from django.conf import settings
from django.db import models

from core.models import TimeStampedModel

USER = settings.AUTH_USER_MODEL


class CompanyProfile(TimeStampedModel):
    """Firma profili — onboarding'de doldurulur, AI profil haritası üretilir."""

    user = models.OneToOneField(
        USER, on_delete=models.CASCADE, related_name="company_profile"
    )
    company_name = models.CharField(max_length=255)
    sector = models.CharField(max_length=255, blank=True)
    activity_areas = models.TextField(blank=True)
    cities = models.JSONField(default=list, blank=True)        # [ekap_il_id, ...]
    tender_types = models.JSONField(default=list, blank=True)  # [1..4]
    budget_min = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    budget_max = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    past_works = models.JSONField(default=list, blank=True)    # ["2023 Ankara yol yapımı", ...]

    # Claude üretimi profil haritası (keywords, okas_prefixes, ...) — API'de read-only
    profile_map = models.JSONField(null=True, blank=True)
    profile_map_generated_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)  # günlük eşleştirme açık/kapalı

    class Meta:
        verbose_name = "Firma Profili"
        verbose_name_plural = "Firma Profilleri"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.company_name} ({self.user})"


class TenderRecommendation(models.Model):
    """Günlük eşleştirme görevinin ürettiği ihale önerisi."""

    user = models.ForeignKey(
        USER, on_delete=models.CASCADE, related_name="tender_recommendations"
    )
    tender = models.ForeignKey(
        "ekap.Tender", on_delete=models.CASCADE, related_name="+"
    )
    score = models.FloatField()
    reasons = models.JSONField(default=list)  # ["Şehir: ANKARA", "Anahtar kelime: asfalt"]
    date = models.DateField(db_index=True)    # öneri günü (digest gruplama)
    seen = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "İhale Önerisi"
        verbose_name_plural = "İhale Önerileri"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "tender"], name="uniq_user_tender_recommendation"
            )
        ]
        ordering = ["-score"]

    def __str__(self):
        return f"{self.tender.ikn} → {self.user} ({self.score:.1f})"


class ChatConversation(models.Model):
    """Sohbet oturumu — mobil her açılışta boş sohbetle başlar, geçmiş buradan listelenir."""

    class Kind(models.TextChoices):
        CHAT = "chat", "Sohbet"
        DIGEST = "digest", "Günlük Özet"

    user = models.ForeignKey(
        USER, on_delete=models.CASCADE, related_name="assistant_conversations"
    )
    title = models.CharField(max_length=120, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.CHAT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        verbose_name = "Sohbet Oturumu"
        verbose_name_plural = "Sohbet Oturumları"
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["user", "-updated_at"])]

    def __str__(self):
        return f"{self.user} · {self.title or self.pk}"


class ChatMessage(models.Model):
    """Asistan sohbet mesajı (kullanıcı veya asistan)."""

    class Role(models.TextChoices):
        USER = "user", "Kullanıcı"
        ASSISTANT = "assistant", "Asistan"

    user = models.ForeignKey(
        USER, on_delete=models.CASCADE, related_name="assistant_messages"
    )
    # Faz 2 öncesi mesajlar konuşmasız kalabilir (null)
    conversation = models.ForeignKey(
        ChatConversation,
        on_delete=models.CASCADE,
        related_name="messages",
        null=True,
        blank=True,
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField()
    # {"kind": "digest"|"text", "tender_cards": [{ikn, ihale_adi, idare_adi, il, ihale_tarihi, ihale_tip}]}
    payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Sohbet Mesajı"
        verbose_name_plural = "Sohbet Mesajları"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            # Oturum bazlı sayfalama (en yeniden eskiye) için — uzun sohbetlerde hızlı
            models.Index(fields=["conversation", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.user} [{self.role}]: {self.content[:40]}"
