"""assistant URL'leri — /api/v1/assistant/..."""
from django.urls import path

from .views import (
    AssistantActionDismissView,
    AssistantActionExecuteView,
    ChatMessageFeedbackView,
    ChatMessageListView,
    ChatSendView,
    ConversationDetailView,
    ConversationListView,
    ProfileView,
    RecommendationListView,
    RecommendationSeenView,
)
from .views_ek import EkDetayView, EkSinirlariView, EkYukleView

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="assistant-profile"),
    path("messages/", ChatMessageListView.as_view(), name="assistant-messages"),
    path(
        "messages/<int:pk>/feedback/",
        ChatMessageFeedbackView.as_view(),
        name="assistant-message-feedback",
    ),
    path("conversations/", ConversationListView.as_view(), name="assistant-conversations"),
    path(
        "conversations/<int:pk>/",
        ConversationDetailView.as_view(),
        name="assistant-conversation-detail",
    ),
    path("chat/", ChatSendView.as_view(), name="assistant-chat"),
    # Doküman ve fotoğraf ekleri (docs/mobil-asistan-ekler.md)
    path("attachments/", EkYukleView.as_view(), name="assistant-attachments"),
    path("attachments/limits/", EkSinirlariView.as_view(), name="assistant-attachment-limits"),
    path("attachments/<uuid:ek_id>/", EkDetayView.as_view(), name="assistant-attachment-detail"),
    path(
        "actions/<uuid:action_id>/execute/",
        AssistantActionExecuteView.as_view(),
        name="assistant-action-execute",
    ),
    path(
        "actions/<uuid:action_id>/dismiss/",
        AssistantActionDismissView.as_view(),
        name="assistant-action-dismiss",
    ),
    path("recommendations/", RecommendationListView.as_view(), name="assistant-recommendations"),
    path(
        "recommendations/<int:pk>/seen/",
        RecommendationSeenView.as_view(),
        name="assistant-recommendation-seen",
    ),
]
