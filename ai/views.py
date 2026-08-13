"""
ai view'ları.

Doküman analizi (uzun süren iş) → HER ZAMAN Celery worker'a atılır.
İstemci akışı:
  1) POST /ai/analyze          → {task_id, status:'pending'}  (cache varsa direkt sonuç)
  2) GET  /ai/tasks/{task_id}  → {status, analysis?, usage?}  (poll)

TTS kısa bir iştir → senkron döner.
"""
import logging

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    inline_serializer,
)
from rest_framework import permissions, serializers, status
from rest_framework.views import APIView

from django.core.cache import cache

from accounts.premium import MSG_AI_OZET, MSG_ANALYSIS, require_premium
from config import celery_app
from core.response import api_response

from .models import AnalysisCache
from .serializers import (
    AnalyzeRequestSerializer,
    SummaryRequestSerializer,
    TTSRequestSerializer,
)
from .services import summary as summary_mod
from .services.tts import TTSError, synthesize_speech
from .tasks import run_analysis_task, run_summary_task

logger = logging.getLogger("ihaletakip")

_ANALYSIS_RESULT = inline_serializer(
    name="AnalysisResult",
    fields={
        "status": serializers.ChoiceField(
            choices=["pending", "processing", "completed", "failed"]
        ),
        "cached": serializers.BooleanField(required=False),
        "analysis": serializers.JSONField(required=False),
        "usage": serializers.JSONField(required=False),
    },
)

_TASK_QUEUED = inline_serializer(
    name="TaskQueued",
    fields={
        "task_id": serializers.CharField(),
        "status": serializers.CharField(),
    },
)


@extend_schema(
    tags=["ai"],
    summary="Doküman analizi başlat",
    responses={200: _ANALYSIS_RESULT, 202: _TASK_QUEUED},
    description=(
        "Analizi Celery kuyruğuna alır ve `202` ile `task_id` döner. Sonucu almak için "
        "`GET /api/v1/ai/tasks/{task_id}` ucunu yoklayın (poll).\n\n"
        "İstisna: `ikn` gönderilmişse ve o İKN için aynı `analysis_type` daha önce "
        "hesaplanmışsa, kuyruk atlanır ve sonuç `200` ile anında döner "
        "(`{\"status\": \"completed\", \"cached\": true}`).\n\n"
        "**Zorunlu alanlar `analysis_type`'a göre değişir:**\n\n"
        "| analysis_type | Ek zorunlu alanlar |\n"
        "|---|---|\n"
        "| `tech_spec` | `file_base64`, `file_name` |\n"
        "| `admin_spec` | `file_base64`, `file_name` |\n"
        "| `cost_analysis` | `tender_meta` |\n"
        "| `generate_keywords` | `tender_meta` |\n\n"
        "`.doc` (eski Word) reddedilir — `.docx` veya `.pdf` gönderin. `file_base64` "
        "ham base64'tür (`data:` öneki olmadan).\n\n"
        "**Pro özellik:** Doküman analizi yalnızca Pro üyelere açıktır. Free üye "
        "dokümanı indirebilir ancak analiz isteğinde **403** alır "
        "(`errors.code = premium_required`); istek işlenmez."
    ),
    request=AnalyzeRequestSerializer,
    examples=[
        OpenApiExample(
            "Teknik şartname (dosya)",
            request_only=True,
            description="Dosya gerektiren türler: tech_spec, admin_spec.",
            value={
                "analysis_type": "tech_spec",
                "file_name": "teknik_sartname.pdf",
                "file_base64": "JVBERi0xLjQKJeLjz9MKMyAwIG9iago8PC9GaWx0ZXI...",
                "ikn": "2025/1234567",
            },
        ),
        OpenApiExample(
            "Maliyet analizi (meta)",
            request_only=True,
            description="tender_meta gerektiren türler: cost_analysis, generate_keywords.",
            value={
                "analysis_type": "cost_analysis",
                "ikn": "2025/1234567",
                "tender_meta": {
                    "ihaleAdi": "Bilgisayar ve Çevre Birimi Alımı",
                    "idareAdi": "Ankara Büyükşehir Belediyesi",
                    "ihaleTarihi": "23.03.2027 14:00",
                    "yaklasikMaliyet": 2500000,
                },
                "similar_tenders": [
                    {"ikn": "2024/1122334", "sozlesmeBedeli": 2310000},
                ],
            },
        ),
        OpenApiExample(
            "Anahtar kelime üret",
            request_only=True,
            value={
                "analysis_type": "generate_keywords",
                "tender_meta": {
                    "ihaleAdi": "Bilgisayar ve Çevre Birimi Alımı",
                    "idareAdi": "Ankara Büyükşehir Belediyesi",
                },
            },
        ),
    ],
)
class AnalyzeView(APIView):
    """POST /ai/analyze — analizi kuyruğa alır (cache varsa anında döner)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Yapay zeka doküman analizi Pro'ya özeldir (cache dahil hiç işlenmez).
        require_premium(request.user, MSG_ANALYSIS)

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
            user_id=request.user.id,  # AnalyzeStatusView sahipliği bununla doğrular
        )
        return api_response(
            data={"task_id": task.id, "status": "pending"},
            message="Analiz kuyruğa alındı.",
            status=status.HTTP_202_ACCEPTED,
        )


@extend_schema(
    tags=["ai"],
    summary="Rapor sesli özeti üret (Pro)",
    description=(
        "İdare profili, pazar panosu veya fiyat analizi raporunu **sesli okunmaya uygun** "
        "kısa bir metne çevirir. Metin `POST /ai/tts/` ile seslendirilir.\n\n"
        "Akış `POST /ai/analyze` ile **birebir aynıdır**: `202` + `task_id` döner, istemci "
        "`GET /ai/tasks/{task_id}/` ile yoklar. Cache isabetinde `200` + `status: completed` "
        "ile sonuç doğrudan gelir (`cached: true`).\n\n"
        "⚠️ Sonuç alanının adı **`analysis`**'tir (`summary` değil) — `AnalyzeStatusView` "
        "sözleşmesiyle aynı kalsın diye.\n\n"
        "**Kapsam parametreleri `kind`'a göre değişir:**\n\n"
        "| `kind` | Zorunlu | Opsiyonel |\n"
        "|---|---|---|\n"
        "| `authority` | `idare_id` \\| `idare_detsis` \\| `en_ust_idare_kod` (biri) | — |\n"
        "| `market` | — | `yil`, `okas_bucket` |\n"
        "| `benchmark` | `ekap_id` | — |\n\n"
        "⚠️ **Üretilen metin bir fiyat tavsiyesi değildir**; prompt modele rakam taahhüt "
        "etmeyi, veride olmayan sayı üretmeyi ve örneklemsiz ortalama anmayı yasaklar."
    ),
    request=SummaryRequestSerializer,
    responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
    examples=[
        OpenApiExample(
            "İdare raporu özeti",
            request_only=True,
            value={"kind": "authority", "idare_id": "28484"},
        ),
        OpenApiExample(
            "Pazar panosu — yıl geneli",
            request_only=True,
            value={"kind": "market", "yil": 2025},
        ),
        OpenApiExample(
            "Pazar panosu — iş grubu",
            request_only=True,
            value={"kind": "market", "yil": 2025, "okas_bucket": "4523"},
        ),
        OpenApiExample(
            "Fiyat analizi özeti",
            request_only=True,
            value={"kind": "benchmark", "ekap_id": "e8c865a28be0b142"},
        ),
        OpenApiExample(
            "Kuyruğa alındı",
            response_only=True,
            value={"task_id": "3f8c2b1a-7e4d-4a91-9c2f-8b6d5e1a0c33", "status": "pending"},
        ),
    ],
)
class AiSummaryView(APIView):
    """POST /ai/summary — rapor sesli özetini kuyruğa alır (cache varsa anında döner)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # ⚠️ **Bu kapı atlanamaz.** `profil()`, `genel_bakis()`, `grup_detayi()` ve
        # `benchmark()` **maskesiz ham veri** döner; maskeleme view katmanında yapılıyor
        # (`_market_maskele`, `TenderBenchmarkView._KILITLI`). Free kullanıcıya 403
        # verilmezse maskeli tutarlar özet metni üzerinden sızar.
        require_premium(request.user, MSG_AI_OZET)

        serializer = SummaryRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        kind = data.pop("kind")

        # 1) Cache — özet kullanıcıdan bağımsızdır, aynı rapor herkese aynı metni verir
        anahtar = summary_mod.cache_anahtari(kind, data)
        cached = cache.get(anahtar)
        if cached:
            return api_response(
                data={"status": "completed", "cached": True, **cached},
                message="Önbellekten getirildi.",
            )

        # 2) Uzun iş → Celery worker'a at (akış /ai/analyze ile birebir aynı)
        task = run_summary_task.delay(
            kind=kind,
            params=data,
            user_id=request.user.id,  # AnalyzeStatusView sahipliği bununla doğrular
        )
        return api_response(
            data={"task_id": task.id, "status": "pending"},
            message="Özet kuyruğa alındı.",
            status=status.HTTP_202_ACCEPTED,
        )


@extend_schema(
    tags=["ai"],
    summary="Analiz durumunu sorgula",
    description=(
        "`POST /ai/analyze` yanıtındaki `task_id` ile görevin durumunu döner.\n\n"
        "| `data.status` | HTTP | Anlamı |\n"
        "|---|---|---|\n"
        "| `pending` | 200 | Kuyrukta bekliyor |\n"
        "| `processing` | 200 | Worker işliyor |\n"
        "| `completed` | 200 | `data.analysis` ve `data.usage` dolu |\n"
        "| `failed` | 422 / 500 | `message` hatayı açıklar |\n\n"
        "İstemci `completed` veya `failed` görene kadar birkaç saniye aralıkla yoklar."
    ),
    parameters=[
        OpenApiParameter(
            name="task_id",
            location=OpenApiParameter.PATH,
            type=str,
            required=True,
            description="`POST /ai/analyze` yanıtındaki Celery görev kimliği (UUID).",
            examples=[
                OpenApiExample(
                    "Görev kimliği",
                    value="3f8c2b1a-7e4d-4a91-9c2f-8b6d5e1a0c33",
                )
            ],
        )
    ],
    responses={200: _ANALYSIS_RESULT},
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
            # ── Sahiplik ──────────────────────────────────────────────────────
            # Görev sonucu YALNIZCA onu başlatan kullanıcıya verilir. Aksi hâlde
            # task_id'yi ele geçiren herhangi bir oturumlu kullanıcı başkasının
            # doküman analizini / asistan yanıtını okuyabilir.
            # `user_id` yoksa görev bu değişiklikten ÖNCE kuyruğa girmiştir (deploy
            # anındaki birkaç dakikalık pencere) → eski davranış korunur, uyarı loglanır.
            owner_id = payload.get("user_id")
            if owner_id is None:
                logger.warning(
                    "AnalyzeStatusView: task %s sonucunda user_id yok (deploy öncesi görev?)",
                    task_id,
                )
            elif owner_id != request.user.id:
                # 404: görevin varlığını da doğrulamayalım.
                return api_response(
                    data=None, message="Görev bulunamadı.", success=False, status=404
                )
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


@extend_schema(
    tags=["ai"],
    summary="Metni sese çevir (TTS)",
    description=(
        "Google Cloud TTS ile metni seslendirir ve base64 kodlu ses döner "
        "(`data.audio_base64`). Kısa bir iş olduğu için **senkron** çalışır, kuyruğa "
        "alınmaz.\n\n"
        "Sunucuda `GOOGLE_APPLICATION_CREDENTIALS` tanımlı değilse hata döner."
    ),
    request=TTSRequestSerializer,
    responses={
        200: inline_serializer(
            name="TTSResult", fields={"audio_base64": serializers.CharField()}
        )
    },
    examples=[
        OpenApiExample(
            "Kısa metin",
            request_only=True,
            value={"text": "Bu ihale 23 Mart 2027 tarihinde saat 14:00'te yapılacaktır."},
        )
    ],
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
