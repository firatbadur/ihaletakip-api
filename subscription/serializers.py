"""subscription serializer'ları — fatura profili + web ödemeleri."""
import re

from rest_framework import serializers

from .models import BillingProfile, Payment


def tckn_gecerli(deger: str) -> bool:
    """TC kimlik no sağlama algoritması (11 hane, ilk hane 0 değil, 10. ve 11. hane)."""
    if not re.fullmatch(r"[1-9]\d{10}", deger or ""):
        return False
    d = [int(c) for c in deger]
    if ((sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10) != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def telefon_normalle(deger: str) -> str | None:
    """`0532…`, `532…`, `+90 532 …` → `+90532…` (Türkiye cep/sabit, 10 hane)."""
    rakam = re.sub(r"\D", "", deger or "")
    if rakam.startswith("90") and len(rakam) == 12:
        rakam = rakam[2:]
    elif rakam.startswith("0") and len(rakam) == 11:
        rakam = rakam[1:]
    if not re.fullmatch(r"[2-5]\d{9}", rakam):
        return None
    return "+90" + rakam


_KURUMSAL = ("company_name", "tax_office", "tax_number")


def profil_eksikleri(profil) -> list[str]:
    """Ödemeye engel eksik alanlar (kayıtlı profil sonradan bozulduysa da yakalanır)."""
    eksik = [a for a in ("full_name", "address", "city", "phone") if not getattr(profil, a)]
    if profil.type == BillingProfile.Type.KURUMSAL:
        eksik += [a for a in _KURUMSAL if not getattr(profil, a)]
    elif not profil.identity_number:
        eksik.append("identity_number")
    return eksik


class BillingProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingProfile
        fields = [
            "type", "full_name", "company_name", "tax_office", "tax_number",
            "identity_number", "address", "district", "city", "phone", "updated_at",
        ]
        read_only_fields = ["updated_at"]
        extra_kwargs = {
            "company_name": {"required": False},
            "tax_office": {"required": False},
            "tax_number": {"required": False},
            "identity_number": {"required": False},
            "district": {"required": False},
        }

    def validate_full_name(self, v):
        v = " ".join((v or "").split())
        if len(v.split()) < 2:
            raise serializers.ValidationError("Ad ve soyad birlikte girilmeli.")
        return v

    def validate_phone(self, v):
        n = telefon_normalle(v)
        if not n:
            raise serializers.ValidationError("Geçerli bir telefon numarası girin (+90…).")
        return n

    def validate(self, attrs):
        tur = attrs.get("type")
        hatalar = {}
        if tur == BillingProfile.Type.KURUMSAL:
            for alan in _KURUMSAL:
                attrs[alan] = (attrs.get(alan) or "").strip()
                if not attrs[alan]:
                    hatalar[alan] = ["Kurumsal faturada bu alan zorunludur."]
            vkn = attrs.get("tax_number")
            if vkn and not re.fullmatch(r"\d{10}", vkn):
                hatalar["tax_number"] = ["Vergi kimlik numarası 10 haneli olmalı."]
            # ⚠️ Kurumsal profilde TCKN TUTULMAZ — gereksiz kişisel veri (KVKK).
            attrs["identity_number"] = ""
        else:
            tckn = (attrs.get("identity_number") or "").strip()
            if not tckn_gecerli(tckn):
                hatalar["identity_number"] = ["Geçerli bir TC kimlik numarası girin."]
            attrs["identity_number"] = tckn
            for alan in _KURUMSAL:
                attrs[alan] = ""
        if hatalar:
            raise serializers.ValidationError(hatalar)
        return attrs


class PaymentListSerializer(serializers.ModelSerializer):
    pro_from = serializers.SerializerMethodField()
    pro_until = serializers.SerializerMethodField()
    invoice_available = serializers.BooleanField(read_only=True)
    cancel_requested = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id", "created_at", "amount", "currency", "status", "pro_from", "pro_until",
            "card_last4", "invoice_available", "cancel_requested",
        ]

    def _grant(self, obj):
        # Ters OneToOne yoksa `RelatedObjectDoesNotExist` (AttributeError alt sınıfı).
        return getattr(obj, "grant", None)

    def get_pro_from(self, obj):
        g = self._grant(obj)
        return g.starts_at.isoformat() if g else None

    def get_pro_until(self, obj):
        g = self._grant(obj)
        return g.ends_at.isoformat() if g and g.ends_at else None

    def get_cancel_requested(self, obj):
        return obj.cancel_requested_at is not None


class CheckoutConsentsSerializer(serializers.Serializer):
    on_bilgilendirme = serializers.BooleanField()
    mesafeli_satis = serializers.BooleanField()
    cayma_istisnasi = serializers.BooleanField()


class CheckoutSerializer(serializers.Serializer):
    consents = CheckoutConsentsSerializer()
    consent_version = serializers.CharField(max_length=64)


class CheckoutCompleteSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=128)
