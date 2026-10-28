"""
Abonelik modelleri — Pro hakkı kaynakları + web (iyzico) ödemeleri.

⚠️⚠️ **`User.subscription_tier` / `subscription_expires_at` artık bir ETKİN DEĞERDİR**:
tek yazarı `services.pro_hak.recompute_pro(user)`'dur ve `ProGrant` satırlarından
hesaplanır. Eskiden RevenueCat senkronu (`revenuecat._apply`) bu iki alanı doğrudan
yazıyordu → RC'de hakkı olmayan herkes (web'den ödeyen, admin'den Pro verilen)
mobil uygulama her açıldığında `verify` ile Free'ye çekiliyordu.
`User.is_premium`, `core.dashboard.pro_q()` ve istemciler değişmedi; değişen yalnızca
o alanları kimin, neye bakarak yazdığı.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel


class ProGrant(TimeStampedModel):
    """
    Bir kaynaktan gelen Pro hakkı.

    - `store`: RevenueCat (App Store / Play) — kullanıcı başına **TEK** satır, RC
      sonucu üzerine yazılır (aktif değilse `ends_at = şimdi`).
    - `iyzico`: web'den tek seferlik yıllık ödeme — **her ödeme bir satır** (geçmiş
      korunur), `payment` dolu.
    - `admin`: elle verilen hak (admin panelindeki satır).

    ⚠️ Satır SİLİNMEZ: iade/fesihte `revoked_at` doldurulur — "bu kişi neden Pro" sorusu
    geçmişten cevaplanabilsin.
    """

    class Source(models.TextChoices):
        STORE = "store", "Mağaza (RevenueCat)"
        IYZICO = "iyzico", "Web (iyzico)"
        ADMIN = "admin", "Admin (elle)"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pro_grants"
    )
    source = models.CharField("kaynak", max_length=10, choices=Source.choices)
    starts_at = models.DateTimeField("başlangıç")
    ends_at = models.DateTimeField(
        "bitiş", null=True, blank=True,
        help_text="Boş = bitişsiz (yalnız mağaza/admin).",
    )
    revoked_at = models.DateTimeField(
        "iptal (iade/fesih)", null=True, blank=True,
        help_text="Doluysa hak sayılmaz. Satır silinmez.",
    )
    # ⚠️ OneToOne: bir ödeme EN ÇOK bir hak açar — `complete/` ile mutabakat aynı anda
    # koşsa bile ikinci satır DB seviyesinde reddedilir (idempotenslik garantisi).
    payment = models.OneToOneField(
        "subscription.Payment", on_delete=models.PROTECT, null=True, blank=True,
        related_name="grant",
    )
    note = models.TextField("not", blank=True)
    # Web süre hatırlatmaları (30/7/1 gün + bitti) — bu hakkın bitişi için hangileri
    # gitti. DB'de tutulur: Redis sıfırlanınca aynı e-posta ikinci kez gitmesin.
    hatirlatmalar = models.JSONField("gönderilen hatırlatmalar", default=list, blank=True)

    class Meta:
        verbose_name = "Pro hakkı"
        verbose_name_plural = "Pro hakları"
        ordering = ["-starts_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"], condition=Q(source="store"), name="progrant_tek_magaza"
            ),
        ]
        indexes = [models.Index(fields=["user", "source"])]

    def __str__(self):
        return f"{self.get_source_display()} · {self.starts_at:%d.%m.%Y} → " + (
            f"{self.ends_at:%d.%m.%Y}" if self.ends_at else "bitişsiz"
        )


class BillingProfile(TimeStampedModel):
    """
    Fatura profili (kullanıcı başına bir) — iyzico alıcı bilgisi + e-Arşiv/e-Fatura.

    ⚠️ KVKK: TCKN/VKN yalnız fatura ve ödeme için işlenir; API'de yalnızca SAHİBİNE
    döner, loglara YAZILMAZ (`__str__` de kimlik numarası içermez).
    """

    class Type(models.TextChoices):
        BIREYSEL = "bireysel", "Bireysel"
        KURUMSAL = "kurumsal", "Kurumsal"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="billing_profile"
    )
    type = models.CharField("tür", max_length=10, choices=Type.choices)
    full_name = models.CharField(
        "ad soyad", max_length=120, help_text="Bireysel: ad soyad · kurumsal: yetkili adı."
    )
    company_name = models.CharField("unvan", max_length=255, blank=True)
    tax_office = models.CharField("vergi dairesi", max_length=120, blank=True)
    tax_number = models.CharField("VKN", max_length=10, blank=True)
    identity_number = models.CharField("TCKN", max_length=11, blank=True)
    address = models.CharField("adres", max_length=500)
    district = models.CharField("ilçe", max_length=80, blank=True)
    city = models.CharField("il", max_length=80)
    phone = models.CharField("telefon", max_length=16)

    class Meta:
        verbose_name = "Fatura profili"
        verbose_name_plural = "Fatura profilleri"

    def __str__(self):
        return self.company_name or self.full_name

    def anlik_goruntu(self) -> dict:
        """Ödeme anındaki kopya (`Payment.billing_snapshot`) — profil sonradan değişse
        de kesilen fatura ödeme anının bilgisiyle kalır."""
        return {
            "type": self.type,
            "full_name": self.full_name,
            "company_name": self.company_name,
            "tax_office": self.tax_office,
            "tax_number": self.tax_number,
            "identity_number": self.identity_number,
            "address": self.address,
            "district": self.district,
            "city": self.city,
            "phone": self.phone,
        }


def _fatura_yolu(instance, filename):
    return f"faturalar/{instance.created_at:%Y/%m}/{instance.pk}.pdf"


class Payment(TimeStampedModel):
    """
    Web'den tek seferlik ödeme (iyzico Checkout Form). `id` = iyzico `conversationId`.

    ⚠️ Tutar/KDV ödeme anında KOPYALANIR: plan fiyatı sonradan değişse de bu ödemenin
    doğrulaması ve faturası kendi tutarıyla yapılır.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Bekliyor"
        SUCCESS = "success", "Başarılı"
        FAILURE = "failure", "Başarısız"
        REFUNDED = "refunded", "İade edildi"
        PARTIALLY_REFUNDED = "partially_refunded", "Kısmi iade"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments"
    )
    plan_code = models.CharField("plan", max_length=32)
    months = models.PositiveSmallIntegerField("süre (ay)", default=12)
    amount = models.DecimalField("tutar (KDV dahil)", max_digits=10, decimal_places=2)
    vat_rate = models.PositiveSmallIntegerField("KDV oranı (%)")
    currency = models.CharField("para birimi", max_length=3, default="TRY")
    status = models.CharField(
        "durum", max_length=20, choices=Status.choices, default=Status.PENDING,
        db_index=True,
    )
    iyzico_token = models.CharField("iyzico token", max_length=128, blank=True, db_index=True)
    iyzico_payment_id = models.CharField("iyzico paymentId", max_length=64, blank=True)
    # İade `paymentTransactionId` ister (sepet kalemi bazlı) — detay yanıtından saklanır.
    iyzico_transaction_id = models.CharField(
        "iyzico paymentTransactionId", max_length=64, blank=True
    )
    billing_snapshot = models.JSONField("fatura bilgisi (ödeme anı)", default=dict)
    consents = models.JSONField("onaylar", default=dict)
    client_ip = models.GenericIPAddressField("istemci IP", null=True, blank=True)
    paid_at = models.DateTimeField("ödeme anı", null=True, blank=True)
    card_last4 = models.CharField("kart son 4", max_length=4, blank=True)
    card_association = models.CharField("kart ağı", max_length=32, blank=True)
    error_code = models.CharField("hata kodu", max_length=64, blank=True)
    error_message = models.CharField("hata mesajı", max_length=500, blank=True)
    invoice_number = models.CharField("fatura no", max_length=64, blank=True)
    invoice_url = models.URLField("fatura bağlantısı", max_length=500, blank=True)
    # ⚠️ Dosya `/media/` altında ama herkese açık sunulmaz (nginx medya sunmuyor;
    # Django yalnızca `/media/blog/`'u sunuyor) → indirme sahibine `invoice/` ucundan.
    invoice_pdf = models.FileField("fatura PDF", upload_to=_fatura_yolu, blank=True)
    cancel_requested_at = models.DateTimeField("fesih talebi", null=True, blank=True)
    refunded_at = models.DateTimeField("iade anı", null=True, blank=True)

    class Meta:
        verbose_name = "Web ödemesi"
        verbose_name_plural = "Web ödemeleri"
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.pk} · {self.amount} {self.currency} · {self.get_status_display()}"

    @property
    def invoice_available(self) -> bool:
        return bool(self.invoice_pdf)
