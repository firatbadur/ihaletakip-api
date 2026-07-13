"""assistant URL'leri — /api/v1/assistant/..."""
from django.urls import path

from .views import (
    ChatMessageListView,
    ChatSendView,
    ProfileView,
    RecommendationListView,
    RecommendationSeenView,
)

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="assistant-profile"),
    path("messages/", ChatMessageListView.as_view(), name="assistant-messages"),
    path("chat/", ChatSendView.as_view(), name="assistant-chat"),
    path("recommendations/", RecommendationListView.as_view(), name="assistant-recommendations"),
    path(
        "recommendations/<int:pk>/seen/",
        RecommendationSeenView.as_view(),
        name="assistant-recommendation-seen",
    ),
]
