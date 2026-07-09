"""ai Celery görevleri — asenkron analiz + cache temizliği."""
import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("ihaletakip")


@shared_task(name="ai.tasks.run_analysis_task", bind=True)
def run_analysis_task(self, analysis_type, file_base64=None, file_name=None,
                      tender_meta=None, similar_tenders=None, ikn=None):
    """
    Ağır analiz işini arka planda çalıştırır (opsiyonel asenkron akış).
    Sonuç Celery result backend'de saklanır; istemci task_id ile sorgular.
    """
    from ai.services.claude import AnalysisError, run_analysis
    from ai.models import AnalysisCache

    try:
        result = run_analysis(
            analysis_type=analysis_type,
            file_base64=file_base64,
            file_name=file_name,
            tender_meta=tender_meta,
            similar_tenders=similar_tenders,
        )
    except AnalysisError as e:
        return {"success": False, "error": e.message}

    if ikn:
        AnalysisCache.objects.update_or_create(
            ikn=ikn,
            analysis_type=analysis_type,
            defaults={"analysis": result["analysis"], "usage": result.get("usage")},
        )

    return {"success": True, **result}


@shared_task(name="ai.tasks.cleanup_expired_analyses")
def cleanup_expired_analyses(days: int = 30):
    """Belirtilen günden eski analiz cache kayıtlarını siler."""
    from ai.models import AnalysisCache

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = AnalysisCache.objects.filter(created_at__lt=cutoff).delete()
    logger.info("cleanup_expired_analyses: %s analiz silindi", deleted)
    return {"deleted": deleted}
