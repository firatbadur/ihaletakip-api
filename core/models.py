"""Core modeller — ortak taban, uygulama ayarları, destek, DETSIS."""
from django.conf import settings
from django.db import models
from django.utils import timezone


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
    """
    Destek talebi — bir YAZIŞMADIR (`SupportMessage` dizisi), tek soru-tek yanıt değil.

    Akış: kullanıcı açar (Bekliyor) → admin yanıtlar (Yanıtlandı + push) → kullanıcı
    yanıtlar (Bekliyor) → … → bir taraf kapatır (Kapandı; kullanıcı yazarsa yeniden açılır).

    ⚠️ `message` ilk mesajın KOPYASIDIR (liste önizlemesi, admin araması ve eski
    istemcilerin `POST {message}` sözleşmesi için). Yazışmanın asıl kaynağı `messages`.
    ⚠️ `reply` ESKİDİR: tek yanıtlı dönemden kalır, 0004'te `SupportMessage`'a
    taşındı. Yeni kod YAZMAZ (eski worker'lar kolonu tanıdığı için silinmedi).
    """

    STATUS_CHOICES = [
        ("Bekliyor", "Bekliyor"),
        ("İşlemde", "İşlemde"),
        ("Yanıtlandı", "Yanıtlandı"),
        ("Kapandı", "Kapandı"),
    ]
    CATEGORY_CHOICES = [
        ("hesap", "Hesap & giriş"),
        ("abonelik", "Abonelik & ödeme"),
        ("bildirim", "Bildirimler"),
        ("veri", "İhale verisi"),
        ("hata", "Hata bildirimi"),
        ("oneri", "Öneri"),
        ("diger", "Diğer"),
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
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default="diger", db_default="diger",
        verbose_name="Konu",
    )
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Bekliyor")
    reply = models.TextField(null=True, blank=True)
    # Son mesajın zamanı — liste sıralaması (admin + mobil) buna göre.
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Kullanıcının henüz görmediği bir destek yanıtı var mı? Detay GET'i sıfırlar.
    user_unread = models.BooleanField(default=False, db_default=False)
    # Cihaz bağlamı (uygulama sürümü, platform, OS, model) — "hangi sürümde?" sorusunu
    # kullanıcıya sordurmamak için açılışta istemci gönderir. İzinli anahtarlar serializer'da.
    # ⚠️ null=True: deploy sırasında eski worker'lar kolonu tanımadan INSERT eder
    # (NOT NULL + eski kod = her INSERT düşer; ekap 0023 dersi).
    meta = models.JSONField(default=dict, blank=True, null=True)

    class Meta:
        verbose_name = "Destek Talebi"
        verbose_name_plural = "Destek Talepleri"
        ordering = ["-last_message_at", "-created_at"]

    def __str__(self):
        return f"#{self.pk} {self.email or 'Anonim'} — {self.status}"


class SupportMessage(models.Model):
    """Destek yazışmasındaki tek mesaj (kullanıcı ya da destek ekibi)."""

    SENDER_USER = "user"
    SENDER_STAFF = "staff"
    SENDER_CHOICES = [(SENDER_USER, "Kullanıcı"), (SENDER_STAFF, "Destek ekibi")]

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField(max_length=10, choices=SENDER_CHOICES)
    # Yanıtlayan yönetici (sender=staff). Kullanıcı mesajında boş — sahibi ticket.user.
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    body = models.TextField()
    # ⚠️ auto_now_add DEĞİL: 0004 taşıması eski yanıtları geçmiş tarihleriyle yazar.
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Destek Mesajı"
        verbose_name_plural = "Destek Mesajları"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"#{self.ticket_id} {self.sender}: {self.body[:40]}"

# Not: DETSIS/kurum kaydı artık ekap.Authority modelinde (EKAP'tan senkronlanır).
