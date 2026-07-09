"""
ai view'ları.

Doküman analizi (uzun süren iş) → HER ZAMAN Celery worker'a atılır.
İstemci akışı:
  1) POST /ai/analyze          → {task_id, status:'pending'}  (cache varsa direkt sonuç)
  2) GET  /ai/tasks/{task_id}  → {status, analysis?, usage?}  (poll)

TTS kısa bir iştir → senkron döner.
"""
import logging

from rest_framework import permissions, status
from rest_framework.views import APIView

from config import celery_app
from core.response import api_response

from .models import AnalysisCache
from .serializers import AnalyzeRequestSerializer, TTSRequestSerializer
from .services.tts import TTSError, synthesize_speech
from .tasks import run_analysis_task

logger = logging.getLogger("ihaletakip")


class AnalyzeView(APIView):
    """POST /ai/analyze — analizi kuyruğa alır (cache varsa anında döner)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AnalyzeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        analysis_type = data["analysis_type"]
        ikn = data.get("ikn") or None

        # 1) Cache kontrolü (İKN varsa) — anında dönebilir
        if ikn:
            cached = AnalysisCache.objects.filter(
                ikn=ikn, analysis_type=analysis_type
            ).first()
            if cached:
                return api_response(
                    data={
                        "status": "completed",
                        "cached": True,
                        "analysis": cached.analysis,
                        "usage": cached.usage,
                    },
                    message="Önbellekten getirildi.",
                )

        # 2) Uzun iş → Celery worker'a at
        task = run_analysis_task.delay(
            analysis_type=analysis_type,
            file_base64=data.get("file_base64"),
            file_name=data.get("file_name"),
            tender_meta=data.get("tender_meta"),
            similar_tenders=data.get("similar_tenders"),
            ikn=ikn,
        )
        return api_response(
            data={"task_id": task.id, "status": "pending"},
            message="Analiz kuyruğa alındı.",
            status=status.HTTP_202_ACCEPTED,
        )


class AnalyzeStatusView(APIView):
    """GET /ai/tasks/{task_id} — Celery görev durumunu/sonucunu döndürür."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, task_id):
        result = celery_app.AsyncResult(task_id)
        state = result.state

        if state in ("PENDING", "RECEIVED"):
            return api_response(data={"status": "pending"}, message="Sırada bekliyor.")

        if state in ("STARTED", "RETRY"):
            return api_response(data={"status": "processing"}, message="İşleniyor.")

        if state == "SUCCESS":
            payload = result.result or {}
            if not payload.get("success", False):
                return api_response(
                    data={"status": "failed"},
                    message=payload.get("error", "Analiz başarısız."),
                    success=False,
                    status=422,
                )
            return api_response(
                data={
                    "status": "completed",
                    "analysis": payload.get("analysis"),
                    "usage": payload.get("usage"),
                },
                message="Tamamlandı.",
            )

        # FAILURE / REVOKED
        return api_response(
            data={"status": "failed"},
            message="Analiz sırasında bir hata oluştu.",
            success=False,
            status=500,
        )


class TTSView(APIView):
    """POST /ai/tts — metni sese çevirir (senkron)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = TTSRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            audio_base64 = synthesize_speech(serializer.validated_data["text"])
        except TTSError as e:
            return api_response(
                message=e.message, success=False, status=e.status
            )
        return api_response(data={"audio_base64": audio_base64})
