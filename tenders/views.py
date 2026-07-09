"""tenders view'ları — favoriler, filtreler, kayıtlı ihaleler, alarmlar, bildirimler."""
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Favorite, Notification, SavedFilter, SavedTender, TenderAlarm
from .serializers import (
    FavoriteSerializer,
    NotificationSerializer,
    SavedFilterSerializer,
    SavedTenderSerializer,
    TenderAlarmSerializer,
)


class OwnerQuerysetMixin:
    """İstekteki kullanıcıya ait kayıtları filtreler ve otomatik atar."""

    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return self.queryset_model.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


# ── Favoriler ──────────────────────────────────────────
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


class FavoriteDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, tender_id):
        Favorite.objects.filter(user=request.user, tender_id=tender_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get(self, request, tender_id):
        exists = Favorite.objects.filter(
            user=request.user, tender_id=tender_id
        ).exists()
        return Response({"is_favorite": exists})


# ── Kayıtlı Filtreler ──────────────────────────────────
class SavedFilterListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = SavedFilterSerializer
    queryset_model = SavedFilter


class SavedFilterDetailView(OwnerQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = SavedFilterSerializer
    queryset_model = SavedFilter


# ── Kayıtlı İhaleler ───────────────────────────────────
class SavedTenderListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = SavedTenderSerializer
    queryset_model = SavedTender

    def perform_create(self, serializer):
        SavedTender.objects.update_or_create(
            user=self.request.user,
            tender_ikn=serializer.validated_data["tender_ikn"],
            defaults=serializer.validated_data,
        )


class SavedTenderDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, ikn):
        SavedTender.objects.filter(user=request.user, tender_ikn=ikn).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get(self, request, ikn):
        exists = SavedTender.objects.filter(
            user=request.user, tender_ikn=ikn
        ).exists()
        return Response({"is_saved": exists})


# ── Alarmlar ───────────────────────────────────────────
class TenderAlarmListCreateView(OwnerQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = TenderAlarmSerializer
    queryset_model = TenderAlarm

    def perform_create(self, serializer):
        TenderAlarm.objects.update_or_create(
            user=self.request.user,
            tender_id=serializer.validated_data["tender_id"],
            defaults=serializer.validated_data,
        )


class TenderAlarmDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, tender_id):
        TenderAlarm.objects.filter(user=request.user, tender_id=tender_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get(self, request, tender_id):
        alarm = TenderAlarm.objects.filter(
            user=request.user, tender_id=tender_id
        ).first()
        if not alarm:
            return Response(None)
        return Response(TenderAlarmSerializer(alarm).data)


# ── Bildirimler ────────────────────────────────────────
class NotificationListView(OwnerQuerysetMixin, generics.ListAPIView):
    serializer_class = NotificationSerializer
    queryset_model = Notification


class NotificationReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, notification_id):
        updated = Notification.objects.filter(
            user=request.user, id=notification_id
        ).update(read=True)
        return Response({"updated": updated})


class NotificationReadAllView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        updated = Notification.objects.filter(
            user=request.user, read=False
        ).update(read=True)
        return Response({"updated": updated})


class NotificationUnreadCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(user=request.user, read=False).count()
        return Response({"unread": count})
