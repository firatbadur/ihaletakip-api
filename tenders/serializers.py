"""tenders serializer'ları."""
from rest_framework import serializers

from .models import (
    DEFAULT_TENDER_GROUP_NAME,
    MAX_TENDER_GROUPS,
    DtKayitliFiltre,
    Favorite,
    FavoriteAuthority,
    FavoriteContractor,
    Notification,
    SavedDogrudanTemin,
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


class DtKayitliFiltreSerializer(serializers.ModelSerializer):
    """
    Doğrudan temin kayıtlı filtresi.

    ⚠️ Tanınmayan filtre anahtarı REDDEDİLİR (400): yok saymak, filtreyi kullanıcının
    sandığından geniş yapar ve alarmı "her şeye" uyan bir alarma çevirirdi. Mobilde bir
    anahtar yazım hatası bu yüzden sessiz değil, görünür bir hatadır.
    """

    class Meta:
        model = DtKayitliFiltre
        fields = ["id", "ad", "filters", "alarm", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_filters(self, deger):
        from ekap.dt_sorgu import FILTRE_ANAHTARLARI

        if deger in (None, ""):
            return {}
        if not isinstance(deger, dict):
            raise serializers.ValidationError("Filtre bir nesne olmalı.")
        bilinmeyen = sorted(set(deger) - set(FILTRE_ANAHTARLARI))
        if bilinmeyen:
            raise serializers.ValidationError(
                f"Tanınmayan filtre alanı: {', '.join(bilinmeyen)}. "
                f"Geçerli alanlar: {', '.join(FILTRE_ANAHTARLARI)}."
            )
        return deger


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
    # Klasördeki kayıtlı doğrudan teminler. ⚠️ `tender_count`'a EKLENMEDİ: web
    # "Kaydedilenler" yalnızca ihaleleri listeliyor, toplam orada tutmazdı.
    # Mobil ikisini toplayıp gösterir.
    dt_count = serializers.SerializerMethodField()
    # `allow_blank` → boş ad hatasını DRF'in alan doğrulaması yerine aşağıdaki
    # `validate()` üretir; böylece mobile "name: ..." öneksiz Türkçe mesaj gider.
    name = serializers.CharField(max_length=60, allow_blank=True)

    class Meta:
        model = TenderGroup
        fields = ["id", "name", "tender_count", "dt_count", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_tender_count(self, obj):
        # Liste ucunda `annotate(tender_count=...)` ile gelir; tekil kayıtta sayılır.
        count = getattr(obj, "tender_count", None)
        return count if count is not None else obj.tenders.count()

    def get_dt_count(self, obj):
        count = getattr(obj, "dt_count", None)
        return count if count is not None else obj.dogrudan_teminler.count()

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


class SavedDogrudanTeminSerializer(serializers.ModelSerializer):
    """
    Kayıtlı doğrudan temin. Kart alanları kaydetme anındaki kopyadır; DT satırı hâlâ
    duruyorsa (`context["canli"]` = {dtn: DogrudanTemin}) güncel değer kullanılır —
    düzeltme ilanıyla son teklif değişebilir. `kalan_dakika` SUNUCU saatinden
    (istemci saatine güvenilmez, DT listesiyle aynı sözleşme). `acik`: alım hâlâ
    teklif verilebilir mi (satır silinmişse ya da süresi geçmişse false).
    """

    group = serializers.PrimaryKeyRelatedField(
        queryset=TenderGroup.objects.all(), required=False, allow_null=True
    )
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)
    tur_adi = serializers.SerializerMethodField()
    kalan_dakika = serializers.SerializerMethodField()
    acik = serializers.SerializerMethodField()

    class Meta:
        model = SavedDogrudanTemin
        fields = [
            "id",
            "dtn",
            "ad",
            "idare_adi",
            "il_adi",
            "tur",
            "tur_adi",
            "madde",
            "son_teklif",
            "kalan_dakika",
            "acik",
            "dokuman_var",
            "group",
            "group_name",
            "saved_at",
        ]
        read_only_fields = [
            "id", "ad", "idare_adi", "il_adi", "tur", "madde", "son_teklif",
            "dokuman_var", "saved_at",
        ]

    def validate_group(self, value):
        # Başka kullanıcının klasörüne kayıt atanamaz.
        if value is not None and value.user_id != self.context["request"].user.id:
            raise serializers.ValidationError("Klasör bulunamadı.")
        return value

    def _canli(self, obj):
        return (self.context.get("canli") or {}).get(obj.dtn)

    def _son_teklif(self, obj):
        dt = self._canli(obj)
        return dt.son_teklif if dt is not None else obj.son_teklif

    def to_representation(self, obj):
        data = super().to_representation(obj)
        dt = self._canli(obj)
        if dt is not None:
            data["son_teklif"] = serializers.DateTimeField().to_representation(dt.son_teklif) \
                if dt.son_teklif else None
            data["dokuman_var"] = dt.dokuman_var
        return data

    def get_tur_adi(self, obj):
        from ekap.mobil.constants import DT_TUR

        return DT_TUR.get(obj.tur, "")

    def get_kalan_dakika(self, obj):
        from django.utils import timezone

        son = self._son_teklif(obj)
        if son is None:
            return None
        return max(0, int((son - timezone.now()).total_seconds() // 60))

    def get_acik(self, obj):
        from django.utils import timezone

        dt = self._canli(obj)
        if dt is None:
            return False
        return dt.son_teklif is None or dt.son_teklif > timezone.now()


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
            # KİK uyuşmazlık kararı alarmı (vars. kapalı).
            "kurul_karar",
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
            # Filtre bildiriminde: bildirimin kapsadığı yayım günü (YYYY-MM-DD).
            # Mobil listeyi tam bu güne kısar; `null` ise eski davranışa (bildirimin
            # oluşma günü) düşer.
            "ilan_gun",
            # Filtre bildiriminde: sayılan **kayıt tarihi** penceresi (ISO). Mobil
            # listeyi `created_at_min`/`created_at_max` ile tam buna kısar.
            "pencere_bas",
            "pencere_bit",
            # Birleşik özet: eşleşen filtre / idare id'leri (CSV). Mobil bunları
            # `kayitli_filtreler` / `favori_idareler` olarak uca gönderir.
            "filtre_idler",
            "idare_detsis_liste",
            # Doğrudan temin alarmı: kapsanan DT numaraları (CSV). Mobil
            # `GET /ekap/dogrudan-temin/?dtn=<dtn_liste>` açar.
            "dtn_liste",
            # Kurul kararı alarmında: bildirilen kararın numarası.
            "karar_no",
            "read",
            "created_at",
        ]
        read_only_fields = fields
