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


class ErisimEngeli(models.Model):
    """
    Erişim engeli: IP/CIDR kara listesi, hesap askıya alma, e-posta alanı yasağı.

    Mantık `core.guvenlik`'te (tek yer: admin, panel düğmeleri ve süre görevi aynı
    fonksiyonları çağırır). ⚠️ Satırı silmek engeli kaldırmanın doğru yolu DEĞİLDİR:
    hesap engeli `is_active`'i kapatır, geri açan `guvenlik.engeli_kaldir`'dır.
    """

    TUR_IP = "ip"
    TUR_KULLANICI = "kullanici"
    TUR_EPOSTA_ALANI = "eposta_alani"
    TUR_CHOICES = [
        (TUR_IP, "IP / ağ (CIDR)"),
        (TUR_KULLANICI, "Hesap"),
        (TUR_EPOSTA_ALANI, "E-posta alanı"),
    ]

    tur = models.CharField("tür", max_length=16, choices=TUR_CHOICES, db_index=True)
    # IP/CIDR ya da alan adı (küçük harf). Hesap engelinde kullanıcı e-postası (gösterim).
    deger = models.CharField("değer", max_length=255, blank=True, db_index=True)
    kullanici = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="erisim_engelleri", verbose_name="hesap",
    )
    sebep = models.TextField("sebep", blank=True)
    bitis = models.DateTimeField("bitiş", null=True, blank=True,
                                 help_text="Boş = süresiz.")
    aktif = models.BooleanField(default=True, db_index=True)
    # Hesap engeli: engelden önce hesap aktif miydi? Kaldırınca yalnızca o zaman açılır.
    onceki_aktif = models.BooleanField(null=True, blank=True, editable=False)
    olusturan = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", editable=False,
    )
    isabet_sayisi = models.PositiveIntegerField("isabet", default=0, editable=False)
    son_isabet = models.DateTimeField(null=True, blank=True, editable=False)
    kaldirilma = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Erişim Engeli"
        verbose_name_plural = "Erişim Engelleri"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tur", "aktif"], name="core_engel_tur_aktif")]

    def __str__(self):
        hedef = self.deger or (self.kullanici_id and f"uid {self.kullanici_id}") or "?"
        return f"{self.get_tur_display()}: {hedef}"

    @property
    def suresi_doldu(self) -> bool:
        return bool(self.bitis and self.bitis <= timezone.now())


class GuvenlikOlayi(models.Model):
    """
    Güvenlik günlüğü — yönetim panelindeki "son olaylar" ve şüpheli hesap kanıtı.

    ⚠️ Her istek DEĞİL, yalnızca olay yazılır (hız engeli, kota dolumu, doğrulama, engel
    isabeti…) ve kimlik × tür başına dakikada en çok bir satır (`guvenlik.olay_yaz`).
    90 günden eskiler `cleanup_old_notifications` ile silinir.
    """

    TURLER = [
        ("hiz_engeli", "Hız sınırı engeli"),
        ("ekap_kota", "EKAP kotası doldu"),
        ("dogrulama_istendi", "İnsan doğrulaması istendi"),
        ("dogrulama_gecti", "İnsan doğrulaması geçti"),
        ("dogrulama_basarisiz", "İnsan doğrulaması başarısız"),
        ("engel_isabet", "Engelli erişim denemesi"),
        ("tek_kullanimlik_eposta", "Tek kullanımlık e-posta reddi"),
        ("sayfa_siniri", "Free sayfa sınırı"),
        ("engel_eklendi", "Engel eklendi"),
        ("engel_kaldirildi", "Engel kaldırıldı"),
        ("otomatik_aski", "Otomatik askı"),
        ("pro_supheli", "Şüpheli Pro (askı yerine işaret)"),
    ]

    zaman = models.DateTimeField(default=timezone.now, db_index=True)
    tur = models.CharField("tür", max_length=32, choices=TURLER, db_index=True)
    kimlik = models.CharField(max_length=80, blank=True, db_index=True)
    kullanici = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="hesap",
    )
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    ua = models.CharField("tarayıcı", max_length=300, blank=True)
    istemci_turu = models.CharField("istemci", max_length=24, blank=True)
    yol = models.CharField(max_length=300, blank=True)
    ayrinti = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Güvenlik Olayı"
        verbose_name_plural = "Güvenlik Olayları"
        ordering = ["-zaman"]

    def __str__(self):
        return f"{self.get_tur_display()} · {self.kimlik}"


class TopluGonderim(models.Model):
    """Admin'den toplu e-posta / push bildirimi (`core/toplu.py`, admin → Toplu Gönderim).

    Alıcılar gönderim başlarken `TopluGonderimAlici` satırlarına dökülür; görev onları
    küçük partilerle işler ve süre bütçesi dolunca kendini yeniden kuyruğa atar →
    `celery` kuyruğundaki diğer işler beklemez, worker çökerse kalan alıcılar
    "Devam ettir" ile kaldığı yerden sürer. Aynı kullanıcıya iki kez gitmez (unique).
    """

    class Kanal(models.TextChoices):
        EPOSTA = "eposta", "E-posta"
        BILDIRIM = "bildirim", "Push bildirimi"

    class Durum(models.TextChoices):
        KUYRUKTA = "kuyrukta", "Kuyrukta"
        GONDERILIYOR = "gonderiliyor", "Gönderiliyor"
        TAMAMLANDI = "tamamlandi", "Tamamlandı"
        IPTAL = "iptal", "İptal edildi"

    class IcerikTuru(models.TextChoices):
        METIN = "metin", "Düz metin"
        HTML = "html", "HTML"

    class Kategori(models.TextChoices):
        GENEL = "genel", "Duyuru"
        BLOG = "blog", "Blog yazısı"

    kanal = models.CharField(max_length=10, choices=Kanal.choices)
    # `blog` → `User.blog_eposta` kapalı olana gitmez, abonelik bağlantısı yalnız blogdan çıkarır.
    kategori = models.CharField(max_length=8, choices=Kategori.choices, default=Kategori.GENEL,
                                db_default=Kategori.GENEL)
    durum = models.CharField(max_length=14, choices=Durum.choices, default=Durum.KUYRUKTA,
                             db_index=True)
    konu = models.CharField("konu / bildirim başlığı", max_length=200)
    eposta_baslik = models.CharField("e-posta üst başlığı", max_length=200, blank=True)
    on_metin = models.CharField("ön metin", max_length=200, blank=True)
    icerik_turu = models.CharField(max_length=6, choices=IcerikTuru.choices,
                                   default=IcerikTuru.METIN)
    icerik = models.TextField("içerik")
    dugme_metni = models.CharField(max_length=60, blank=True)
    dugme_url = models.URLField(max_length=500, blank=True)
    uygulama_ici = models.BooleanField("uygulama içi bildirim de yaz", default=True)
    sessiz_saat_yok = models.BooleanField("sessiz saat / günlük limit uygulanmasın",
                                          default=False)
    hedef = models.JSONField(default=dict, blank=True)
    hedef_ozet = models.CharField("hedef", max_length=200, blank=True)
    tema = models.CharField(max_length=2, blank=True)
    toplam = models.PositiveIntegerField(default=0)
    gonderilen = models.PositiveIntegerField(default=0)
    atlanan = models.PositiveIntegerField(default=0)
    hatali = models.PositiveIntegerField(default=0)
    son_hata = models.CharField(max_length=500, blank=True)
    olusturan = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField("oluşturuldu", auto_now_add=True, db_index=True)
    baslangic = models.DateTimeField(null=True, blank=True)
    bitis = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Toplu Gönderim"
        verbose_name_plural = "Toplu Gönderimler"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kanal_display()} · {self.konu}"

    @property
    def islenen(self) -> int:
        return self.gonderilen + self.atlanan + self.hatali


class TopluGonderimAlici(models.Model):
    class Durum(models.TextChoices):
        BEKLIYOR = "bekliyor", "Bekliyor"
        GONDERILDI = "gonderildi", "Gönderildi"
        ATLANDI = "atlandi", "Atlandı"
        HATALI = "hatali", "Hatalı"

    gonderim = models.ForeignKey(TopluGonderim, on_delete=models.CASCADE,
                                 related_name="alicilar")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="+")
    durum = models.CharField(max_length=10, choices=Durum.choices, default=Durum.BEKLIYOR)
    sebep = models.CharField(max_length=200, blank=True)
    islem_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Toplu Gönderim Alıcısı"
        verbose_name_plural = "Toplu Gönderim Alıcıları"
        constraints = [models.UniqueConstraint(fields=["gonderim", "user"],
                                               name="toplu_alici_tekil")]
        indexes = [models.Index(fields=["gonderim", "durum"], name="toplu_alici_durum")]

    def __str__(self):
        return f"{self.gonderim_id} → {self.user_id} ({self.durum})"
