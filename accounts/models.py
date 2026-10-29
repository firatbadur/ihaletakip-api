"""Kullanıcı modeli — Firestore users/{uid} dokümanının karşılığı."""
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

from core.models import TimeStampedModel

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
    # ⚠️ `fcm_token` alanı KALDIRILDI (0008) → `PushDevice` tablosu. Gerekçe dosya
    # sonundaki model docstring'inde; özeti: token cihaza aittir, kullanıcıya değil.
    deactivated_at = models.DateTimeField(null=True, blank=True)

    # ── Tanışma sihirbazı (mobil `Onboarding` ekranı) ───────
    # Mobil, girişten sonra `onboarding_status == "pending"` olan kullanıcıya
    # sihirbazı BİR KEZ açar. "Şimdilik geç" → `skipped` (bir daha zorlanmaz),
    # bitiş → `completed`. Durum sunucuda tutulur ki cihaz değişince tekrar sorulmasın.
    # Şirket bilgisi burada DEĞİL, `assistant.CompanyProfile`'dadır (asistanla ortak).
    class AgeRange(models.TextChoices):
        R18_24 = "18_24", "18-24"
        R25_34 = "25_34", "25-34"
        R35_44 = "35_44", "35-44"
        R45_54 = "45_54", "45-54"
        R55_PLUS = "55_plus", "55+"

    class OnboardingStatus(models.TextChoices):
        PENDING = "pending", "Bekliyor"
        SKIPPED = "skipped", "Atlandı"
        COMPLETED = "completed", "Tamamlandı"

    age_range = models.CharField(
        "yaş aralığı", max_length=8, choices=AgeRange.choices, blank=True
    )
    onboarding_status = models.CharField(
        "tanışma sihirbazı",
        max_length=10,
        choices=OnboardingStatus.choices,
        default=OnboardingStatus.PENDING,
    )
    onboarding_completed_at = models.DateTimeField(
        "sihirbaz tamamlanma", null=True, blank=True
    )

    # ── Etkinlik izi (MAU/DAU ölçümü) ──────────────────────
    # ⚠️ `last_login` bu projede İŞE YARAMAZ: JWT uçları (`serializers.issue_tokens`)
    # `django.contrib.auth.login()` çağırmaz ve `SIMPLE_JWT["UPDATE_LAST_LOGIN"]`
    # tanımlı değil → alan yalnızca admin paneline girenlerde dolar. Mobil kullanıcı
    # sayısı ondan okunamaz. Bu alan `accounts.authentication` tarafından, JWT ile
    # kimliği doğrulanan her istekte (kullanıcı başına GÜNDE BİR KEZ) yazılır.
    last_seen_at = models.DateTimeField(
        "son görülme",
        null=True,
        blank=True,
        db_index=True,
        help_text="Uygulamayı en son ne zaman kullandı (kimliği doğrulanan istek).",
    )

    # E-posta sahipliği kodla kanıtlandığı an (kayıt doğrulaması ya da şifre sıfırlama).
    # ⚠️ Boş = "bilinmiyor", "doğrulanmamış" DEĞİL: Google/Apple hesapları ve bu
    # özellikten önceki e-posta kayıtları boş kalır; hiçbir kapı buna bağlı değil.
    email_verified_at = models.DateTimeField("e-posta doğrulandı", null=True, blank=True)
    # Günlük e-posta bülteni (`tenders/services/eposta_bulten.py`) — varsayılan AÇIK.
    # ⚠️ `preferences` JSON'una DEĞİL ayrı kolona konuldu: `PATCH /auth/preferences/`
    # JSON'un TAMAMINI değiştiriyor, başka bir ayarı kaydeden istemci bu tercihi
    # sessizce silip (yokluk = açık) geri açardı. Kolon ayrıca bülten sorgusunda süzülür.
    # ⚠️ `db_default` şart: eski worker'lar bu kolonu tanımadan User yazmaya devam eder
    # (2026-08-28 / ekap 0023 dersi — NOT NULL kolon + eski kod = her INSERT düşer).
    email_notifications = models.BooleanField(
        "e-posta bildirimleri", default=True, db_default=True,
        help_text="Günlük bildirim özetini e-postayla al.",
    )

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
    # Deneme bitiş hatırlatması gönderildi mi? (subscription.tasks.send_trial_reminders)
    # Mobil paywall "deneme bitmeden bildirimle hatırlatırız" VAADİ veriyor; bu alan
    # o vaadin bir kez ve yalnızca bir kez tutulduğunun kaydıdır.
    trial_reminder_sent_at = models.DateTimeField(
        "deneme hatırlatması gönderildi",
        null=True,
        blank=True,
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


class PushDevice(TimeStampedModel):
    """
    Bir FCM cihaz token'ı ve o an hangi hesaba ait olduğu.

    ⚠️⚠️ `token` UNIQUE — bu modelin var oluş sebebi budur: **FCM token'ı CİHAZA
    aittir, kullanıcıya değil.** Aynı token iki satırda olamaz; cihazda hesap
    değişince satırın `user`'ı GÜNCELLENİR, ikinci satır açılmaz.

    Eskiden token `User.fcm_token` tek kolonundaydı ve hiçbir yerde tekillik
    aranmıyordu. Üretimde yaşandı (2026-10-05): tek telefonun token'ı **dört**
    hesapta birden kayıtlıydı (uid 3/4/5/309) → 08:00 özet görevi kullanıcı
    grain'inde çalıştığı için aynı telefona **üç push** düştü (10/93/84 ihale).
    İkinci bir token iki **gerçek müşteri** hesabında paylaşılıyordu (uid 299/313).

    ⚠️ Mükerrer bildirimden ağırı HESAPLAR ARASI SIZINTIdır: alarm push'unun
    BAŞLIĞI ihale adıdır (`tenders/services/templates.py` → `alarm_tender`), yani
    A'nın takip ettiği ihalenin adı, cihazda artık B ile oturum açmış kişinin
    kilit ekranında görünür. Mobilde özel FCM handler yok, gövde doğrudan OS
    bildirim tepsisine basılıyor → istemcide filtrelenemez.

    ⚠️ Eski tasarım KENDİLİĞİNDEN DÜZELMİYORDU: tek otomatik temizlik FCM'in
    `INVALID_TOKEN`'ına bağlı (`tenders/services/notify.py`), paylaşılan token ise
    canlı ve geçerli olduğu için o kapı hiç tetiklenmiyordu.

    ⚠️ `is_active` alanı YOK — ölü token'ı saklamanın değeri yok, satır silinir.
    `app_version`/`device_name` de yok: bugün hiçbir kararı etkilemiyorlar.
    """

    token = models.CharField("FCM token", max_length=500, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_devices",
        verbose_name="kullanıcı",
    )
    platform = models.CharField(
        "platform", max_length=10, blank=True, help_text="ios | android (opsiyonel)"
    )
    last_registered_at = models.DateTimeField("son kayıt", auto_now=True)

    class Meta:
        verbose_name = "Push Cihazı"
        verbose_name_plural = "Push Cihazları"
        ordering = ["-last_registered_at"]

    def __str__(self):
        return f"{self.user_id} · {self.token[:12]}…"


class EpostaKodu(models.Model):
    """
    E-posta doğrulama kodu — kayıt ve şifre sıfırlama (`accounts/services/eposta_kod.py`).

    `(email, amac)` başına TEK satır: yeni kod istenince satır güncellenir (eski kod
    geçersizleşir). Satırın kendisi hız sınırının durumudur (bekleme, saatlik sayaç),
    bu yüzden Redis'te değil DB'dedir — Redis temizlenince sınırlar sıfırlanmasın ve
    destek "kod gelmedi" sorusuna admin'den bakabilsin.

    ⚠️ Kodun kendisi SAKLANMAZ, yalnızca HMAC özeti (`kod_hash`).
    ⚠️ `veri` kayıt amacında bekleyen hesabın şifre ÖZETİNİ (`make_password`) taşır;
    hesap açılınca boşaltılır. Ham şifre hiçbir yere yazılmaz.
    """

    class Amac(models.TextChoices):
        KAYIT = "kayit", "Kayıt"
        SIFRE = "sifre", "Şifre sıfırlama"

    email = models.EmailField("e-posta")
    amac = models.CharField("amaç", max_length=10, choices=Amac.choices)
    kod_hash = models.CharField(max_length=64, blank=True)
    gecerlilik = models.DateTimeField("geçerlilik sonu", null=True, blank=True)
    deneme = models.PositiveSmallIntegerField("hatalı deneme", default=0)
    son_gonderim = models.DateTimeField("son gönderim", null=True, blank=True)
    pencere_bas = models.DateTimeField(null=True, blank=True)
    gonderim = models.PositiveSmallIntegerField("penceredeki gönderim", default=0)
    kullanildi_at = models.DateTimeField("kullanıldı", null=True, blank=True)
    veri = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "E-posta Kodu"
        verbose_name_plural = "E-posta Kodları"
        ordering = ["-son_gonderim"]
        constraints = [
            models.UniqueConstraint(fields=["email", "amac"], name="eposta_kodu_email_amac"),
        ]

    def __str__(self):
        return f"{self.email} · {self.get_amac_display()}"


class EpostaGeriBildirim(models.Model):
    """SES'ten gelen bounce/şikâyet kaydı (`accounts/services/ses_sns.py`).

    Destek "mail gelmiyor" sorusuna buradan bakar; kalıcı bounce ve şikâyet o adresin
    günlük bültenini kapatır (`User.email_notifications=False`). ⚠️ Doğrulama kodları
    yine denenir: kullanıcı posta kutusunu düzeltmiş olabilir — SES'in hesap düzeyi
    bastırma listesi zaten sorunlu adrese gerçek gönderimi engeller.
    """

    class Tur(models.TextChoices):
        BOUNCE = "bounce", "Geri döndü (kalıcı)"
        GECICI = "gecici", "Geri döndü (geçici)"
        SIKAYET = "sikayet", "Spam şikâyeti"

    email = models.EmailField("e-posta", db_index=True)
    tur = models.CharField("tür", max_length=10, choices=Tur.choices)
    alt_tur = models.CharField("alt tür", max_length=64, blank=True)
    aciklama = models.CharField("açıklama", max_length=500, blank=True)
    ses_mesaj_id = models.CharField(max_length=128, blank=True)
    bulten_kapatildi = models.BooleanField("bülten kapatıldı", default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "E-posta Geri Bildirimi"
        verbose_name_plural = "E-posta Geri Bildirimleri"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} · {self.get_tur_display()}"


class WebHandoff(models.Model):
    """
    Web → mobil geçiş kodu: web'de girişli kullanıcının telefonda AYNI hesapla,
    giriş ekranı görmeden ödeme ekranına düşmesi (`accounts/services/web_handoff.py`).

    Kod kısa süreli bir GİRİŞ ANAHTARIDIR: bozdurulunca hesabın token çifti döner.
    Bu yüzden:
    ⚠️ Kodların kendisi SAKLANMAZ, yalnızca HMAC özetleri (`kod_hash`, `kisa_hash`).
    ⚠️ Tek kullanımlık + kısa ömürlü (`WEB_HANDOFF_SURE_DK`, vars. 15). Kod AppsFlyer
    derin bağlantısından (`deep_link_sub2`) geçtiği için üçüncü tarafın loglarına düşer;
    ömrün kısa olması bunun bedelini sınırlar.
    ⚠️ "Süresi doldu" bir DURUM değildir, `gecerlilik`ten hesaplanır (`etkin_durum`):
    süresi dolan satırı güncelleyecek bir görev yok, olmasına da gerek yok.
    """

    class Durum(models.TextChoices):
        BEKLIYOR = "bekliyor", "Bekliyor"
        OKUTULDU = "okutuldu", "Okutuldu"
        KULLANILDI = "kullanildi", "Kullanıldı"
        IPTAL = "iptal", "İptal"

    AKTIF = (Durum.BEKLIYOR, Durum.OKUTULDU)
    SURESI_DOLDU = "suresi_doldu"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="web_handoffs",
        verbose_name="kullanıcı",
    )
    kod_hash = models.CharField(max_length=64, unique=True)
    kisa_hash = models.CharField(max_length=64, db_index=True)
    baglam = models.CharField(
        "bağlam", max_length=40, blank=True,
        help_text="Web'de tıklanan Pro özelliği (`PRO_CONTEXTS` anahtarı) — ödeme ekranına taşınır.",
    )
    durum = models.CharField("durum", max_length=12, choices=Durum.choices,
                             default=Durum.BEKLIYOR)
    gecerlilik = models.DateTimeField("geçerlilik sonu")
    olusturan_ip = models.GenericIPAddressField("oluşturan IP", null=True, blank=True)
    okutuldu_at = models.DateTimeField("okutuldu", null=True, blank=True)
    kullanildi_at = models.DateTimeField("kullanıldı", null=True, blank=True)
    kullanan_ip = models.GenericIPAddressField("kullanan IP", null=True, blank=True)
    iptal_at = models.DateTimeField("iptal", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Web → Mobil Geçiş"
        verbose_name_plural = "Web → Mobil Geçişler"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user_id} · {self.etkin_durum}"

    @property
    def etkin_durum(self) -> str:
        from django.utils import timezone

        if self.durum in self.AKTIF and self.gecerlilik <= timezone.now():
            return self.SURESI_DOLDU
        return self.durum
