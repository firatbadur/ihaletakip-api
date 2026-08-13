"""ai Celery görevleri — asenkron analiz + cache temizliği."""
import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

# Rapor özeti cache süresi (sn). Özet kullanıcıdan bağımsızdır; aynı rapor
# herkese aynı metni verir, bu yüzden anahtara kullanıcı girmez.
SUMMARY_CACHE_TTL = 24 * 60 * 60


@shared_task(name="ai.tasks.run_analysis_task", bind=True)
def run_analysis_task(self, analysis_type, file_base64=None, file_name=None,
                      tender_meta=None, similar_tenders=None, ikn=None, user_id=None):
    """
    Ağır analiz işini arka planda çalıştırır (opsiyonel asenkron akış).
    Sonuç Celery result backend'de saklanır; istemci task_id ile sorgular.

    ⚠️ `user_id` dönüş payload'una **konulmalıdır**: `AnalyzeStatusView` sonucu yalnızca
    görevi başlatan kullanıcıya verir. Yoksa task_id'yi bilen herhangi bir oturumlu
    kullanıcı başkasının analizini okuyabilir. Aynı sözleşme `assistant.tasks`'ın iki
    görevi için de geçerli (üçü de bu tek uçtan sorgulanır).
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
        return {"success": False, "error": e.message, "user_id": user_id}

    if ikn:
        AnalysisCache.objects.update_or_create(
            ikn=ikn,
            analysis_type=analysis_type,
            defaults={"analysis": result["analysis"], "usage": result.get("usage")},
        )

    return {"success": True, "user_id": user_id, **result}


@shared_task(name="ai.tasks.run_summary_task", bind=True)
def run_summary_task(self, kind, params, user_id=None):
    """
    Rapor sesli özeti üretir (idare profili / pazar panosu / fiyat analizi).

    ⚠️ `user_id` dönüş payload'una **konulmalıdır**: `AnalyzeStatusView` sonucu yalnızca
    görevi başlatan kullanıcıya verir (aynı sözleşme `run_analysis_task` ve
    `assistant.tasks` için de geçerli — üçü de tek uçtan sorgulanır).

    ⚠️ Dönüş alanının adı **`analysis`** olmalı, `summary`/`text` değil: mobildeki
    `pollTask` bu alanı okuyor ve `AnalyzeStatusView` sözleşmesi bu.
    """
    from django.core.cache import cache

    from ai.services.claude import AnalysisError
    from ai.services.summary import cache_anahtari, ozet_uret

    try:
        sonuc = ozet_uret(kind, params or {})
    except AnalysisError as e:
        return {"success": False, "error": e.message, "user_id": user_id}
    except Exception as e:  # beklenmeyen — görev sessizce kaybolmasın
        logger.exception("run_summary_task hata (kind=%s): %s", kind, e)
        return {"success": False, "error": "Özet üretilemedi.", "user_id": user_id}

    # Cache'i **görev** yazar, view değil: view yalnızca okur ve isabet varsa hiç
    # kuyruğa almaz. TTL 24 sa — pazar verisi gece 01:30'da, idare profili ve
    # benchmark günlük ölçekte değişiyor.
    cache.set(cache_anahtari(kind, params or {}), sonuc, timeout=SUMMARY_CACHE_TTL)
    return {"success": True, "user_id": user_id, **sonuc}


@shared_task(name="ai.tasks.cleanup_expired_analyses")
def cleanup_expired_analyses(days: int = 30):
    """Belirtilen günden eski analiz cache kayıtlarını siler."""
    from ai.models import AnalysisCache

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = AnalysisCache.objects.filter(created_at__lt=cutoff).delete()
    logger.info("cleanup_expired_analyses: %s analiz silindi", deleted)
    return {"deleted": deleted}
