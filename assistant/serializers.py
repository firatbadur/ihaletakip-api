"""assistant serializer'ları."""
from rest_framework import serializers

from .models import ChatConversation, ChatMessage, CompanyProfile, TenderRecommendation


class CompanyProfileSerializer(serializers.ModelSerializer):
    # Firma EKAP yüklenici kaydına bağlıysa geçmiş işler/iller/türler ondan türetilir
    # (bkz. services/profile_map.derive_from_contractor) → sihirbaz bunları sormaz.
    contractor_ad = serializers.CharField(source="contractor.kanonik_ad", read_only=True, default=None)
    contractor_sozlesme_sayisi = serializers.IntegerField(
        source="contractor.sozlesme_sayisi", read_only=True, default=None
    )
    # ⚠️ `URLField` DEĞİL: kullanıcılar sitelerini doğal biçimde yazıyor
    # ("ornekinsaat.com.tr", "www.ornekinsaat.com.tr") ve URLField bunları
    # "Geçerli bir URL girin." diye REDDEDİYORDU. Şema `validate_website`'te
    # tamamlanır; doğrulama ondan sonra yapılır.
    website = serializers.CharField(
        max_length=300, required=False, allow_blank=True, trim_whitespace=True
    )

    class Meta:
        model = CompanyProfile
        fields = [
            "id",
            "contractor",
            "contractor_ad",
            "contractor_sozlesme_sayisi",
            "company_name",
            "website",
            "il_id",
            "sector",
            "activity_areas",
            "cities",
            "tender_types",
            "budget_min",
            "budget_max",
            "past_works",
            "profile_map",
            "profile_map_generated_at",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "contractor_ad",
            "contractor_sozlesme_sayisi",
            "profile_map",
            "profile_map_generated_at",
            "created_at",
            "updated_at",
        ]


    def validate_website(self, value):
        """
        Web sitesini normalize eder: şema yoksa `https://` ekler.

        Kullanıcı sihirbazda "ornekinsaat.com.tr" yazıyor — bu geçerli bir girdidir,
        eksik olan yalnızca şemadır. Reddetmek yerine tamamlıyoruz; ancak gerçekten
        bozuk bir değeri (boşluklu metin, nokta içermeyen kelime) yine de eleriz ki
        profil haritası üretimi anlamsız bir adrese istek atmasın.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.core.validators import URLValidator

        url = (value or "").strip()
        if not url:
            return ""
        # Şema kontrolü küçük harfe indirgenmiş kopyada yapılır: "HTTP://…" de
        # geçerli bir adrestir, başına ikinci bir şema eklenmemeli.
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")

        try:
            URLValidator()(url)
        except DjangoValidationError:
            raise serializers.ValidationError(
                "Web sitesi adresi anlaşılamadı. Örnek: ornekinsaat.com.tr"
            ) from None
        return url[:300]


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ["id", "conversation", "role", "content", "payload", "created_at"]
        read_only_fields = fields


class ChatConversationSerializer(serializers.ModelSerializer):
    last_message = serializers.SerializerMethodField()
    message_count = serializers.SerializerMethodField()

    class Meta:
        model = ChatConversation
        fields = [
            "id", "title", "kind", "tender_ikn",
            "last_message", "message_count", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_last_message(self, obj) -> str:
        last = obj.messages.order_by("-created_at").values_list("content", flat=True).first()
        return (last or "")[:140]

    def get_message_count(self, obj) -> int:
        return obj.messages.count()


class ChatSendSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=2000, trim_whitespace=True)
    # Boş/gönderilmemiş → yeni konuşma açılır
    conversation = serializers.IntegerField(required=False, allow_null=True)
    # Doluysa (ekap_id veya İKN) YENİ konuşma o ihale odaklı açılır
    tender = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class TenderRecommendationSerializer(serializers.ModelSerializer):
    ikn = serializers.CharField(source="tender.ikn", read_only=True)
    ekap_id = serializers.CharField(source="tender.ekap_id", read_only=True)
    ihale_adi = serializers.CharField(source="tender.ihale_adi", read_only=True)
    idare_adi = serializers.CharField(source="tender.idare_adi", read_only=True)
    il = serializers.CharField(source="tender.ihale_il_adi", read_only=True)
    ihale_tarihi = serializers.DateTimeField(source="tender.ihale_tarihi", read_only=True)
    ihale_tip = serializers.IntegerField(source="tender.ihale_tip", read_only=True)

    class Meta:
        model = TenderRecommendation
        fields = [
            "id",
            "score",
            "reasons",
            "date",
            "seen",
            "ikn",
            "ekap_id",
            "ihale_adi",
            "idare_adi",
            "il",
            "ihale_tarihi",
            "ihale_tip",
        ]
        read_only_fields = fields
