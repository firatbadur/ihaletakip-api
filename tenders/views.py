"""tenders view'ları — favoriler, filtreler, kayıtlı ihaleler, alarmlar, bildirimler."""
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.premium import (
    FREE_FAVORITE_AUTHORITY_LIMIT,
    FREE_SAVED_FILTER_LIMIT,
    FREE_SAVED_TENDER_LIMIT,
    MSG_FAVORITE_AUTHORITY_LIMIT,
    MSG_SAVED_FILTER_LIMIT,
    MSG_SAVED_TENDER_LIMIT,
    enforce_free_limit,
)

from .models import (
    Favorite,
    FavoriteAuthority,
    Notification,
    SavedFilter,
    SavedTender,
    TenderAlarm,
)
from .serializers import (
    FavoriteAuthoritySerializer,
    FavoriteSerializer,
    NotificationSerializer,
    SavedFilterSerializer,
    SavedTenderSerializer,
    TenderAlarmSerializer,
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
            "tekrar gönderilirse kayıt **güncellenir** (upsert), hata dönmez.\n\n"
            f"**Ücretsiz (Free) üyelik:** en fazla {FREE_FAVORITE_AUTHORITY_LIMIT} idare "
            "favorilenebilir. Sınır aşılırsa **403** döner "
            "(`errors.code = premium_required`). Zaten favorideki bir idarenin güncellenmesi "
            "(upsert) limiti tetiklemez. Pro üyelikte sınır yoktur."
        ),
        examples=[
            OpenApiExample(
                "İdareyi favoriye ekle",
                request_only=True,
                value={"detsis_no": "24308110"},
            )
        ],
    ),
)
class FavoriteAuthorityListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = FavoriteAuthoritySerializer
    queryset_model = FavoriteAuthority

    def perform_create(self, serializer):
        user = self.request.user
        detsis_no = serializer.validated_data["detsis_no"]
        # Free limiti yalnızca YENİ bir favori için uygulanır; mevcut idarenin güncellemesi
        # (upsert) sayılmaz. Aynı idare tekrar eklenirse günceller; ad/idare_id DB'den zenginleşir.
        if not FavoriteAuthority.objects.filter(user=user, detsis_no=detsis_no).exists():
            enforce_free_limit(
                user,
                current_count=FavoriteAuthority.objects.filter(user=user).count(),
                limit=FREE_FAVORITE_AUTHORITY_LIMIT,
                message=MSG_FAVORITE_AUTHORITY_LIMIT,
            )
        FavoriteAuthority.objects.update_or_create(
            user=user,
            detsis_no=detsis_no,
            defaults=_enrich_authority(detsis_no),
        )


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
            "Yeni bir arama filtresi kaydeder.\n\n"
            f"**Ücretsiz (Free) üyelik:** en fazla {FREE_SAVED_FILTER_LIMIT} filtre "
            "kaydedilebilir. Sınır aşılırsa **403** döner "
            "(`errors.code = premium_required`) — mobil abonelik paketlerini sunar. "
            "Pro üyelikte sınır yoktur."
        ),
        examples=[_SAVED_FILTER_EXAMPLE],
    ),
)
class SavedFilterListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = SavedFilterSerializer
    queryset_model = SavedFilter

    def perform_create(self, serializer):
        # Free üyelik filtre limiti (her POST yeni bir filtredir; upsert yok).
        enforce_free_limit(
            self.request.user,
            current_count=SavedFilter.objects.filter(user=self.request.user).count(),
            limit=FREE_SAVED_FILTER_LIMIT,
            message=MSG_SAVED_FILTER_LIMIT,
        )
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
        description="Filtreyi tamamen değiştirir — tüm alanlar gönderilmelidir.",
    ),
    patch=extend_schema(
        tags=["saved-filters"], summary="Filtreyi kısmi güncelle",
        parameters=[_FILTER_ID_PARAM], examples=[_SAVED_FILTER_EXAMPLE],
        description="Yalnızca gönderilen alanları günceller.",
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
            "İhaleyi kayıtlılara ekler.\n\n"
            f"**Ücretsiz (Free) üyelik:** en fazla {FREE_SAVED_TENDER_LIMIT} ihale "
            "kaydedilebilir. Sınır aşılırsa **403** döner "
            "(`errors.code = premium_required`). Zaten kayıtlı bir İKN'nin "
            "güncellenmesi (upsert) limiti tetiklemez. Pro üyelikte sınır yoktur."
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

    def perform_create(self, serializer):
        user = self.request.user
        ikn = serializer.validated_data["tender_ikn"]
        # Free limiti yalnızca YENİ bir kayıt için uygulanır; mevcut İKN güncellemesi
        # (upsert) sayılmaz — kullanıcı zaten kayıtlı ihalesini serbestçe güncelleyebilir.
        if not SavedTender.objects.filter(user=user, tender_ikn=ikn).exists():
            enforce_free_limit(
                user,
                current_count=SavedTender.objects.filter(user=user).count(),
                limit=FREE_SAVED_TENDER_LIMIT,
                message=MSG_SAVED_TENDER_LIMIT,
            )
        SavedTender.objects.update_or_create(
            user=user,
            tender_ikn=ikn,
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
        summary="Kayıtlı mı?",
        description="İhalenin kullanıcının kayıtlıları arasında olup olmadığını döner.",
        responses={
            200: inline_serializer(
                name="IsSaved", fields={"is_saved": serializers.BooleanField()}
            )
        },
    )
    def get(self, request, ikn):
        exists = SavedTender.objects.filter(
            user=request.user, tender_ikn=ikn
        ).exists()
        return Response({"is_saved": exists})


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
        description="İhale için alarm kurar.",
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
