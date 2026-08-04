"""tenders serializer'ları."""
from rest_framework import serializers

from .models import (
    DEFAULT_TENDER_GROUP_NAME,
    MAX_TENDER_GROUPS,
    Favorite,
    FavoriteAuthority,
    FavoriteContractor,
    Notification,
    SavedFilter,
    SavedTender,
    TenderAlarm,
    TenderGroup,
)


class FavoriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Favorite
        fields = [
            "id",
            "tender_id",
            "tender_title",
            "tender_type",
            "source",
            "added_at",
        ]
        read_only_fields = ["id", "added_at"]


class FavoriteAuthoritySerializer(serializers.ModelSerializer):
    class Meta:
        model = FavoriteAuthority
        fields = ["id", "detsis_no", "alarm", "idare_id", "ad", "has_items", "added_at"]
        # `ad`/`idare_id`/`has_items` sunucuda `ekap.Authority`'den doldurulur;
        # mobil yalnızca `detsis_no` (+ ist/opsiyonel `alarm`) gönderir. `alarm`
        # varsayılan `True` → favorileyince yeni ihale bildirimi açık gelir.
        read_only_fields = ["id", "idare_id", "ad", "has_items", "added_at"]


class FavoriteContractorSerializer(serializers.ModelSerializer):
    """
    Takip edilen firma. Mobil yalnızca `contractor` (id) + opsiyonel `alarm` gönderir;
    ad/istatistik alanları sunucuda `ekap.Contractor`'dan zenginleştirilir (favori idarede
    olduğu gibi — istemci veriyi tekrar taşımasın).
    """

    ad = serializers.CharField(source="contractor.kanonik_ad", read_only=True)
    kind = serializers.CharField(source="contractor.kind", read_only=True)
    sozlesme_sayisi = serializers.IntegerField(
        source="contractor.sozlesme_sayisi", read_only=True
    )
    son_sozlesme_tarihi = serializers.DateTimeField(
        source="contractor.son_sozlesme_tarihi", read_only=True
    )

    class Meta:
        model = FavoriteContractor
        fields = [
            "id", "contractor", "alarm", "ad", "kind",
            "sozlesme_sayisi", "son_sozlesme_tarihi", "added_at",
        ]
        read_only_fields = [
            "id", "ad", "kind", "sozlesme_sayisi", "son_sozlesme_tarihi", "added_at",
        ]


class SavedFilterSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavedFilter
        fields = ["id", "name", "filters", "tags", "alarm", "created_at"]
        read_only_fields = ["id", "created_at"]


def _tr_fold(s):
    """
    Türkçe büyük/küçük harf duyarsız karşılaştırma anahtarı.

    `str.casefold()` yetmez: "İ".casefold() → "i̇" (i + U+0307) olduğundan
    "İşleri" ile "işleri" eşleşmez. Önce İ/I Türkçe karşılıklarına indirilir.
    """
    return s.replace("İ", "i").replace("I", "ı").lower()


class TenderGroupSerializer(serializers.ModelSerializer):
    """
    Kayıtlı ihale klasörü. Varsayılan klasör ("Genel") burada dönmez — o,
    `SavedTender.group is None` demektir; mobil listenin başına kendisi ekler.
    """

    tender_count = serializers.SerializerMethodField()
    # `allow_blank` → boş ad hatasını DRF'in alan doğrulaması yerine aşağıdaki
    # `validate()` üretir; böylece mobile "name: ..." öneksiz Türkçe mesaj gider.
    name = serializers.CharField(max_length=60, allow_blank=True)

    class Meta:
        model = TenderGroup
        fields = ["id", "name", "tender_count", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_tender_count(self, obj):
        # Liste ucunda `annotate(tender_count=...)` ile gelir; tekil kayıtta sayılır.
        count = getattr(obj, "tender_count", None)
        return count if count is not None else obj.tenders.count()

    def validate(self, attrs):
        # Mesajlar mobilde doğrudan kullanıcıya gösterilir → alan öneki olmasın diye
        # hatalar `detail` altında toplanır (bkz. core.response.extract_message).
        name = " ".join(str(attrs.get("name", "")).split())
        if not name:
            raise serializers.ValidationError({"detail": "Klasör adı boş olamaz."})
        if _tr_fold(name) == _tr_fold(DEFAULT_TENDER_GROUP_NAME):
            raise serializers.ValidationError(
                {"detail": f'"{DEFAULT_TENDER_GROUP_NAME}" varsayılan klasörün adıdır, kullanılamaz.'}
            )
        user = self.context["request"].user
        siblings = TenderGroup.objects.filter(user=user)
        if self.instance is not None:
            siblings = siblings.exclude(pk=self.instance.pk)
        elif siblings.count() >= MAX_TENDER_GROUPS:
            raise serializers.ValidationError(
                {"detail": f"En fazla {MAX_TENDER_GROUPS} klasör oluşturabilirsiniz."}
            )
        # Karşılaştırma Python'da (_tr_fold): veritabanı `iexact`'i Türkçe harflerde
        # büyük/küçük harf duyarsız değildir ("İşleri"/"işleri" eşleşmez). Klasör
        # sayısı kullanıcı başına 20 ile sınırlı → tam liste üzerinde döngü güvenli.
        folded = _tr_fold(name)
        if any(_tr_fold(g.name) == folded for g in siblings.only("name")):
            raise serializers.ValidationError({"detail": "Bu isimde bir klasör zaten var."})
        attrs["name"] = name
        return attrs


class SavedTenderSerializer(serializers.ModelSerializer):
    # Boş/null gönderilirse kayıt varsayılan "Genel" klasörüne düşer.
    group = serializers.PrimaryKeyRelatedField(
        queryset=TenderGroup.objects.all(), required=False, allow_null=True
    )
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)

    class Meta:
        model = SavedTender
        fields = [
            "id",
            "tender_id",
            "tender_ikn",
            "tender_title",
            "tender_type",
            "tender_status",
            "tender_city",
            "tender_date",
            "institution",
            "group",
            "group_name",
            "saved_at",
        ]
        read_only_fields = ["id", "saved_at"]

    def validate_group(self, value):
        # Başka kullanıcının klasörüne kayıt atanamaz.
        if value is not None and value.user_id != self.context["request"].user.id:
            raise serializers.ValidationError("Klasör bulunamadı.")
        return value


class TenderAlarmSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenderAlarm
        fields = [
            "id",
            "tender_id",
            "tender_ikn",
            "tender_title",
            "institution",
            "reminder_day",
            "document_change",
            "completed",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id",
            "type",
            "title",
            "body",
            "tender_id",
            "tender_title",
            "tender_ikn",
            "institution",
            # Derin bağlantı alanları — mobil önceliği: conversation_id > filter_id >
            # authority_detsis > contractor_id > okas_kodlar > tender_ikn/tender_id
            "conversation_id",
            "filter_id",
            "authority_detsis",
            "contractor_id",
            "okas_kodlar",
            "read",
            "created_at",
        ]
        read_only_fields = fields
