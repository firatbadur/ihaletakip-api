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
    # Onboarding artık firmayı EKAP yüklenici veritabanında ARATIR (100k+ kayıt).
    # Bağlıysa geçmiş işler / iller / ihale türleri SORULMAZ — sözleşme geçmişinden
    # türetilir (bkz. services/profile_map.derive_from_contractor). SET_NULL: firma
    # kaydı birleştirilir/silinirse profil kaybolmasın, elle girilmiş ada düşsün.
    contractor = models.ForeignKey(
        "ekap.Contractor",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="company_profiles",
    )
    company_name = models.CharField(max_length=255)
    # Firmanın kendi web sitesi — profil haritası üretilirken KISACA okunur
    # (services/profile_map._website_text). Erişilemezse sessizce atlanır.
    website = models.URLField(max_length=300, blank=True)
    # Firmanın merkez ili (ekap_il_id). `cities` "ilgilenilen iller"dir, bu AYRI.
    il_id = models.IntegerField(null=True, blank=True)
    sector = models.CharField(max_length=255, blank=True)
    activity_areas = models.TextField(blank=True)
    cities = models.JSONField(default=list, blank=True)        # [ekap_il_id, ...]
    tender_types = models.JSONField(default=list, blank=True)  # [1..4]
    budget_min = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    budget_max = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    # ⚠️ ARTIK ONBOARDING'DE SORULMUYOR (firma bağlıysa EKAP sözleşme geçmişi zaten
    # elimizde). Alan eski profillerin verisi için duruyor; prompt'a hâlâ ekleniyor.
    past_works = models.JSONField(default=list, blank=True)    # ["2023 Ankara yol yapımı", ...]

    # Claude üretimi profil haritası (keywords, okas_prefixes, ...) — API'de read-only
    profile_map = models.JSONField(null=True, blank=True)
    profile_map_generated_at = models.DateTimeField(null=True, blank=True)
    # Haritayı üreten prompt girdisinin özeti. `PUT /assistant/profile/` her çağrıldığında
    # ücretli bir Claude (Sonnet) isteği tetikleniyordu — profilde hiçbir şey değişmemiş
    # olsa bile. Girdi özeti aynıysa görev LLM'e HİÇ gitmez, mevcut haritayı döner.
    # ⚠️ Bunun yerine "Pro'ya kilitle" YAPILMADI: kural tabanlı eşleştirme
    # (`services/matching.py`) `profile_map.keywords`/`okas_prefixes` okuyor ve ücretsiz
    # katmandaki eşleştirme/teaser da buna dayanıyor → harita Free için de gerekli.
    profile_map_kaynak_hash = models.CharField(max_length=64, blank=True)

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
    # Doluysa sohbet BU ihale odaklıdır; assistant_chat_task ihale detayını bağlama koyar
    tender_ikn = models.CharField(max_length=100, blank=True, db_index=True)
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
    # Bu mesajı üretmenin LLM maliyeti: {input_tokens, output_tokens, cache_read_input_tokens,
    # cache_creation_input_tokens, model, tur_sayisi}. Yalnızca asistan mesajlarında dolu.
    #
    # ⚠️ Araç kullanan asistanda mesaj başına maliyet, tek atış sohbete göre kat kat yüksek
    # (her araç turu tüm geçmişi yeniden gönderir). Bu alan İLK GÜNDEN toplanmalı — sonradan
    # eklenirse geriye dönük veri olmaz ve tavan/kademe kararı tahminle verilir.
    usage = models.JSONField(null=True, blank=True)
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
