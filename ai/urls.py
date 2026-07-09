"""ai URL'leri — /api/v1/ai/..."""
from django.urls import path

from .views import AnalyzeStatusView, AnalyzeView, TTSView

urlpatterns = [
    path("analyze/", AnalyzeView.as_view(), name="analyze"),
    path("tasks/<str:task_id>/", AnalyzeStatusView.as_view(), name="analyze-status"),
    path("tts/", TTSView.as_view(), name="tts"),
]
