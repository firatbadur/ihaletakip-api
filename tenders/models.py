"""
İhale ile ilgili kullanıcı içerikleri.

Firestore karşılıkları:
  Favorite     ← users/{uid}/favorites
  SavedFilter  ← users/{uid}/savedFilters
  SavedTender  ← users/{uid}/savedTenders
  TenderAlarm  ← users/{uid}/alarms
  Notification ← users/{uid}/notifications
"""
from django.conf import settings
from django.db import models

from core.models import TimeStampedModel

USER = settings.AUTH_USER_MODEL


class Favorite(models.Model):
    """Favori ihale."""

    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="favorites")
    tender_id = models.CharField(max_length=100, db_index=True)
    tender_title = models.CharField(max_length=500, blank=True)
    tender_type = models.CharField(max_length=50, null=True, blank=True)
    source = models.CharField(max_length=30, default="ekap")
    added_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Favori"
        verbose_name_plural = "Favoriler"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "tender_id"], name="uniq_user_favorite"
            )
        ]
        ordering = ["-added_at"]

    def __str__(self):
        return f"{self.tender_title or self.tender_id}"


class SavedFilter(TimeStampedModel):
    """Kaydedilmiş arama filtresi (opsiyonel alarm ile)."""

    user = models.ForeignKey(
        USER, on_delete=models.CASCADE, related_name="saved_filters"
    )
    name = models.CharField(max_length=200)
    filters = models.JSONField(default=dict)
    tags = models.JSONField(default=list, blank=True)
    alarm = models.JSONField(null=True, blank=True)
    # Bildirim servisi: bu filtre için en son ne zaman "yeni ihale" bildirimi
    # kontrol edildi. İlk kontrolde null → sadece taban alınır (bildirim yok).
    last_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Kayıtlı Filtre"
        verbose_name_plural = "Kayıtlı Filtreler"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class SavedTender(models.Model):
    """Kaydedilmiş (takip edilen) ihale."""

    user = models.ForeignKey(
        USER, on_delete=models.CASCADE, related_name="saved_tenders"
    )
    tender_id = models.CharField(max_length=100, blank=True)
    tender_ikn = models.CharField(max_length=100, db_index=True)
    tender_title = models.CharField(max_length=500, blank=True)
    tender_type = models.CharField(max_length=50, null=True, blank=True)
    tender_status = models.CharField(max_length=50, null=True, blank=True)
    tender_city = models.CharField(max_length=100, null=True, blank=True)
    tender_date = models.CharField(max_length=50, null=True, blank=True)
    institution = models.CharField(max_length=500, null=True, blank=True)
    saved_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Kayıtlı İhale"
        verbose_name_plural = "Kayıtlı İhaleler"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "tender_ikn"], name="uniq_user_saved_tender"
            )
        ]
        ordering = ["-saved_at"]

    def __str__(self):
        return f"{self.tender_title or self.tender_ikn}"


class TenderAlarm(TimeStampedModel):
    """İhale alarmı (hatırlatma / doküman değişikliği)."""

    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="alarms")
    tender_id = models.CharField(max_length=100, db_index=True)
    tender_ikn = models.CharField(max_length=100, null=True, blank=True)
    tender_title = models.CharField(max_length=500, blank=True)
    institution = models.CharField(max_length=500, null=True, blank=True)
    reminder_day = models.BooleanField(default=False)
    document_change = models.BooleanField(default=False)
    completed = models.BooleanField(default=False)
    # ── Bildirim servisi snapshot/guard alanları ──
    # Doküman değişikliği ve durum geçişi tespiti için son görülen değerler.
    last_dokuman_sayisi = models.IntegerField(null=True, blank=True)
    last_ihale_durum = models.IntegerField(null=True, blank=True)
    # "İhale sonuçlandı" bildirimi tek sefer gönderilsin diye guard.
    completed_notified = models.BooleanField(default=False)

    class Meta:
        verbose_name = "İhale Alarmı"
        verbose_name_plural = "İhale Alarmları"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "tender_id"], name="uniq_user_alarm"
            )
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Alarm: {self.tender_title or self.tender_id}"


class Notification(models.Model):
    """Kullanıcı bildirimi (push / uygulama içi)."""

    class Type(models.TextChoices):
        TENDER = "tender", "İhale"
        INFO = "info", "Bilgi"
        ALARM = "alarm", "Alarm"
        CHAT = "chat", "Sohbet"

    user = models.ForeignKey(
        USER, on_delete=models.CASCADE, related_name="notifications"
    )
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.INFO)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    tender_id = models.CharField(max_length=100, null=True, blank=True)
    tender_title = models.CharField(max_length=500, null=True, blank=True)
    tender_ikn = models.CharField(max_length=100, null=True, blank=True)
    institution = models.CharField(max_length=500, null=True, blank=True)
    # type=CHAT bildirimlerde: tıklanınca açılacak asistan sohbetinin (ChatConversation) id'si.
    conversation_id = models.BigIntegerField(null=True, blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Bildirim"
        verbose_name_plural = "Bildirimler"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
