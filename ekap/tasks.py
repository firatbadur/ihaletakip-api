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
from .series import series_skeleton

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


def _window_floor():
    """EKAP toplama penceresinin alt sınırı: son ``EKAP_BACKFILL_YEARS`` yıl.

    ``(datetime, ISO string)`` döner. Sınır **ihale tarihine** göredir; EKAP
    aramasına ``ihaleTarihSaatBaslangic`` olarak geçilir. EKAP bu alanı yalnızca
    ISO (``YYYY-MM-DDTHH:MM:SS``) formatıyla kabul eder (DD.MM.YYYY → HTTP 400).
    Liste seviyesinde ``ilanTarihi`` boş geldiği için pencereyi **EKAP'ın kendi**
    filtresiyle sunucu tarafında sınırlamak tek güvenilir yoldur (aksi halde EKAP
    2002'ye kadar ~1.96M kayıt döndürür ve backfill hiç durmaz)."""
    floor = timezone.now() - timedelta(days=365 * settings.EKAP_BACKFILL_YEARS)
    return floor, floor.strftime("%Y-%m-%dT%H:%M:%S")


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
        _, window_iso = _window_floor()
        total = 0
        errors = 0
        for page in range(max_pages):
            body = client.build_search_body(
                orderBy="ilanTarihi", siralamaTipi="desc",
                ihaleTarihSaatBaslangic=window_iso,
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
def backfill(max_pages=10, page_size=50, defer_detail=True):
    """Pencere tabanından (son N yıl) ileriye doğru imleçle geçmişi doldurur.

    Arama ``ihaleTarihSaatBaslangic`` ile EKAP tarafında son ``EKAP_BACKFILL_YEARS``
    yıla sınırlanır; ``ihaleTarihi`` **asc** sıralanır (en eski → yeni). Böylece
    DB'deki asıl boşluk (eski yıllar) önce dolar ve imleç, en yeni kayıtlar listenin
    sonuna eklendiği için kaymaz. ``skip >= total_count`` (pencere içi toplam) ya da
    boş sayfada biter."""
    with _run("backfill") as run:
        if run is None:
            return
        cp, _ = SyncCheckpoint.objects.get_or_create(name="backfill")
        if cp.done:
            return {"status": "done"}

        client = EkapV2Client()
        floor, floor_iso = _window_floor()
        total = 0
        errors = 0
        enqueued = 0
        skip = cp.cursor_skip
        oldest = None
        aborted = None
        for _ in range(max_pages):
            body = client.build_search_body(
                orderBy="ihaleTarihi", siralamaTipi="asc",
                ihaleTarihSaatBaslangic=floor_iso,
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
                    # Backfill'in işi **boşluk doldurmaktır**; tazeliği `refresh_stale`
                    # yönetir. Bu guard olmadan pencere genişletildiğinde (ör. 5→10 yıl,
                    # imleç sıfırlanır ve arşiv baştan taranır) detayı ZATEN çekilmiş
                    # yüz binlerce ihale için tekrar detay isteği kuyruğa girer — EKAP
                    # ~1 istek/sn olduğundan bu, günlerce boşa çekim demektir.
                    # Detayı hiç gelmemiş eskiler `sync_contractors.enqueue_missing_detail`
                    # ile yakalanır.
                    if tender.detail_synced_at is None:
                        _enqueue_detail(tender.ekap_id, defer=defer_detail)
                        enqueued += 1
                    if tender.ihale_tarihi:
                        oldest = tender.ihale_tarihi if oldest is None else min(oldest, tender.ihale_tarihi)
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
        run.note = f"detay_kuyruk={enqueued}/{total}"
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


# ── Yüklenici (firma) çözümlemesi ──────────────────────
@shared_task(name="ekap.tasks.sync_contractors")
def sync_contractors(
    max_tenders=50000, max_seconds=None, enqueue_missing_detail=True, missing_limit=50
):
    """
    Sözleşmeleri firmalara bağlar — **EKAP'a gitmez**, `Tender.detail_raw` arşivinden
    çalışır (yalnızca `enqueue_missing_detail` dalı detay kuyruğa atar).

    ⚠️ Sınır **süre bütçesidir**, sabit ihale sayısı değil: iş tamamen DB-içi olduğu için
    hız makineye göre çok değişir (ölçüm: ~200 ihale/sn). Sabit küçük bir batch, saatlik
    kapasitenin yüzde biri kadarını kullanıp backfill'i günlere yayardı.
    `max_seconds`, global `CELERY_TASK_TIME_LIMIT=300`'ün altında kalmalıdır.
    `max_tenders` yalnızca emniyet tavanıdır.

    ⚠️ **Duty cycle kullanıcı sorgularıyla aynı DB'yi paylaşır.** Eskiden beat bunu 5 dk'da
    bir 240 sn bütçeyle çağırıyordu (~%80 duty cycle); görev `detail_raw`'ı (~40 KB/satır)
    arşiv boyunca okuduğu için Postgres buffer cache'i sürekli boşalıyor ve ihale arama
    sorguları her seferinde diskten okumak zorunda kalıyordu. Şimdi 10 dk'da bir 90 sn
    (~%15) — süpürme birkaç kat uzun sürer ama arama ucu nefes alır. Süpürme bitip artımlı
    moda geçince yük zaten kendiliğinden düşer.

    İki mod:
      • Süpürme  (checkpoint bitmemişse) — PK imleciyle tüm arşivi tarar, kaymaz.
      • Artımlı  (süpürme bitince)       — `refresh_stale` bir detayı yenilediğinde
                                            (`contractors_synced_at < detail_synced_at`)
                                            kendiliğinden yakalar.
    """
    import time

    from django.db.models import F, Q

    from . import contractors as contractors_mod

    with _run("sync_contractors") as run:
        if run is None:
            return

        cp, _ = SyncCheckpoint.objects.get_or_create(name="contractors")
        sweeping = not cp.done
        last_pk = int((cp.extra or {}).get("last_tender_pk") or 0)

        # ⚠️ Süpürme YALNIZCA gece penceresinde çalışır (artımlı mod her saat serbest).
        # Süpürme tüm arşivin `detail_raw`'ını (~40 KB/satır) okur; 3 GB'lık makinede
        # 512 MB `shared_buffers` bunu kaldıramıyor ve arama sorgularının çalışma kümesi
        # sürekli eviction'a uğruyordu (ölçüm: heap cache isabeti %53 — olması gereken
        # >%99). Artımlı mod ise `refresh_stale`'in tazelediği birkaç yüz satıra dokunur,
        # ucuzdur, gündüz de çalışabilir.
        # Maliyet: süpürme daha uzun sürer (gündüz de koşsa ~5 gün, yalnız gece ~2 hafta)
        # — ama bu arka plan zenginleştirmesidir, kullanıcıyı bekletmez.
        if sweeping:
            hour = timezone.localtime().hour
            if not (settings.CONTRACTOR_SWEEP_START <= hour < settings.CONTRACTOR_SWEEP_END):
                logger.info(
                    "sync_contractors: süpürme gece penceresi dışında (saat=%s), atlanıyor", hour
                )
                run.note = f"süpürme atlandı (saat={hour}, pencere="
                run.note += f"{settings.CONTRACTOR_SWEEP_START}-{settings.CONTRACTOR_SWEEP_END})"
                return {"skipped": "peak_hours", "hour": hour}

        # Süre bütçesi moda göre: süpürme zaten yalnız gece koştuğu için pencereyi
        # doldurabilir (kısa bütçe gecenin %85'ini boşa harcıyordu); artımlı mod gündüz
        # de koştuğundan kısa kalır. Tavan `CELERY_TASK_TIME_LIMIT=300`'ün altındadır.
        if max_seconds is None:
            max_seconds = (
                settings.CONTRACTOR_SWEEP_MAX_SECONDS
                if sweeping
                else settings.CONTRACTOR_INCREMENTAL_MAX_SECONDS
            )

        # `.only()`: `sync_contracts_from_raw` yalnızca bu alanları okur (+`ikn` log için).
        # Bilhassa `list_raw` dışarıda kalmalı — `detail_raw` kadar büyük ve hiç
        # kullanılmıyor; arşiv taramasında okunan TOAST hacmini yarıya indirir.
        base = Tender.objects.filter(detail_raw__isnull=False).only(
            "ikn", "detail_raw", "idare_id", "il_id", "ihale_tip"
        )
        if sweeping:
            qs = base.filter(pk__gt=last_pk).order_by("pk")[:max_tenders]
        else:
            qs = base.filter(
                Q(contractors_synced_at__isnull=True)
                | Q(contractors_synced_at__lt=F("detail_synced_at"))
            ).order_by("detail_synced_at")[:max_tenders]

        processed = errors = contracts = alias_cakismasi = 0
        touched = set()
        deadline = time.monotonic() + max_seconds
        timed_out = False
        # detail_raw ~40 KB/satır → iterator şart (tümünü belleğe almaz)
        for tender in qs.iterator(chunk_size=200):
            # ⚠️ İmleci try'DAN ÖNCE ilerlet: kalıcı bozuk tek ihale imleci sonsuza
            # dek kilitlemesin (backfill `skip`'i aynı sebeple koşulsuz ilerletir).
            if sweeping:
                last_pk = max(last_pk, tender.pk)
            try:
                # recompute=False: agregalar tur sonunda birleşik kümede tek seferde
                res = sync_mod.sync_contracts_from_raw(tender, recompute=False)
                contracts += res["contracts"]
                touched |= res["contractors"]
                alias_cakismasi += res.get("alias_cakismasi", 0)
                processed += 1
            except Exception as e:
                errors += 1
                logger.warning("yüklenici çözümü atlandı ikn=%s: %s", tender.ikn, e)
            # Süre bütçesi dolduysa temiz çık — imleç kaydedilecek, sonraki tur devam eder
            if time.monotonic() >= deadline:
                timed_out = True
                break

        if sweeping:
            # Bitti sayabilmek için süre dolmamış OLMALI; aksi halde sadece bu turun
            # bütçesi tükenmiştir, arşiv bitmiş değildir.
            if processed == 0 and errors == 0 and not timed_out:
                cp.done = True
                logger.info("sync_contractors süpürmesi tamamlandı (pk=%s)", last_pk)
            cp.extra = {**(cp.extra or {}), "last_tender_pk": last_pk}
            cp.save()

        if touched:
            contractors_mod.recompute_aggregates(touched)

        # `refresh_stale` yalnızca EKAP_REFRESH_YEARS geriye bakıyor → daha eski
        # ihaleler hiç detay almaz. Bu dal tek EKAP'a dokunan parçadır, sınırlıdır.
        enqueued = 0
        if enqueue_missing_detail and missing_limit > 0:
            for ekap_id in (
                Tender.objects.filter(detail_synced_at__isnull=True)
                .exclude(ekap_id="")
                .order_by("-ihale_tarihi")
                .values_list("ekap_id", flat=True)[:missing_limit]
            ):
                sync_detail.delay(ekap_id)
                enqueued += 1

        kalan = (
            Tender.objects.filter(detail_raw__isnull=False, pk__gt=last_pk).count()
            if sweeping else 0
        )
        run.items = processed
        run.errors = errors
        run.note = (
            f"mod={'süpürme' if sweeping else 'artımlı'} sözleşme={contracts} "
            f"firma={len(touched)} alias_cakismasi={alias_cakismasi} detay_kuyruk={enqueued} "
            f"{'süre doldu ' if timed_out else ''}kalan={kalan}"
        )[:1000]
        return {
            "tenders": processed,
            "contracts": contracts,
            "contractors": len(touched),
            "alias_cakismasi": alias_cakismasi,
            "errors": errors,
            "enqueued_detail": enqueued,
            "remaining": kalan,
            "timed_out": timed_out,
        }


@shared_task(name="ekap.tasks.backfill_tender_fields")
def backfill_tender_fields(max_tenders=200000, max_seconds=None, batch_size=500):
    """
    Pro sinyal kolonlarını `Tender.detail_raw` arşivinden doldurur — **EKAP'a gitmez**.

    `sync.apply_pro_fields` ile aynı çıkarımı kullanır (tek kaynak): `okas_ana_kod`,
    `en_ust_idare_*`, `istekli_sayisi`, şikâyet/fiyat-dışı-unsur bayrakları, `seri_anahtar`.
    Yeni ve `refresh_stale` ile tazelenen kayıtlar zaten senkron anında dolar; bu görev
    yalnızca **arşivin geri kalanını** yakalar.

    ⚠️ **Yüklenici süpürmesine ÖNCELİK verir.** `sync_contractors` süpürme modundayken bu
    görev kendini geri çeker. Gerekçe (ölçülmüş): ikisi de arşivin `detail_raw`'ını
    (~40 KB/satır) okur; aynı gecede koşarlarsa TOAST trafiği ikiye katlanır ve **ikisi de**
    yarı hızda ilerler. Yüklenici süpürmesi hâlihazırda ilerlemiş durumda ve bitmesi
    `indirim_orani` + `sozlesme_sayisi` + `toplam_sozlesme_bedeli` alanlarını dolduruyor —
    yani yarım kalmış iki tarama yerine önce onu bitirmek daha değerli. Süpürme bitince
    (`SyncCheckpoint(name="contractors").done`) bu görev kendiliğinden devralır.

    ⚠️ Süpürme YALNIZCA gece penceresinde çalışır (`PRO_BACKFILL_START/END`) — arşiv
    taraması Postgres buffer cache'ini boşaltıp ihale aramasını diske düşürüyor.

    Sınır **süre bütçesidir** (`CELERY_TASK_TIME_LIMIT=300` altında), sabit sayı değil.
    İmleç `SyncCheckpoint(name="tender_fields")`; bozuk tek satır imleci kilitlemesin diye
    `try`'dan ÖNCE ilerletilir.
    """
    import time

    with _run("backfill_tender_fields") as run:
        if run is None:
            return

        cp, _ = SyncCheckpoint.objects.get_or_create(name="tender_fields")
        if cp.done:
            return {"skipped": "tamamlandi"}

        # ── Öncelik: yüklenici süpürmesi bitmeden başlama ─────────────────────
        yuklenici_cp = SyncCheckpoint.objects.filter(name="contractors").first()
        if yuklenici_cp is not None and not yuklenici_cp.done:
            logger.info("backfill_tender_fields: yüklenici süpürmesi sürüyor, sıra bekleniyor")
            run.note = "yüklenici süpürmesine öncelik verildi"
            return {"skipped": "contractor_sweep_active"}

        hour = timezone.localtime().hour
        if not (settings.PRO_BACKFILL_START <= hour < settings.PRO_BACKFILL_END):
            run.note = (
                f"gece penceresi dışında (saat={hour}, pencere="
                f"{settings.PRO_BACKFILL_START}-{settings.PRO_BACKFILL_END})"
            )
            return {"skipped": "peak_hours", "hour": hour}

        if max_seconds is None:
            max_seconds = settings.PRO_BACKFILL_MAX_SECONDS

        last_pk = int((cp.extra or {}).get("last_tender_pk") or 0)

        # `.only()`: `apply_pro_fields` bunları okur. ⚠️ `idare_id` ve `ihale_adi` ŞART —
        # `seri_anahtar` ikisini kullanıyor; eksik kalsalar alan başına ek sorgu atılırdı
        # (sessiz N+1). `list_raw` bilerek dışarıda: `detail_raw` kadar büyük, hiç
        # kullanılmıyor → taranan TOAST hacmini yarıya indirir.
        qs = (
            Tender.objects.filter(detail_raw__isnull=False, pk__gt=last_pk)
            .only("pk", "detail_raw", "idare_id", "ihale_adi")
            .order_by("pk")[:max_tenders]
        )

        processed = errors = 0
        batch = []
        deadline = time.monotonic() + max_seconds
        timed_out = False

        # detail_raw ~40 KB/satır → iterator şart (tümünü belleğe almaz)
        for tender in qs.iterator(chunk_size=200):
            last_pk = max(last_pk, tender.pk)  # ⚠️ try'DAN ÖNCE
            try:
                sync_mod.apply_pro_fields(tender, (tender.detail_raw or {}).get("item", {}))
                batch.append(tender)
                processed += 1
            except Exception as e:
                errors += 1
                logger.warning("sinyal çıkarımı atlandı pk=%s: %s", tender.pk, e)

            if len(batch) >= batch_size:
                Tender.objects.bulk_update(batch, sync_mod.PRO_TENDER_FIELDS)
                batch = []
            if time.monotonic() >= deadline:
                timed_out = True
                break

        if batch:
            Tender.objects.bulk_update(batch, sync_mod.PRO_TENDER_FIELDS)

        # "Bitti" diyebilmek için süre dolmamış OLMALI; aksi halde yalnızca bu turun
        # bütçesi tükenmiştir, arşiv bitmiş değildir.
        if processed == 0 and errors == 0 and not timed_out:
            cp.done = True
            logger.info("backfill_tender_fields tamamlandı (pk=%s)", last_pk)
        cp.extra = {**(cp.extra or {}), "last_tender_pk": last_pk}
        cp.save()

        kalan = Tender.objects.filter(detail_raw__isnull=False, pk__gt=last_pk).count()
        run.items = processed
        run.errors = errors
        run.note = f"{'süre doldu ' if timed_out else ''}kalan={kalan}"[:1000]
        return {
            "tenders": processed,
            "errors": errors,
            "remaining": kalan,
            "timed_out": timed_out,
            "done": cp.done,
        }


@shared_task(name="ekap.tasks.detect_recurring_series")
def detect_recurring_series(min_uye=3, max_seconds=None):
    """
    Tekrar eden ihale serilerini tespit eder — **EKAP'a gitmez, `detail_raw` OKUMAZ**.

    Seri anahtarı ingest'te hesaplanıyor (`sync.apply_pro_fields` → `series.series_key`),
    bu görev yalnızca o indeksli `varchar(40)` üzerinde GROUP BY yapar. Metin
    karşılaştırması, trigram, self-join YOK — bu bilinçli bir tasarım kararıydı
    (bkz. `ekap/series.py` modül docstring'i).

    ⚠️ `.values()` kullanır → `detail_raw` TOAST'ına hiç dokunulmaz, dolayısıyla
    `sync_contractors`/`backfill_tender_fields` ile pencere çakışması sorunu yoktur.

    Periyot tespiti: üye ilan tarihleri arasındaki aralıkların **medyanı** (ortalama değil
    — tek bir sıra dışı aralık ortalamayı kaydırır). Güven, sapmanın medyana oranına göre.
    """
    import statistics
    import time

    from django.db.models import Avg, Count, Max, Min, Sum

    from .models import Contract, RecurringTenderSeries

    with _run("detect_recurring_series") as run:
        if run is None:
            return

        if max_seconds is None:
            max_seconds = settings.PRO_BACKFILL_MAX_SECONDS
        deadline = time.monotonic() + max_seconds
        basla = timezone.now()

        gruplar = (
            Tender.objects.exclude(seri_anahtar="")
            .filter(ilan_tarihi__isnull=False)
            .order_by()  # ⚠️ Meta.ordering GROUP BY'a sızmasın
            .values("seri_anahtar", "idare_id")
            .annotate(n=Count("id"), ilk=Min("ilan_tarihi"), son=Max("ilan_tarihi"))
            .filter(n__gte=min_uye)
            .order_by("seri_anahtar")
        )

        yazilacak, islenen, timed_out = [], 0, False
        for g in gruplar.iterator(chunk_size=500):
            uyeler = list(
                Tender.objects.filter(
                    seri_anahtar=g["seri_anahtar"], idare_id=g["idare_id"],
                    ilan_tarihi__isnull=False,
                )
                .order_by("ilan_tarihi")
                .values("ilan_tarihi", "ihale_adi", "ekap_id", "il_id",
                        "ihale_tip", "okas_ana_kod", "okas_ana_adi",
                        "idare_adi", "en_ust_idare_kod")
            )
            if len(uyeler) < min_uye:
                continue

            tarihler = [u["ilan_tarihi"] for u in uyeler]
            araliklar = [
                (tarihler[i + 1] - tarihler[i]).days for i in range(len(tarihler) - 1)
            ]
            araliklar = [a for a in araliklar if a > 0]
            if not araliklar:
                continue
            medyan = int(statistics.median(araliklar))
            sapma = int(statistics.pstdev(araliklar)) if len(araliklar) > 1 else 0
            son = uyeler[-1]

            yazilacak.append(RecurringTenderSeries(
                seri_anahtar=g["seri_anahtar"],
                idare_id=g["idare_id"],
                idare_adi=son["idare_adi"] or "",
                en_ust_idare_kod=son["en_ust_idare_kod"] or "",
                il_id=son["il_id"],
                okas_ana_kod=son["okas_ana_kod"] or "",
                okas_ana_adi=son["okas_ana_adi"] or "",
                ihale_tip=son["ihale_tip"],
                iskelet=series_skeleton(son["ihale_adi"])[:300],
                ornek_ihale_adi=son["ihale_adi"] or "",
                ihale_sayisi=len(uyeler),
                ilk_ilan=tarihler[0],
                son_ilan=tarihler[-1],
                son_ekap_id=son["ekap_id"] or "",
                periyot_gun=medyan,
                sapma_gun=sapma,
                periyot_tip=_periyot_tipi(medyan),
                guven=_seri_guven(len(uyeler), medyan, sapma),
                **_beklenen(tarihler[-1], medyan),
            ))
            islenen += 1
            if time.monotonic() >= deadline:
                timed_out = True
                break

        # Upsert + buda: tur içinde dokunulmayan eski seriler silinir (ör. iskelet
        # değiştiği için artık oluşmayan gruplar).
        if yazilacak:
            RecurringTenderSeries.objects.bulk_create(
                yazilacak,
                update_conflicts=True,
                unique_fields=["seri_anahtar", "idare_id"],
                update_fields=[
                    "idare_adi", "en_ust_idare_kod", "il_id", "okas_ana_kod",
                    "okas_ana_adi", "ihale_tip", "iskelet", "ornek_ihale_adi",
                    "ihale_sayisi", "ilk_ilan", "son_ilan", "son_ekap_id",
                    "periyot_gun", "sapma_gun", "periyot_tip", "guven",
                    "beklenen_ilan_tarihi", "beklenen_ay", "aktif",
                ],
                batch_size=1000,
            )
            _seri_para_agregalari(yazilacak)

        budanan = 0
        if not timed_out:
            budanan, _ = RecurringTenderSeries.objects.filter(
                guncelleme__lt=basla
            ).delete()

        run.items = islenen
        run.note = f"{'süre doldu ' if timed_out else ''}seri={islenen} budanan={budanan}"[:1000]
        return {"series": islenen, "pruned": budanan, "timed_out": timed_out}


def _periyot_tipi(medyan_gun):
    """Aralık medyanından periyot etiketi. Sınırlar takvim kaymalarına toleranslı."""
    if 330 <= medyan_gun <= 400:
        return "yillik"
    if 150 <= medyan_gun <= 220:
        return "6_aylik"
    if 75 <= medyan_gun <= 110:
        return "3_aylik"
    if 25 <= medyan_gun <= 40:
        return "aylik"
    return "duzensiz"


def _seri_guven(uye_sayisi, medyan, sapma):
    """
    Tahminin ne kadar güvenilir olduğu.

    ⚠️ Yalnızca üye sayısına bakmak yetmez: 5 üyeli ama aralıkları 30/400/60/380 gün olan
    bir "seri" tahmin üretmemeli. Bu yüzden **düzenlilik** (sapma/medyan) de şart.
    """
    if not medyan:
        return "dusuk"
    dagilim = sapma / medyan
    if uye_sayisi >= 4 and dagilim <= 0.15:
        return "yuksek"
    if uye_sayisi >= 3 and dagilim <= 0.30:
        return "orta"
    return "dusuk"


def _beklenen(son_ilan, medyan_gun):
    """Sıradaki ilan tahmini + serinin hâlâ canlı olup olmadığı."""
    beklenen = (son_ilan + timedelta(days=medyan_gun)).date()
    # 2 periyot geçtiyse seri muhtemelen sona ermiş (ihtiyaç kalktı / usul değişti).
    aktif = (timezone.now() - son_ilan).days < 2 * medyan_gun
    return {
        "beklenen_ilan_tarihi": beklenen,
        "beklenen_ay": beklenen.strftime("%Y-%m"),
        "aktif": aktif,
    }


def _seri_para_agregalari(seriler):
    """
    Serilerin bedel/indirim ortalamalarını sözleşmelerden doldurur.

    Ayrı adım: ana döngüde her seri için sorgu atmak N+1 olurdu. Burada seri başına tek
    toplu sorgu yapılır ve yalnızca para alanları güncellenir.
    """
    from django.db.models import Avg, Count, Sum

    from .models import Contract, RecurringTenderSeries

    guncel = []
    for s in seriler:
        agg = (
            Contract.objects.filter(
                tender__seri_anahtar=s.seri_anahtar, tender__idare_id=s.idare_id
            )
            .order_by()
            .aggregate(
                ort=Avg("sozlesme_bedeli_num"),
                ort_ind=Avg("indirim_orani"),
                n_ind=Count("indirim_orani"),
            )
        )
        if agg["ort"] is None and agg["n_ind"] == 0:
            continue
        satir = RecurringTenderSeries.objects.filter(
            seri_anahtar=s.seri_anahtar, idare_id=s.idare_id
        ).first()
        if satir is None:
            continue
        satir.ortalama_bedel = agg["ort"]
        satir.ortalama_indirim = agg["ort_ind"]
        satir.indirim_ornek = agg["n_ind"]
        guncel.append(satir)
    if guncel:
        RecurringTenderSeries.objects.bulk_update(
            guncel, ["ortalama_bedel", "ortalama_indirim", "indirim_ornek"], batch_size=500
        )


@shared_task(name="ekap.tasks.refresh_idare_id_set")
def refresh_idare_id_set():
    """
    "İhalede geçen idare_id" kümesini tazeler (`idare_detsis` filtresinin kesişim kümesi).

    ⚠️ **Neden ayrı bir görev:** hesap `DISTINCT idare_id` üzerinde ~500k satır tarar ve
    üretimde **~40 sn** ölçüldü. Eskiden bunu cache TTL'i dolduğunda gelen ilk kullanıcı
    isteği ödüyordu → `/ekap/tenders/?idare_detsis=...` ucu 30 dk'da bir 40 sn sürüyordu.
    Beat 10 dk'da bir tazelediği için cache (TTL 30 dk) pratikte hiç boşalmaz; istek
    yolundaki hesap yalnızca son çare olarak durur (bkz. `detsis_tree.tender_idare_id_set`).

    EKAP'a gitmez, tamamen DB-içi → `celery` kuyruğunda çalışır (bkz. CELERY_TASK_ROUTES).
    """
    from .detsis_tree import refresh_tender_idare_id_set

    ids = refresh_tender_idare_id_set()
    logger.info("refresh_idare_id_set: %s idare_id cache'lendi", len(ids))
    return {"idare_ids": len(ids)}
