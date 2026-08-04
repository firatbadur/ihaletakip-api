"""tenders view'ları — favoriler, filtreler, kayıtlı ihaleler, klasörler, alarmlar, bildirimler."""
from django.db.models import Count
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import generics, permissions, serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.premium import MSG_ALARM, MSG_FILTER_ALARM, require_premium

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
from .serializers import (
    FavoriteAuthoritySerializer,
    FavoriteContractorSerializer,
    FavoriteSerializer,
    NotificationSerializer,
    SavedFilterSerializer,
    SavedTenderSerializer,
    TenderAlarmSerializer,
    TenderGroupSerializer,
)

# Tüm tenders uçları kullanıcıya özeldir; kayıtlar otomatik olarak istekteki
# kullanıcıya bağlanır ve yalnızca kendi kayıtları listelenir.
_TENDER_ID_PARAM = OpenApiParameter(
    name="tender_id",
    location=OpenApiParameter.PATH,
    type=str,
    required=True,
    description="İhalenin EKAP iç kimliği (`ekap_id`).",
    examples=[OpenApiExample("EKAP iç kimliği", value="1234567")],
)

_IKN_PARAM = OpenApiParameter(
    name="ikn",
    location=OpenApiParameter.PATH,
    type=str,
    required=True,
    description=(
        "İhale Kayıt Numarası (ör. `2025/1234567`). İçerdiği `/` olduğu gibi "
        "gönderilebilir; kodlanmış biçim (`2025%2F1234567`) de kabul edilir."
    ),
    examples=[OpenApiExample("İKN", value="2025/1234567")],
)

_UPDATED_RESPONSE = inline_serializer(
    name="UpdatedCount", fields={"updated": serializers.IntegerField()}
)


class OwnerQuerysetMixin:
    """İstekteki kullanıcıya ait kayıtları filtreler ve otomatik atar."""

    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return self.queryset_model.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


# ── Favoriler ──────────────────────────────────────────
@extend_schema_view(
    get=extend_schema(
        tags=["favorites"],
        summary="Favorileri listele",
        description="Oturum açmış kullanıcının favori ihalelerini döner.",
    ),
    post=extend_schema(
        tags=["favorites"],
        summary="Favoriye ekle",
        description=(
            "İhaleyi favorilere ekler. Aynı `tender_id` tekrar gönderilirse kayıt "
            "**güncellenir** (upsert), hata dönmez."
        ),
        examples=[
            OpenApiExample(
                "Favori ekle",
                request_only=True,
                value={
                    "tender_id": "1234567",
                    "tender_title": "Bilgisayar ve Çevre Birimi Alımı",
                    "tender_type": "Mal Alımı",
                    "source": "ekap",
                },
            )
        ],
    ),
)
class FavoriteListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = FavoriteSerializer
    queryset_model = Favorite

    def perform_create(self, serializer):
        # Aynı ihale tekrar eklenirse günceller (upsert)
        Favorite.objects.update_or_create(
            user=self.request.user,
            tender_id=serializer.validated_data["tender_id"],
            defaults=serializer.validated_data,
        )


@extend_schema(tags=["favorites"], parameters=[_TENDER_ID_PARAM])
class FavoriteDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Favoriden çıkar",
        description="İhaleyi favorilerden siler. Kayıt yoksa da `204` döner (idempotent).",
        responses={204: None},
    )
    def delete(self, request, tender_id):
        Favorite.objects.filter(user=request.user, tender_id=tender_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Favoride mi?",
        description="İhalenin kullanıcının favorilerinde olup olmadığını döner.",
        responses={
            200: inline_serializer(
                name="IsFavorite", fields={"is_favorite": serializers.BooleanField()}
            )
        },
    )
    def get(self, request, tender_id):
        exists = Favorite.objects.filter(
            user=request.user, tender_id=tender_id
        ).exists()
        return Response({"is_favorite": exists})


# ── Favori İdareler ────────────────────────────────────
_DETSIS_NO_PARAM = OpenApiParameter(
    name="detsis_no",
    location=OpenApiParameter.PATH,
    type=str,
    required=True,
    description="İdarenin DETSIS ağaç anahtarı (`detsis_no`).",
    examples=[OpenApiExample("DETSIS no", value="24308110")],
)


def _enrich_authority(detsis_no):
    """`detsis_no`'dan `ekap.Authority` bulup ad/idare_id/has_items döndürür (yoksa boş)."""
    from ekap.models import Authority

    a = Authority.objects.filter(detsis_no=detsis_no).first()
    if not a:
        return {}
    return {"ad": a.ad, "idare_id": a.idare_id or None, "has_items": a.has_items}


@extend_schema_view(
    get=extend_schema(
        tags=["favorites"],
        summary="Favori idareleri listele",
        description="Oturum açmış kullanıcının favori idarelerini (DETSIS kurum) döner.",
    ),
    post=extend_schema(
        tags=["favorites"],
        summary="İdareyi favoriye ekle",
        description=(
            "İdareyi favorilere ekler. Yalnızca `detsis_no` gönderin; `ad`, `idare_id` "
            "ve `has_items` sunucuda `ekap.Authority`'den doldurulur. Aynı `detsis_no` "
            "tekrar gönderilirse kayıt **güncellenir** (upsert), hata dönmez. Sınır yoktur "
            "(Free + Pro).\n\n"
            "`alarm` (varsayılan `true`): açıkken bu idare **yeni bir ihale yayınladığında** "
            "kullanıcıya bildirim gider; bildirime basınca o idarenin ihale listesi açılır. "
            "Yalnızca hızlı erişim için favorilemek isteyen kullanıcı `alarm:false` gönderebilir."
        ),
        examples=[
            OpenApiExample(
                "İdareyi favoriye ekle",
                request_only=True,
                value={"detsis_no": "24308110", "alarm": True},
            )
        ],
    ),
)
class FavoriteAuthorityListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = FavoriteAuthoritySerializer
    queryset_model = FavoriteAuthority

    def perform_create(self, serializer):
        # Sınır yok (Free dahil). Aynı idare tekrar eklenirse günceller (upsert);
        # ad/idare_id/has_items DB'den zenginleşir, alarm tercihi korunur.
        detsis_no = serializer.validated_data["detsis_no"]
        defaults = _enrich_authority(detsis_no)
        defaults["alarm"] = serializer.validated_data.get("alarm", True)
        FavoriteAuthority.objects.update_or_create(
            user=self.request.user,
            detsis_no=detsis_no,
            defaults=defaults,
        )


@extend_schema_view(
    get=extend_schema(
        tags=["favorites"],
        summary="Takip edilen firmaları listele",
        description="Kullanıcının takip ettiği yüklenici firmalar (en yeni önce).",
    ),
    post=extend_schema(
        tags=["favorites"],
        summary="Firmayı takibe al",
        description=(
            "Gövde: `{\"contractor\": <id>, \"alarm\": true}`. Aynı firma tekrar "
            "gönderilirse **upsert** edilir (hata yok).\n\n"
            "Takip etmek **her üyeye açıktır ve sınırsızdır**; `alarm` açıkken firma yeni "
            "bir iş aldığında gelen **bildirim Pro'ya özeldir** (favori idaredeki asimetrinin "
            "aynısı — kaydetmek serbest, bildirim Pro)."
        ),
        examples=[OpenApiExample("Takibe al", value={"contractor": 1234, "alarm": True})],
    ),
)
class FavoriteContractorListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = FavoriteContractorSerializer
    queryset_model = FavoriteContractor

    def get_queryset(self):
        # `select_related`: serializer firma adı/istatistiklerini okuyor → satır başına
        # ek sorgu olmasın.
        return super().get_queryset().select_related("contractor")

    def perform_create(self, serializer):
        # Sınır yok (Free dahil). Aynı firma tekrar eklenirse alarm tercihi güncellenir.
        FavoriteContractor.objects.update_or_create(
            user=self.request.user,
            contractor=serializer.validated_data["contractor"],
            defaults={"alarm": serializer.validated_data.get("alarm", True)},
        )


@extend_schema(
    tags=["favorites"],
    parameters=[
        OpenApiParameter(
            name="contractor_id", location=OpenApiParameter.PATH, type=int, required=True,
            description="`ekap.Contractor` kimliği.",
        )
    ],
)
class FavoriteContractorDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Firmayı takipten çıkar",
        description="Kayıt yoksa da `204` döner (idempotent).",
        responses={204: None},
    )
    def delete(self, request, contractor_id):
        FavoriteContractor.objects.filter(
            user=request.user, contractor_id=contractor_id
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Firma takipte mi?",
        responses={
            200: inline_serializer(
                name="IsFavoriteContractor",
                fields={"is_favorite": serializers.BooleanField()},
            )
        },
    )
    def get(self, request, contractor_id):
        exists = FavoriteContractor.objects.filter(
            user=request.user, contractor_id=contractor_id
        ).exists()
        return Response({"is_favorite": exists})


@extend_schema(tags=["favorites"], parameters=[_DETSIS_NO_PARAM])
class FavoriteAuthorityDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="İdareyi favoriden çıkar",
        description="İdareyi favorilerden siler. Kayıt yoksa da `204` döner (idempotent).",
        responses={204: None},
    )
    def delete(self, request, detsis_no):
        FavoriteAuthority.objects.filter(
            user=request.user, detsis_no=detsis_no
        ).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="İdare favoride mi?",
        description="İdarenin kullanıcının favorilerinde olup olmadığını döner.",
        responses={
            200: inline_serializer(
                name="IsFavoriteAuthority",
                fields={"is_favorite": serializers.BooleanField()},
            )
        },
    )
    def get(self, request, detsis_no):
        exists = FavoriteAuthority.objects.filter(
            user=request.user, detsis_no=detsis_no
        ).exists()
        return Response({"is_favorite": exists})


# ── Kayıtlı Filtreler ──────────────────────────────────
_SAVED_FILTER_EXAMPLE = OpenApiExample(
    "Arama filtresi kaydet",
    request_only=True,
    description=(
        "`filters` serbest JSON'dur; `GET /ekap/tenders/` query parametrelerini "
        "saklamak için kullanılır. `alarm=true` ise filtreye uyan yeni ihaleler "
        "için bildirim üretilir."
    ),
    value={
        "name": "Ankara bilgisayar alımları",
        "filters": {"q": "bilgisayar", "il": "251", "tur": "1"},
        "tags": ["donanım", "ankara"],
        "alarm": True,
    },
)

_FILTER_ID_PARAM = OpenApiParameter(
    name="id", location=OpenApiParameter.PATH, type=int, required=True,
    description="Kayıtlı filtrenin veritabanı kimliği.",
    examples=[OpenApiExample("Filtre id", value=1)],
)


@extend_schema_view(
    get=extend_schema(
        tags=["saved-filters"],
        summary="Kayıtlı filtreleri listele",
        description="Kullanıcının kaydettiği arama filtrelerini döner.",
    ),
    post=extend_schema(
        tags=["saved-filters"],
        summary="Filtre kaydet",
        description=(
            "Yeni bir arama filtresi kaydeder. Filtre kaydetmenin **sınırı yoktur** "
            "(Free + Pro).\n\n"
            "**Filtre alarmı Pro'ya özeldir:** `alarm` açık gönderilirse ve üyelik Free "
            "ise **403** döner (`errors.code = premium_required`). Alarmsız kaydetmek "
            "serbesttir; alarm (uygun yeni ihale bildirimi) için Pro gerekir."
        ),
        examples=[_SAVED_FILTER_EXAMPLE],
    ),
)
class SavedFilterListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = SavedFilterSerializer
    queryset_model = SavedFilter

    def perform_create(self, serializer):
        # Filtre kaydetme serbest; ancak ALARM açıksa Pro gerekir (yeni ihale bildirimi).
        from .tasks import _alarm_enabled

        if _alarm_enabled(serializer.validated_data.get("alarm")):
            require_premium(self.request.user, MSG_FILTER_ALARM)
        serializer.save(user=self.request.user)


@extend_schema_view(
    get=extend_schema(
        tags=["saved-filters"], summary="Filtreyi getir",
        parameters=[_FILTER_ID_PARAM],
        description="Tek bir kayıtlı filtreyi döner.",
    ),
    put=extend_schema(
        tags=["saved-filters"], summary="Filtreyi değiştir",
        parameters=[_FILTER_ID_PARAM], examples=[_SAVED_FILTER_EXAMPLE],
        description=(
            "Filtreyi tamamen değiştirir — tüm alanlar gönderilmelidir. Sonuçta `alarm` "
            "açık kalır/olursa **Pro** gerekir (Free → 403)."
        ),
    ),
    patch=extend_schema(
        tags=["saved-filters"], summary="Filtreyi kısmi güncelle",
        parameters=[_FILTER_ID_PARAM], examples=[_SAVED_FILTER_EXAMPLE],
        description=(
            "Yalnızca gönderilen alanları günceller. Sonuçta `alarm` açık kalır/olursa "
            "**Pro** gerekir (Free → 403). Alarmı **kapatmak** her üyeye serbesttir."
        ),
    ),
    delete=extend_schema(
        tags=["saved-filters"], summary="Filtreyi sil",
        parameters=[_FILTER_ID_PARAM], responses={204: None},
        description="Filtreyi siler.",
    ),
)
class SavedFilterDetailView(OwnerQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = SavedFilterSerializer
    queryset_model = SavedFilter

    def perform_update(self, serializer):
        # Güncelleme sonrası ALARM açık kalıyorsa Pro gerekir. PATCH'te alarm gönderilmediyse
        # mevcut (instance) değeri temel alınır → alarmı kapatmak serbest, açık tutmak Pro.
        from .tasks import _alarm_enabled

        alarm = serializer.validated_data.get("alarm", getattr(serializer.instance, "alarm", None))
        if _alarm_enabled(alarm):
            require_premium(self.request.user, MSG_FILTER_ALARM)
        serializer.save()


# ── Kayıtlı İhale Klasörleri ───────────────────────────
# Varsayılan klasör ("Genel") bir satır DEĞİLDİR: `SavedTender.group is None`
# demektir, bu uçlarda dönmez ve oluşturulamaz. Mobil listenin başına ekler.
@extend_schema_view(
    get=extend_schema(
        tags=["tender-groups"],
        summary="Klasörleri listele",
        description=(
            "Kullanıcının kayıtlı ihale klasörlerini (her birinin ihale sayısıyla) döner. "
            "Varsayılan **Genel** klasörü listeye dahil değildir; `group` alanı boş olan "
            "kayıtlar oraya aittir."
        ),
    ),
    post=extend_schema(
        tags=["tender-groups"],
        summary="Klasör oluştur",
        description=(
            f"Yeni klasör açar. Ad benzersiz olmalıdır (büyük/küçük harf duyarsız), "
            f'"{DEFAULT_TENDER_GROUP_NAME}" adı kullanılamaz ve kullanıcı başına en fazla '
            f"{MAX_TENDER_GROUPS} klasör açılabilir. Sınır yoktur (Free + Pro)."
        ),
        examples=[
            OpenApiExample("Klasör oluştur", request_only=True, value={"name": "Ankara İşleri"})
        ],
    ),
)
class TenderGroupListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = TenderGroupSerializer
    queryset_model = TenderGroup

    def get_queryset(self):
        return super().get_queryset().annotate(tender_count=Count("tenders"))


@extend_schema_view(
    get=extend_schema(
        tags=["tender-groups"], summary="Klasör detayı", description="Tek klasörü döner."
    ),
    patch=extend_schema(
        tags=["tender-groups"],
        summary="Klasörü yeniden adlandır",
        examples=[
            OpenApiExample("Yeniden adlandır", request_only=True, value={"name": "Yapım İşleri"})
        ],
    ),
    put=extend_schema(tags=["tender-groups"], summary="Klasörü güncelle (tam)"),
    delete=extend_schema(
        tags=["tender-groups"],
        summary="Klasörü sil",
        description=(
            "Klasörü siler. **İçindeki kayıtlar silinmez**, varsayılan **Genel** "
            "klasörüne döner (`group` → null)."
        ),
        responses={204: None},
    ),
)
class TenderGroupDetailView(OwnerQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = TenderGroupSerializer
    queryset_model = TenderGroup

    def get_object(self):
        row = self.get_queryset().filter(pk=self.kwargs["pk"]).first()
        if row is None:
            raise NotFound("Klasör bulunamadı.")
        return row


# ── Kayıtlı İhaleler ───────────────────────────────────
@extend_schema_view(
    get=extend_schema(
        tags=["saved-tenders"],
        summary="Kayıtlı ihaleleri listele",
        description="Kullanıcının kaydettiği ihaleleri döner.",
    ),
    post=extend_schema(
        tags=["saved-tenders"],
        summary="İhaleyi kaydet",
        description=(
            "İhaleyi kayıtlılara ekler. Aynı `tender_ikn` tekrar gönderilirse kayıt "
            "güncellenir (upsert). Sınır yoktur (Free + Pro)."
        ),
        examples=[
            OpenApiExample(
                "İhaleyi kaydet",
                request_only=True,
                description="Aynı `tender_ikn` tekrar gönderilirse kayıt güncellenir (upsert).",
                value={
                    "tender_id": "1234567",
                    "tender_ikn": "2025/1234567",
                    "tender_title": "Bilgisayar ve Çevre Birimi Alımı",
                    "tender_type": "Mal Alımı",
                    "tender_status": "Katılıma Açık",
                    "tender_city": "ANKARA",
                    "tender_date": "23.03.2027 14:00",
                    "institution": "Ankara Büyükşehir Belediyesi",
                },
            )
        ],
    ),
)
class SavedTenderListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = SavedTenderSerializer
    queryset_model = SavedTender

    def get_queryset(self):
        return super().get_queryset().select_related("group")

    def perform_create(self, serializer):
        # Sınır yok (Free dahil). Aynı İKN tekrar gönderilirse günceller (upsert).
        # `group` gönderilmezse mevcut klasör korunur (yeni kayıtta null = "Genel").
        SavedTender.objects.update_or_create(
            user=self.request.user,
            tender_ikn=serializer.validated_data["tender_ikn"],
            defaults=serializer.validated_data,
        )


@extend_schema(tags=["saved-tenders"], parameters=[_IKN_PARAM])
class SavedTenderDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Kaydı sil",
        description="İhaleyi kayıtlılardan siler. Kayıt yoksa da `204` döner (idempotent).",
        responses={204: None},
    )
    def delete(self, request, ikn):
        SavedTender.objects.filter(user=request.user, tender_ikn=ikn).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Kayıtlı mı? (hangi klasörde?)",
        description=(
            "İhalenin kullanıcının kayıtlıları arasında olup olmadığını döner. "
            "Kayıtlıysa bulunduğu klasör de gelir; `group: null` → varsayılan "
            "**Genel** klasörü."
        ),
        responses={
            200: inline_serializer(
                name="IsSaved",
                fields={
                    "is_saved": serializers.BooleanField(),
                    "group": serializers.IntegerField(allow_null=True),
                    "group_name": serializers.CharField(allow_null=True),
                },
            )
        },
    )
    def get(self, request, ikn):
        row = (
            SavedTender.objects.filter(user=request.user, tender_ikn=ikn)
            .select_related("group")
            .first()
        )
        return Response(
            {
                "is_saved": row is not None,
                "group": row.group_id if row else None,
                "group_name": row.group.name if row and row.group else None,
            }
        )

    @extend_schema(
        summary="Kaydı klasöre taşı",
        description=(
            "Kayıtlı ihalenin klasörünü değiştirir. `group` gövdede klasör kimliği "
            "veya `null` (varsayılan **Genel** klasörü) olmalıdır. Kayıt yoksa `404`."
        ),
        request=inline_serializer(
            name="SavedTenderMove",
            fields={"group": serializers.IntegerField(allow_null=True)},
        ),
        responses={200: SavedTenderSerializer},
        examples=[
            OpenApiExample("Klasöre taşı", request_only=True, value={"group": 3}),
            OpenApiExample(
                "Genel'e taşı (varsayılan)", request_only=True, value={"group": None}
            ),
        ],
    )
    def patch(self, request, ikn):
        row = SavedTender.objects.filter(user=request.user, tender_ikn=ikn).first()
        if row is None:
            raise NotFound("Kayıtlı ihale bulunamadı.")
        serializer = SavedTenderSerializer(
            row, data={"group": request.data.get("group")}, partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ── Alarmlar ───────────────────────────────────────────
@extend_schema_view(
    get=extend_schema(
        tags=["alarms"],
        summary="Alarmları listele",
        description="Kullanıcının kurduğu ihale alarmlarını döner.",
    ),
    post=extend_schema(
        tags=["alarms"],
        summary="Alarm kur",
        description=(
            "İhale için alarm kurar.\n\n"
            "**İhale alarmları Pro aboneliğe özeldir.** Free üyelik alarm kuramaz → **403** "
            "döner (`errors.code = premium_required`); mobil abonelik paketlerini sunar. "
            "Alarmları listeleme/silme her üyeye açıktır."
        ),
        examples=[
            OpenApiExample(
                "İhale alarmı kur",
                request_only=True,
                description=(
                    "`reminder_day` ihale gününde, `document_change` doküman değişince "
                    "bildirim üretir. Alarmlar saatlik Celery görevi ile kontrol edilir. "
                    "Aynı `tender_id` tekrar gönderilirse kayıt güncellenir (upsert)."
                ),
                value={
                    "tender_id": "1234567",
                    "tender_ikn": "2025/1234567",
                    "tender_title": "Bilgisayar ve Çevre Birimi Alımı",
                    "institution": "Ankara Büyükşehir Belediyesi",
                    "reminder_day": True,
                    "document_change": True,
                },
            )
        ],
    ),
)
class TenderAlarmListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = TenderAlarmSerializer
    queryset_model = TenderAlarm

    def perform_create(self, serializer):
        # İhale alarmı kurma Pro'ya özeldir (kurma/güncelleme kilitli; listeleme/silme serbest).
        require_premium(self.request.user, MSG_ALARM)
        TenderAlarm.objects.update_or_create(
            user=self.request.user,
            tender_id=serializer.validated_data["tender_id"],
            defaults=serializer.validated_data,
        )


@extend_schema(tags=["alarms"], parameters=[_TENDER_ID_PARAM])
class TenderAlarmDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Alarmı sil",
        description="İhale alarmını siler. Kayıt yoksa da `204` döner (idempotent).",
        responses={204: None},
    )
    def delete(self, request, tender_id):
        TenderAlarm.objects.filter(user=request.user, tender_id=tender_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Alarmı getir",
        description="İhaleye kurulu alarmı döner. Alarm yoksa `data` alanı `null` olur.",
        responses={200: TenderAlarmSerializer},
    )
    def get(self, request, tender_id):
        alarm = TenderAlarm.objects.filter(
            user=request.user, tender_id=tender_id
        ).first()
        if not alarm:
            return Response(None)
        return Response(TenderAlarmSerializer(alarm).data)


# ── Bildirimler ────────────────────────────────────────
@extend_schema(
    tags=["notifications"],
    summary="Bildirimleri listele",
    description=(
        "Kullanıcının bildirimlerini döner (okunmuş + okunmamış). Bildirimler alarm "
        "ve kayıtlı filtre eşleşmelerinden üretilir; eski bildirimler günlük Celery "
        "görevi ile temizlenir."
    ),
)
class NotificationListView(OwnerQuerysetMixin, generics.ListAPIView):
    serializer_class = NotificationSerializer
    queryset_model = Notification


@extend_schema(
    tags=["notifications"],
    summary="Bildirimi okundu işaretle",
    description=(
        "Tek bir bildirimi okundu yapar. `data.updated` güncellenen kayıt sayısıdır — "
        "bildirim yoksa veya başkasına aitse `0` döner (hata değil)."
    ),
    parameters=[
        OpenApiParameter(
            name="notification_id", location=OpenApiParameter.PATH, type=int,
            required=True, description="Bildirimin veritabanı kimliği.",
            examples=[OpenApiExample("Bildirim id", value=1)],
        )
    ],
    request=None,
    responses={200: _UPDATED_RESPONSE},
)
class NotificationReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, notification_id):
        updated = Notification.objects.filter(
            user=request.user, id=notification_id
        ).update(read=True)
        return Response({"updated": updated})


@extend_schema(
    tags=["notifications"],
    summary="Tümünü okundu işaretle",
    description=(
        "Kullanıcının okunmamış tüm bildirimlerini okundu yapar. Gövde gerektirmez. "
        "`data.updated` güncellenen kayıt sayısıdır."
    ),
    request=None,
    responses={200: _UPDATED_RESPONSE},
)
class NotificationReadAllView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        updated = Notification.objects.filter(
            user=request.user, read=False
        ).update(read=True)
        return Response({"updated": updated})


@extend_schema(
    tags=["notifications"],
    summary="Okunmamış bildirim sayısı",
    description="Rozet (badge) göstermek için okunmamış bildirim sayısını döner.",
    responses={
        200: inline_serializer(
            name="UnreadCount", fields={"unread": serializers.IntegerField()}
        )
    },
)
class NotificationUnreadCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(user=request.user, read=False).count()
        return Response({"unread": count})
