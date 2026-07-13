"""
EKAP toplama Celery görevleri.

Tümü `ekap` kuyruğuna yönlendirilir (settings CELERY_TASK_ROUTES) ve tek
concurrency'li worker ile serileştirilir. Her görev Redis kilidiyle eşzamanlı
çalışmayı engeller, `SyncRun` ile loglanır. EKAP çağrıları client içinde
throttle + backoff uygular.
"""
import logging
from contextlib import contextmanager
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .client import EkapV2Client
from .models import SyncCheckpoint, SyncRun, Tender
from . import sync as sync_mod

logger = logging.getLogger("ihaletakip")


@contextmanager
def _run(task_name, lock_ttl=3600):
    """SyncRun kaydı + Redis kilidi (aynı görev tekrar tetiklenirse atla)."""
    lock_key = f"ekap:lock:{task_name}"
    got = cache.add(lock_key, "1", timeout=lock_ttl)
    if not got:
        logger.info("%s zaten çalışıyor, atlanıyor", task_name)
        yield None
        return
    run = SyncRun.objects.create(task=task_name)
    try:
        yield run
        run.status = "ok"
    except Exception as e:
        run.status = "error"
        run.note = str(e)[:1000]
        logger.exception("%s hata: %s", task_name, e)
        raise
    finally:
        run.finished_at = timezone.now()
        run.save()
        cache.delete(lock_key)


def _enqueue_detail(ekap_id, defer=True):
    if defer:
        sync_detail.delay(ekap_id)
    else:
        _client_sync_detail(ekap_id)


def _client_sync_detail(ekap_id):
    sync_mod.sync_detail(ekap_id, EkapV2Client())


def _upsert_item_safe(item):
    """upsert_tender_from_list'i sarar: tek bir bozuk kayıt tüm sayfayı/çalışmayı
    düşürmesin. Başarılıysa (tender, 0), hatada (None, 1) döner."""
    try:
        return sync_mod.upsert_tender_from_list(item), 0
    except Exception as e:
        logger.warning("ihale atlandı ikn=%s: %s", item.get("ikn"), e)
        return None, 1


# ── Detay ──────────────────────────────────────────────
@shared_task(name="ekap.tasks.sync_detail", bind=True, max_retries=2, default_retry_delay=60)
def sync_detail(self, ekap_id):
    """Tek ihalenin detay + ilanlarını çeker."""
    try:
        sync_mod.sync_detail(ekap_id, EkapV2Client())
    except Exception as e:
        raise self.retry(exc=e)
    return {"ekap_id": ekap_id}


# ── Güncel (nightly) ───────────────────────────────────
@shared_task(name="ekap.tasks.sync_recent")
def sync_recent(days=None, max_pages=20, page_size=50, defer_detail=True):
    """Son N günün ihalelerini çekip liste satırlarını upsert eder, detay kuyruğa atar."""
    days = days or settings.EKAP_RECENT_DAYS
    with _run("sync_recent") as run:
        if run is None:
            return
        client = EkapV2Client()
        floor = timezone.now() - timedelta(days=days)
        total = 0
        errors = 0
        for page in range(max_pages):
            body = client.build_search_body(
                orderBy="ilanTarihi", siralamaTipi="desc",
                paginationSkip=page * page_size, paginationTake=page_size,
            )
            items, total_count = sync_mod.extract_list(client.search(body))
            if not items:
                break
            reached_floor = False
            for item in items:
                tender, err = _upsert_item_safe(item)
                errors += err
                if tender:
                    total += 1
                    _enqueue_detail(tender.ekap_id, defer=defer_detail)
                    if tender.ilan_tarihi and tender.ilan_tarihi < floor:
                        reached_floor = True
            if reached_floor or (page + 1) * page_size >= (total_count or 0):
                break
        run.items = total
        run.errors = errors
        _update_checkpoint("recent", newest=timezone.now())
        return {"upserted": total, "errors": errors}


# ── Backfill (sürekli, yavaş) ──────────────────────────
@shared_task(name="ekap.tasks.backfill")
def backfill(max_pages=3, page_size=50, defer_detail=True):
    """İmleçten geriye doğru, son N yıl tabanına kadar geçmişi doldurur."""
    with _run("backfill") as run:
        if run is None:
            return
        cp, _ = SyncCheckpoint.objects.get_or_create(name="backfill")
        if cp.done:
            return {"status": "done"}

        client = EkapV2Client()
        floor = timezone.now() - timedelta(days=365 * settings.EKAP_BACKFILL_YEARS)
        total = 0
        errors = 0
        skip = cp.cursor_skip
        oldest = None
        aborted = None
        for _ in range(max_pages):
            body = client.build_search_body(
                orderBy="ilanTarihi", siralamaTipi="desc",
                paginationSkip=skip, paginationTake=page_size,
            )
            # EKAP gün içinde yavaş/yanıtsız olabilir. Sayfa çekilemezse çalışmayı
            # hata saymayız: o ana kadarki ilerlemeyi (skip) korur, zarifçe biter,
            # bir sonraki tetik kaldığı yerden devam eder. (İstemci zaten timeout +
            # üstel backoff ile EKAP_MAX_RETRIES kez denedi.)
            try:
                items, total_count = sync_mod.extract_list(client.search(body))
            except Exception as e:
                aborted = str(e)[:300]
                logger.warning("backfill sayfası atlandı (EKAP yanıt vermedi): %s", aborted)
                break
            if not items:
                cp.done = True
                break
            for item in items:
                tender, err = _upsert_item_safe(item)
                errors += err
                if tender:
                    total += 1
                    _enqueue_detail(tender.ekap_id, defer=defer_detail)
                    if tender.ilan_tarihi:
                        oldest = tender.ilan_tarihi if oldest is None else min(oldest, tender.ilan_tarihi)
            skip += page_size
            if (oldest and oldest < floor) or skip >= (total_count or 0):
                cp.done = True
                break
        cp.cursor_skip = skip
        if oldest:
            cp.oldest_date = oldest
        cp.save()
        run.items = total
        run.errors = errors
        if aborted:
            run.note = f"EKAP kısmi (sonraki tetikte devam): {aborted}"
        return {
            "upserted": total, "errors": errors, "skip": skip,
            "done": cp.done, "aborted": bool(aborted),
        }


# ── Akıllı yenileme ────────────────────────────────────
@shared_task(name="ekap.tasks.refresh_stale")
def refresh_stale(batch=50, years=None, defer_detail=True):
    """should_refresh_detail politikasına göre bayat detayları yeniler.

    Yalnızca son ``years`` yıl (ilan tarihine göre) içindeki ihaleler aday olur.
    """
    years = years or settings.EKAP_REFRESH_YEARS
    with _run("refresh_stale") as run:
        if run is None:
            return
        now = timezone.now()
        floor = now - timedelta(days=365 * years)
        # Aday havuzu: hiç detay çekilmemiş VEYA son 1 günde bakılmamış
        # (yalnızca son `years` yıl — ilan tarihi floor'un üstünde olanlar)
        candidates = Tender.objects.filter(
            detail_synced_at__isnull=True, ilan_tarihi__gte=floor
        ).order_by("-ilan_tarihi")[: batch * 3]
        if candidates.count() < batch:
            more = Tender.objects.filter(
                detail_synced_at__lt=now - timedelta(days=1), ilan_tarihi__gte=floor
            ).order_by("detail_synced_at")[: batch * 3]
            candidates = list(candidates) + list(more)

        picked = 0
        for tender in candidates:
            if picked >= batch:
                break
            if sync_mod.should_refresh_detail(tender, now):
                _enqueue_detail(tender.ekap_id, defer=defer_detail)
                picked += 1
        run.items = picked
        return {"refreshed": picked}


# ── Lookup senkronları (haftalık) ──────────────────────
@shared_task(name="ekap.tasks.sync_okas")
def sync_okas():
    with _run("sync_okas") as run:
        if run is None:
            return
        count = sync_mod.sync_okas(EkapV2Client())
        run.items = count
        return {"okas": count}


@shared_task(name="ekap.tasks.sync_authorities")
def sync_authorities():
    with _run("sync_authorities") as run:
        if run is None:
            return
        count = sync_mod.sync_authorities(EkapV2Client())
        run.items = count
        return {"authorities": count}


def _update_checkpoint(name, newest=None, oldest=None):
    cp, _ = SyncCheckpoint.objects.get_or_create(name=name)
    if newest:
        cp.newest_date = newest
    if oldest:
        cp.oldest_date = oldest
    cp.save()
