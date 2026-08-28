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

# `enqueue_missing_detail` mükerrer kuyruklama koruması (bkz. sync_contractors).
_DETAY_KUYRUK_PREFIX = "ekap:detq:"
# TTL bir beat aralığından (5 dk) belirgin uzun, ama kalıcı değil: bir `sync_detail`
# görevi worker çökmesiyle kaybolursa satır 20 dk sonra yeniden denenebilmeli.
_DETAY_KUYRUK_TTL = 1200


@contextmanager
def _run(task_name, lock_ttl=3600):
    """
    SyncRun kaydı + Redis kilidi (aynı görev tekrar tetiklenirse atla).

    ⚠️ **`lock_ttl` görevin azami süresine göre seçilmeli.** Görev
    `CELERY_TASK_TIME_LIMIT` ile öldürülürse `finally` çalışmaz ve kilit TTL dolana
    kadar kalır — o süre boyunca görev **tamamen durur**. Sık koşan görevlerde (5 dk'da
    bir) varsayılan 1 saat çok uzundur: tek bir ölüm 12 turu birden yutar. Üretimde
    yaşandı (`backfill_tender_fields`, bkz. oradaki not).
    Kural: `lock_ttl ≈ 2 × azami tur süresi`, üst sınır olarak varsayılan kalsın.
    """
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


def _enqueue_detail(ekap_id, defer=True, queue=None, only_if_missing=False):
    """Detay görevini kuyruğa atar.

    ``queue`` verilirse yönlendirme tablosu **atlanır** — zamana duyarlı çağrılar
    (`sync_recent`) arşiv birikiminin arkasına düşmesin diye `ekap_oncelik`'e gider.
    ``only_if_missing`` bayat kuyruk girdilerinin boşa EKAP isteği atmasını önler.
    """
    if defer:
        sync_detail.apply_async(
            args=[ekap_id], kwargs={"only_if_missing": only_if_missing}, queue=queue
        )
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
def sync_detail(self, ekap_id, only_if_missing=False):
    """Tek ihalenin detay + ilanlarını çeker.

    ⚠️ ``only_if_missing=True`` **bayat kuyruk girdilerine karşı korumadır.** Ölçüm
    2026-08-11: `ekap` kuyruğunda 218.443 görev birikmişti — detayı eksik ihale
    sayısından (159.801) FAZLA, yani içinde çok sayıda mükerrer girdi vardı. Bu görev
    `detail_synced_at`'e bakmadan EKAP'a gittiği için, kuyrukta beklerken detayı
    başka bir yoldan gelmiş ihaleler tekrar isteniyor ve 1 istek/sn bütçesi boşa
    harcanıyordu. Boşluk doldurucu çağıranlar (`enqueue_missing_detail`, `backfill`)
    bu bayrağı verir; `refresh_stale` **vermez** — onun işi zaten tazelemek.
    """
    mark = f"{_DETAY_KUYRUK_PREFIX}{ekap_id}"
    if only_if_missing and Tender.objects.filter(
        ekap_id=str(ekap_id), detail_synced_at__isnull=False
    ).exists():
        cache.delete(mark)
        return {"ekap_id": ekap_id, "atlandi": "zaten_detayli"}
    try:
        sync_mod.sync_detail(ekap_id, EkapV2Client())
    except Exception as e:
        raise self.retry(exc=e)
    # ⚠️ İşareti iş BİTİNCE sil. Yalnızca kuyruğa atarken koyup TTL'e bırakmak,
    # tamamlanmayan kayıtların besleme penceresinin (ilk N satır) slotlarını
    # kilitlemesine yol açıyordu: 600'lük pencerenin 600'ü işaretli kalınca
    # `detay_kuyruk=0` oluyor ve besleme tümüyle duruyordu.
    cache.delete(mark)
    return {"ekap_id": ekap_id}


# ── Güncel (nightly) ───────────────────────────────────
@shared_task(name="ekap.tasks.sync_recent")
def sync_recent(days=None, max_pages=40, page_size=50, defer_detail=True):
    """Son N günde **YAYINLANAN** ihaleleri çekip upsert eder, detay kuyruğa atar.

    ⚠️ **Pencere EKAP tarafında `ilanTarihSaatBaslangic` ile uygulanır** — istemci
    tarafı kontrol imkânsızdır çünkü liste yanıtında `ilanTarihi` **%100 boştur**
    (backfill'de belgelenen tuzağın aynısı; `ilan_tarihi` ancak detay senkronunda
    `ilanList`'ten dolar).

    Eski sürüm ikisini birden yanlış yapıyordu ve **sessizce yanlış veri çekiyordu**:
      • `orderBy="ilanTarihi"` → EKAP'ın döndürmediği bir alana göre sıralama
      • tarih filtresi olarak `ihaleTarihSaatBaslangic=_window_floor()` (10 YILLIK
        taban) → "son N gün" hiçbir zaman EKAP'a gitmiyordu
      • durma koşulu `tender.ilan_tarihi < floor` → hep `None`, asla tetiklenmez
    Sonuç: her gece 10 yıllık arşivden keyfi 1000 satır (`SyncRun.items` daima tam
    1000 = 20×50, yani erken çıkış hiç olmuyordu) ve **günün yeni ihaleleri DB'ye
    hiç girmiyordu** — "bugün yayınlananlar"a bakan filtre/idare bildirimleri de
    bu yüzden boşa çalışıyordu.

    Canlı doğrulama (2026-08-11): filtresiz `totalCount=1.964.677`,
    `ilanTarihSaatBaslangic=<3 gün önce>` → **537**. EKAP filtreyi uyguluyor.

    Sıralama `ihaleTarihi` (DOLU alan) — sayfalama kararlılığı için şart; boş bir
    alana göre sıralamada sayfa sınırları kayar ve satır kaçırılabilir.
    """
    days = days or settings.EKAP_RECENT_DAYS
    with _run("sync_recent") as run:
        if run is None:
            return
        client = EkapV2Client()
        # ⚠️ EKAP yalnızca ISO kabul eder (DD.MM.YYYY → HTTP 400) — bkz. _window_floor.
        ilan_iso = (timezone.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        total = 0
        errors = 0
        total_count = 0
        for page in range(max_pages):
            body = client.build_search_body(
                orderBy="ihaleTarihi", siralamaTipi="asc",
                ilanTarihSaatBaslangic=ilan_iso,
                paginationSkip=page * page_size, paginationTake=page_size,
            )
            items, total_count = sync_mod.extract_list(client.search(body))
            if not items:
                break
            for item in items:
                tender, err = _upsert_item_safe(item)
                errors += err
                if tender:
                    total += 1
                    # ⚠️ **Öncelik kuyruğu ŞART.** `ekap` kuyruğu arşiv doldurmadan
                    # yüz binlerce görev biriktirebiliyor (ölçüldü: 218.443) ve FIFO
                    # olduğu için bugünün ihaleleri o sıranın SONUNA düşüyordu —
                    # `-ihale_tarihi DESC` beslemesi bunu kurtarmaz, sıra kuyruğa
                    # giriş anına göredir. Sonuç: günün ihalelerinin detayı (dolayısıyla
                    # `ilan_tarihi`) günlerce gelmiyor, "bugün yayınlananlar" bildirimleri
                    # boş küme üzerinde çalışıyordu.
                    # ⚠️ `only_if_missing` ŞART. Görev artık günde 12 kez koşuyor;
                    # bayrak olmadan pencerede duran ~700 ihalenin detayı her turda
                    # yeniden istenirdi (12 × 700 ≈ 8.400 istek/gün) — 1 istek/sn
                    # bütçesinin onda biri, tamamı mükerrer. Yeni ihalenin detayı
                    # ilk turda gelir; tazelik `refresh_stale`'in işidir.
                    _enqueue_detail(
                        tender.ekap_id, defer=defer_detail, queue="ekap_oncelik",
                        only_if_missing=True,
                    )
            if (page + 1) * page_size >= (total_count or 0):
                break
        run.items = total
        run.errors = errors
        # Pencereyi taşıyorsak sessizce kırpıyoruz demektir → görünür olsun.
        run.note = f"ilan>={ilan_iso} toplam={total_count}"
        if total_count and total < total_count:
            run.note += f" ⚠️ EKSİK (max_pages={max_pages} yetmedi)"
        _update_checkpoint("recent", newest=timezone.now())
        return {"upserted": total, "errors": errors, "total_count": total_count}


# ── Backfill (sürekli, yavaş) ──────────────────────────
@shared_task(name="ekap.tasks.backfill")
def backfill(max_pages=None, page_size=50, defer_detail=True):
    """Pencere tabanından (son N yıl) ileriye doğru imleçle geçmişi doldurur.

    Arama ``ihaleTarihSaatBaslangic`` ile EKAP tarafında son ``EKAP_BACKFILL_YEARS``
    yıla sınırlanır; ``ihaleTarihi`` **asc** sıralanır (en eski → yeni). Böylece
    DB'deki asıl boşluk (eski yıllar) önce dolar ve imleç, en yeni kayıtlar listenin
    sonuna eklendiği için kaymaz. ``skip >= total_count`` (pencere içi toplam) ya da
    boş sayfada biter.

    ⚠️ **Hızın kısıtı `max_pages`'tir, EKAP throttle'ı DEĞİL** (üretimde ölçüldü,
    2026-08): tur başına tam 500 kayıt işleniyordu (10 sayfa × 50) ve `ekap` kuyruğu
    **boştu** (`LLEN ekap = 0`), yani 1 istek/sn bütçesinin büyük kısmı kullanılmıyordu.
    Görev 10 sayfayı ~10 saniyede çekip 15 dakika boş bekliyordu → 43.500 kayıt/gün,
    691k kalan için ~16 gün.

    Liste taraması aslında **ucuzdur**: 691k kaydı gezmek sayfa başına 50 kayıtla yalnızca
    ~13.8k istek, 1 istek/sn ile ~4 saat. Asıl maliyet **detay** istekleridir; ama detayı
    zaten çekilmiş ihaleler için istek atılmıyor (aşağıdaki guard), yani gerçek yük yalnızca
    eksik yıllardır. Bu yüzden `max_pages`'i büyütmek throttle'a dokunmadan işi kısaltır:
    liste taraması hızlanır, detaylar kuyrukta birikip 1 istek/sn ile akar (kuyruğun geçici
    büyümesi normaldir, sorun değildir).

    `EKAP_BACKFILL_MAX_PAGES` ile ayarlanır (env).

    ⚠️ **Süre bütçesi `max_pages`'ten bağımsız bir güvenlik ağıdır** ve şart:
    tur maliyeti ≈ `max_pages` saniye (throttle) + upsert + EKAP gecikmesi, yani
    `max_pages=40` ile `CELERY_TASK_TIME_LIMIT=300`'ün tam dibinde koşuluyor.
    Ölçüm 2026-08-11: başarılı turlar 3-5 dk sürüyordu ama bir kısmı sınırı aşıp
    **öldürülüyordu** (`SyncRun.status='running'`, `finished_at` boş) — görev
    öldüğünde `finally` çalışmadığı için Redis kilidi 1 saatlik TTL'i dolana kadar
    kalıyor ve backfill 15 dakikada bir yerine **saatte bir** koşuyordu (turlar arası
    ölçülen boşluklar: 56, 69, 75 dk). Bütçe dolunca sayfa döngüsünden temiz çıkılır,
    imleç kaydedilir, sonraki tetik kaldığı yerden devam eder.
    ⚠️ `lock_ttl` de 600'e indirildi: yine de öldürülen bir tur, kilidi 1 saat değil
    10 dakika tutsun."""
    import time

    if max_pages is None:
        max_pages = settings.EKAP_BACKFILL_MAX_PAGES
    deadline = time.monotonic() + settings.EKAP_BACKFILL_MAX_SECONDS
    with _run("backfill", lock_ttl=600) as run:
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
        # ⚠️ Bütçeyle çıkışı EKAP hatasından AYRI tut: ikisini aynı değişkene yazmak
        # `SyncRun.note`'a "EKAP kısmi" yazdırıp normal bir bütçe çıkışını EKAP arızası
        # gibi gösteriyordu — teşhis ederken yanıltır.
        butce_doldu = False
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
                        _enqueue_detail(
                            tender.ekap_id, defer=defer_detail, only_if_missing=True
                        )
                        enqueued += 1
                    if tender.ihale_tarihi:
                        oldest = tender.ihale_tarihi if oldest is None else min(oldest, tender.ihale_tarihi)
            skip += page_size
            if (oldest and oldest < floor) or skip >= (total_count or 0):
                cp.done = True
                break
            # Süre bütçesi doldu → temiz çık. İmleç aşağıda kaydedilir; öldürülmek
            # yerine kendi isteğiyle bitmek kilidin de düzgün bırakılmasını sağlar.
            if time.monotonic() >= deadline:
                butce_doldu = True
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
        elif butce_doldu:
            run.note += f" · süre bütçesi doldu ({settings.EKAP_BACKFILL_MAX_SECONDS} sn)"
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
    max_tenders=50000, max_seconds=None, enqueue_missing_detail=True, missing_limit=None
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

    with _run("sync_contractors", lock_ttl=600) as run:
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
                # ⚠️ **Artımlı modda imleç yok** → hata alan kayıt `contractors_synced_at`
                # güncellenmediği için sorguya HER turda geri gelir ve kalıcı bir bozukluk
                # görevi sonsuza dek meşgul eder. Üretimde yaşandı (2026-08-27): tek bir
                # 2019 ihalesi her turda `errors=1` üretiyordu. Süpürme modunda aynı
                # koruma PK imlecinin `try`'dan önce ilerletilmesiyle zaten var.
                # ⚠️ Damgayı ilerletmek kaydı **atlar**, düzeltmez: hata `SyncRun.errors`'a
                # sayılır ve log'a yazılır — sessizce yutulmaz. Detay bir sonraki
                # `refresh_stale` turunda tazelenince kayıt yeniden denenir.
                if not sweeping:
                    Tender.objects.filter(pk=tender.pk).update(
                        contractors_synced_at=timezone.now()
                    )
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
        #
        # ⚠️ **Detay borcunu eritme hızının asıl düğmesi burasıdır** (`EKAP_MISSING_DETAIL_LIMIT`).
        # Ölçüm 2026-08-11: `LLEN ekap = 0` — kuyruk boş, 5 worker boşta bekliyor, hız
        # 2.273/saat (throttle tavanının %63'ü). Bu dal 50×12 tur = 600/saat besliyordu;
        # `backfill` (imleç detayı dolu yıllarda) ve `refresh_stale` (son 1 yıl) eski
        # arşiv boşluğuna hiç dokunmuyor, yani tek besleyici buydu.
        #
        # ⚠️ **Mükerrer kuyruklama koruması şart.** Sorgu her turda `-ihale_tarihi`
        # sıralı ilk N satırı döndürür; bir tur içinde işlenmeyen satır sonraki turda
        # yeniden kuyruğa girerdi. `sync_detail` `detail_synced_at`'e bakmadan EKAP'a
        # gittiği için bu, kurtarmaya çalıştığımız throttle slotlarını mükerrer istekle
        # harcamak demekti. `cache.add` (Redis SETNX) işareti bunu keser; TTL bir tur
        # aralığından uzun (kuyrukta bekleyen satır yeniden atılmasın), sonsuz değil
        # (bir görev düşerse satır sonunda yeniden denensin).
        enqueued = 0
        if missing_limit is None:
            missing_limit = settings.EKAP_MISSING_DETAIL_LIMIT
        if enqueue_missing_detail and missing_limit > 0:
            for ekap_id in (
                Tender.objects.filter(detail_synced_at__isnull=True)
                .exclude(ekap_id="")
                .order_by("-ihale_tarihi")
                .values_list("ekap_id", flat=True)[:missing_limit]
            ):
                if not cache.add(f"{_DETAY_KUYRUK_PREFIX}{ekap_id}", 1, timeout=_DETAY_KUYRUK_TTL):
                    continue  # bu ihale zaten kuyrukta bekliyor
                _enqueue_detail(ekap_id, only_if_missing=True)
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


@shared_task(name="ekap.tasks.refresh_market_stats")
def refresh_market_stats():
    """
    Pazar panosu özetlerini (`MarketStat` + `MarketYearStat`) yeniden hesaplar.

    ⚠️ **EKAP'a gitmez** ve `.values().annotate()` kullandığı için `detail_raw`
    TOAST'ına **hiç dokunmaz** → `sync_contractors` süpürmesiyle pencere çakışması
    YOKTUR, gece penceresi kısıtı gerekmez (`detect_recurring_series` ile aynı gerekçe).
    Bu yüzden `celery` kuyruğuna yönlendirilir (settings'te `ekap.tasks.*` joker'inden
    ÖNCE gelen istisna) — tek concurrency'li `ekap` kuyruğunu bloklamamalı.

    Ölçülen tam yeniden hesap ~4 sn (`CELERY_TASK_TIME_LIMIT=300`'ün çok altında),
    bu yüzden süre bütçesi/yıl rotasyonu gibi karmaşıklıklar eklenmedi.
    """
    from . import market as market_mod

    with _run("refresh_market_stats") as run:
        if run is None:
            return
        sonuc = market_mod.refresh_market_stats()
        run.items = sonuc["gruplar"]
        run.note = f"grup={sonuc['gruplar']} yil={sonuc['yillar']}"
        return sonuc


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

    # ── Ön kontroller: `_run`'DAN ÖNCE ────────────────────────────────────────
    # ⚠️ Bilinçli olarak `_run` dışında: görev 5 dk'da bir tetikleniyor ve iş yapamadığı
    # her turda bir `SyncRun` satırı yaratmak günde ~288 boş kayıt demekti. Admin'de
    # hepsi `ok / 0 / 0` görünüp sebebi göstermiyordu → gerçek çalışmalar bu gürültünün
    # içinde kayboluyor ve "hiç çalışmamış" izlenimi doğuyordu.
    # Artık **yalnızca gerçekten iş yapılan turlar** SyncRun'a yazılır; atlama sebepleri
    # log'a düşer (aşağıdaki `logger.info`) ve dönüş değerinde görünür.
    cp = SyncCheckpoint.objects.filter(name="tender_fields").first()
    if cp is not None and cp.done:
        return {"skipped": "tamamlandi"}

    # Öncelik: yüklenici süpürmesi bitmeden başlama (ikisi de detail_raw okuyor).
    yuklenici_cp = SyncCheckpoint.objects.filter(name="contractors").first()
    if yuklenici_cp is not None and not yuklenici_cp.done:
        kalan = (yuklenici_cp.extra or {}).get("last_tender_pk")
        logger.info(
            "backfill_tender_fields: yüklenici süpürmesi sürüyor (imleç=%s), sıra bekleniyor",
            kalan,
        )
        return {"skipped": "contractor_sweep_active", "yuklenici_imlec": kalan}

    hour = timezone.localtime().hour
    if not (settings.PRO_BACKFILL_START <= hour < settings.PRO_BACKFILL_END):
        logger.info(
            "backfill_tender_fields: gece penceresi dışında (saat=%s, pencere=%s-%s)",
            hour, settings.PRO_BACKFILL_START, settings.PRO_BACKFILL_END,
        )
        return {"skipped": "peak_hours", "hour": hour}

    with _run("backfill_tender_fields", lock_ttl=600) as run:
        if run is None:
            return
        # Kilit alındıktan sonra checkpoint'i (varsa yaratarak) tazele.
        cp, _ = SyncCheckpoint.objects.get_or_create(name="tender_fields")
        if cp.done:
            return {"skipped": "tamamlandi"}

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

        # ⚠️ `kalan` sayımı SICAK YOLDAN ÇIKARILDI. `detail_raw IS NOT NULL` indeksli
        # değil ve imleçten sonraki yüz binlerce satırı tarıyor; cache zaten süpürme
        # yüzünden hırpalanmışken bu tek sorgu on saniyeler sürebiliyor. 210 sn'lik
        # bütçenin üstüne binince tur `CELERY_TASK_TIME_LIMIT=300`'ü aşıp ÖLDÜRÜLÜYOR →
        # `finally` çalışmıyor → Redis kilidi serbest kalmıyor → görev TTL boyunca
        # tamamen duruyor. (Üretimde yaşandı: 19:50 turu `running`'de asılı kaldı,
        # sonraki ~1 saat hiç tur çalışmadı.)
        # Artık yalnızca iş bittiğinde (süre dolmadıysa) sayılıyor; süpürme sırasında
        # imleç raporlanıyor — ilerleme oradan da izlenebilir.
        kalan = None
        if not timed_out:
            kalan = Tender.objects.filter(detail_raw__isnull=False, pk__gt=last_pk).count()
        run.items = processed
        run.errors = errors
        run.note = (
            f"{'süre doldu ' if timed_out else ''}imleç={last_pk}"
            + (f" kalan={kalan}" if kalan is not None else "")
        )[:1000]
        return {
            "tenders": processed,
            "errors": errors,
            "cursor": last_pk,
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

        yazilacak, islenen, timed_out, para = [], 0, False, 0
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
                    # ⚠️ `guncelleme` ŞART: `auto_now` yalnızca INSERT yolunda yazılır.
                    # Buradan çıkarılırsa zaten var olan seriler UPDATE edilirken
                    # `guncelleme` ESKİ değerinde kalır ve hemen aşağıdaki budama
                    # (`guncelleme__lt=basla`) onları siler. Üretimde yaşandı: 19.527
                    # seri yazıldı, 21.910 budandı, geriye yalnızca **yeni eklenen**
                    # 10.937 kaldı — yani her tur mevcut serilerin tamamı siliniyordu.
                    # Aynı sebeple `_seri_para_agregalari` de onları atlıyordu.
                    "guncelleme",
                ],
                batch_size=1000,
            )
            para = _seri_para_agregalari(basla)

        budanan = 0
        if not timed_out:
            budanan, _ = RecurringTenderSeries.objects.filter(
                guncelleme__lt=basla
            ).delete()

        run.items = islenen
        run.note = (
            f"{'süre doldu ' if timed_out else ''}seri={islenen} "
            f"para={para} budanan={budanan}"
        )[:1000]
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


def _seri_para_agregalari(basla):
    """
    Serilerin bedel/indirim ortalamalarını sözleşmelerden doldurur — **tek sorguda**.

    ⚠️ **Eski sürüm N+1'di ve görevi öldürüyordu.** Docstring'i "ana döngüde her seri için
    sorgu atmak N+1 olurdu, burada toplu yapılır" diyordu ama kod tam da onu yapıyordu:
    seri başına bir `aggregate` + bir `.first()` = 21.910 seri × 2 = **43.820 sorgu**.
    Ana döngü süre bütçesine sığıyor, bu adım `CELERY_TASK_TIME_LIMIT=300`'ü aşıyor ve
    görev öldürülüyordu → `SyncRun.status='running'`, `finished_at` boş, ve **21.910
    serinin hiçbirinde `ortalama_bedel` yoktu** (mobilde "Veri yok" olarak görünüyordu).
    Veri eksikliği değildi: örnek seride 863 sözleşmenin 863'ünde de bedel doluydu.

    Şimdi: sözleşmeler `(seri_anahtar, idare_id)` ile **bir kez** gruplanır, sonuç
    sözlüğe alınır, seri satırları tek `bulk_update` ile yazılır.

    ``basla`` bu turun başlangıç zamanı; yalnızca bu turda yazılan/güncellenen seriler
    işlenir (budama mantığıyla aynı işaret).
    """
    from django.db.models import Avg, Count

    from .models import Contract, RecurringTenderSeries

    # ⚠️ `.order_by()` ŞART: `Contract.Meta.ordering` GROUP BY'a sızarsa gruplama bozulur.
    agg = {
        (r["tender__seri_anahtar"], r["tender__idare_id"]): r
        for r in (
            Contract.objects.exclude(tender__seri_anahtar="")
            .order_by()
            .values("tender__seri_anahtar", "tender__idare_id")
            .annotate(
                ort=Avg("sozlesme_bedeli_num"),
                ort_ind=Avg("indirim_orani"),
                n_ind=Count("indirim_orani"),
            )
        )
    }
    if not agg:
        return 0

    guncel = []
    for satir in RecurringTenderSeries.objects.filter(
        guncelleme__gte=basla
    ).only("id", "seri_anahtar", "idare_id").iterator(chunk_size=2000):
        a = agg.get((satir.seri_anahtar, satir.idare_id))
        if a is None:
            continue
        satir.ortalama_bedel = a["ort"]
        satir.ortalama_indirim = a["ort_ind"]
        satir.indirim_ornek = a["n_ind"]
        guncel.append(satir)
        if len(guncel) >= 2000:
            RecurringTenderSeries.objects.bulk_update(
                guncel, ["ortalama_bedel", "ortalama_indirim", "indirim_ornek"],
                batch_size=500,
            )
            guncel = []
    if guncel:
        RecurringTenderSeries.objects.bulk_update(
            guncel, ["ortalama_bedel", "ortalama_indirim", "indirim_ornek"],
            batch_size=500,
        )
    return len(agg)


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


# ══════════════════════════════════════════════════════════════════════════
# Anahtar kelime (keyword) boru hattı
#
# Akış:  backfill_tender_kalip → dispatch → poll → process → propagate
#         (kalıp çıkar)        (AI'ya sor) (bekle) (yaz)    (ihalelere yay)
#
# ⚠️ Hiçbiri EKAP'a gitmez; hepsi `celery` kuyruğunda (settings CELERY_TASK_ROUTES).
# ⚠️ Hepsi `backfill_tender_fields` iskeletini kullanır: ucuz ön kontroller `_run`'DAN
# ÖNCE (iş yapılmayan turda boş `SyncRun` yazmamak için), süre bütçesi
# `CELERY_TASK_TIME_LIMIT`(300) altında, PK imleci `SyncCheckpoint`'te.
# ══════════════════════════════════════════════════════════════════════════

def _keyword_harcama():
    """Şimdiye kadarki toplam AI maliyeti ($) — bütçe tavanı kontrolü için."""
    from django.db.models import Sum

    from .models import KeywordBatch

    agg = KeywordBatch.objects.aggregate(i=Sum("input_tokens"), o=Sum("output_tokens"))
    return ((agg["i"] or 0) * settings.KEYWORD_FIYAT_IN
            + (agg["o"] or 0) * settings.KEYWORD_FIYAT_OUT) / 1_000_000


@shared_task(name="ekap.tasks.backfill_tender_kalip")
def backfill_tender_kalip(max_tenders=200000, max_seconds=None, batch_size=1000):
    """
    `Tender.kalip_hash` doldurur ve `TenderNamePattern` sözlüğünü kurar.

    ⚠️ Bu görev `detail_raw`'a **hiç dokunmaz** — kaynak yalnızca `ihale_adi` (satır
    içi, TOAST dışı). Bu yüzden `sync_contractors` süpürmesiyle buffer cache yarışına
    girmez ve gece penceresi kısıtı GEREKMEZ; gündüz de koşabilir.

    İmleç: `SyncCheckpoint(name="tender_kalip").extra["last_tender_pk"]`.
    """
    import hashlib
    import time
    from collections import Counter

    from . import keywords as kw_mod
    from .models import TenderNamePattern

    cp = SyncCheckpoint.objects.filter(name="tender_kalip").first()
    if cp is not None and cp.done:
        return {"skipped": "tamamlandi"}

    with _run("backfill_tender_kalip", lock_ttl=600) as run:
        if run is None:
            return
        cp, _ = SyncCheckpoint.objects.get_or_create(name="tender_kalip")
        if cp.done:
            return {"skipped": "tamamlandi"}
        if max_seconds is None:
            max_seconds = settings.KEYWORD_KALIP_MAX_SECONDS

        last_pk = int((cp.extra or {}).get("last_tender_pk") or 0)
        qs = (Tender.objects.filter(pk__gt=last_pk).exclude(ihale_adi="")
              .only("pk", "ihale_adi", "kalip_hash").order_by("pk")[:max_tenders])

        processed = errors = 0
        batch, sayac, ornek = [], Counter(), {}
        deadline = time.monotonic() + max_seconds
        timed_out = False

        for tender in qs.iterator(chunk_size=2000):
            last_pk = max(last_pk, tender.pk)      # ⚠️ try'DAN ÖNCE — bozuk satır
            try:                                   #    imleci kilitlemesin
                # ⚠️ `kalip_norm` BİR KEZ hesaplanır. Önceki sürüm hem
                # `kalip_hash()` içinde hem `ornek.setdefault`'ta çağırıyordu;
                # satır başına iki kez regex + Türkçe katlama = boşa CPU.
                norm = kw_mod.kalip_norm(tender.ihale_adi)
                h = (hashlib.sha1(norm.encode("utf-8")).hexdigest()
                     if len(norm.split()) >= 2 else "")
                if h and tender.kalip_hash != h:
                    tender.kalip_hash = h
                    batch.append(tender)
                if h:
                    sayac[h] += 1
                    ornek.setdefault(h, (norm, (tender.ihale_adi or "")[:500]))
                processed += 1
            except Exception as exc:               # noqa: BLE001
                errors += 1
                logger.warning("kalip_hash hatası (pk=%s): %s", tender.pk, exc)

            if len(batch) >= batch_size:
                Tender.objects.bulk_update(batch, ["kalip_hash"])
                batch = []
            if time.monotonic() >= deadline:
                timed_out = True
                break

        if batch:
            Tender.objects.bulk_update(batch, ["kalip_hash"])

        # Kalıp sözlüğü: yalnızca YENİ kalıplar açılır.
        #
        # ⚠️ `ihale_sayisi` BU GÖREVDE GÜNCELLENMEZ ve bu bilinçli. İki sürüm denendi
        # ve ikisi de turu yavaşlattı (üretim ölçümü, 240 sn'lik turda):
        #   • kalıp başına `.update(F(...)+n)`  → N+1, tur süresi kalıp sayısıyla katlanıyor
        #   • `IN` ile çek + `bulk_update`      → tur 1: 22.077 ihale → tur 2: 12.109 (%45 düşüş)
        # Sebep aynı: her tur, o turda görülen ON BİNLERCE kalıbı DB'den çekip geri
        # yazmak zorunda kalıyor. Oysa `ihale_sayisi` yalnızca `dispatch` sırasını
        # belirliyor — gerçek zamanlı olması hiç gerekmiyor. Tek bir GROUP BY ile
        # sonradan kesin olarak hesaplanır (`refresh_kalip_sayaclari`), o da arşiv
        # taraması bittiğinde otomatik çağrılır.
        #
        # `ignore_conflicts=True` mevcut kalıpları zaten atlar → mevcut/yeni ayrımı
        # için ek SELECT'e de gerek kalmaz.
        yeni = 0
        if sayac:
            TenderNamePattern.objects.bulk_create(
                [TenderNamePattern(kalip_hash=h, kalip_norm=ornek[h][0],
                                   ornek_ad=ornek[h][1], ihale_sayisi=adet,
                                   durum="pending")
                 for h, adet in sayac.items()],
                ignore_conflicts=True, batch_size=2000,
            )
            yeni = len(sayac)

        if processed == 0 and errors == 0 and not timed_out:
            cp.done = True
            # Arşiv bitti → sayaçları tek GROUP BY ile kesinleştir (dispatch sırası
            # bundan sonra doğru olur).
            refresh_kalip_sayaclari()
        cp.extra = {**(cp.extra or {}), "last_tender_pk": last_pk}
        cp.save()

        run.items = processed
        run.errors = errors
        run.note = (f"kalıp={len(sayac)} yeni={yeni} imleç={last_pk} "
                    f"{'süre doldu' if timed_out else ''}")[:1000]
        return {"tenders": processed, "yeni_kalip": yeni, "cursor": last_pk,
                "timed_out": timed_out, "done": cp.done}


@shared_task(name="ekap.tasks.refresh_kalip_sayaclari")
def refresh_kalip_sayaclari():
    """
    `TenderNamePattern.ihale_sayisi`'ni tek GROUP BY ile kesinleştirir.

    ⚠️ Bu sayaç `dispatch` sırasını belirler (en çok ihaleyi kapsayan kalıp önce
    gider), yani AI bütçesi yarıda kesilse bile kapsamın çoğunun alınmasını sağlayan
    şeydir — ama gerçek zamanlı olması gerekmez. Doldurma turlarının içinde artırmak
    turu %45 yavaşlatıyordu (ölçüldü); burada tek sorguyla, kesin olarak hesaplanır.

    Doldurma tamamlandığında otomatik çağrılır; elle: `run_keywords --job sayac`.
    """
    from django.db import connection

    with _run("refresh_kalip_sayaclari", lock_ttl=1800) as run:
        if run is None:
            return
        if connection.vendor == "postgresql":
            with connection.cursor() as cur:
                cur.execute("""
                    UPDATE ekap_tendernamepattern p
                       SET ihale_sayisi = c.n
                      FROM (SELECT kalip_hash, count(*) AS n
                              FROM ekap_tender
                             WHERE kalip_hash <> ''
                             GROUP BY kalip_hash) c
                     WHERE p.kalip_hash = c.kalip_hash
                       AND p.ihale_sayisi IS DISTINCT FROM c.n
                """)
                guncellenen = cur.rowcount
        else:
            # ⚠️ `UPDATE ... FROM` Postgres'e özel; yerel geliştirme SQLite'a düşüyor
            # (bkz. settings: DATABASE_URL yoksa SQLite). ORM karşılığı yavaştır ama
            # yerel veri küçüktür — üretim yolu her zaman yukarıdaki tek sorgudur.
            from django.db.models import Count

            from .models import TenderNamePattern
            sayimlar = dict(
                Tender.objects.exclude(kalip_hash="")
                .values_list("kalip_hash")
                .annotate(n=Count("pk"))
                .values_list("kalip_hash", "n")
            )
            kayitlar = list(TenderNamePattern.objects.only("id", "kalip_hash",
                                                           "ihale_sayisi"))
            degisen = []
            for k in kayitlar:
                yeni_n = sayimlar.get(k.kalip_hash, 0)
                if k.ihale_sayisi != yeni_n:
                    k.ihale_sayisi = yeni_n
                    degisen.append(k)
            TenderNamePattern.objects.bulk_update(degisen, ["ihale_sayisi"],
                                                  batch_size=1000)
            guncellenen = len(degisen)
        run.items = guncellenen
        run.note = f"sayaç güncellenen kalıp={guncellenen}"
        return {"guncellenen": guncellenen}


@shared_task(name="ekap.tasks.dispatch_keyword_batches")
def dispatch_keyword_batches(max_batches=1):
    """
    `pending` kalıpları Anthropic Message Batches API'ye gönderir.

    ⚠️ Kalıplar **`-ihale_sayisi` sırasıyla** gönderilir. Bu, bütçe yarıda kesilse bile
    ihalelerin büyük kısmının keyword almış olmasını sağlar (üretim ölçümü: en sık %1
    kalıp ihalelerin %19'unu, %20 kalıp %47'sini kapsıyor).

    ⚠️ Üç sert kapı: kill switch, eşzamanlı batch tavanı, kümülatif $ tavanı.
    """
    from ai.prompts import KEYWORD_BATCH_SYSTEM, keyword_schema, keyword_user_mesaji
    from ai.services.claude import batch_olustur, get_api_key

    from . import keywords as kw_mod
    from .constants import SEKTOR_KODLARI
    from .models import KeywordBatch, TenderNamePattern

    if not settings.KEYWORD_AI_ENABLED:
        return {"skipped": "kill_switch"}

    ucus_halinde = KeywordBatch.objects.filter(
        durum__in=["created", "in_progress", "ended"]).count()
    if ucus_halinde >= settings.KEYWORD_MAX_INFLIGHT_BATCHES:
        return {"skipped": "inflight_limit", "aktif": ucus_halinde}

    harcama = _keyword_harcama()
    if harcama >= settings.KEYWORD_MAX_TOTAL_USD:
        logger.error("keyword: BÜTÇE TAVANI aşıldı (%.2f$ / %.2f$) — dispatch durdu",
                     harcama, settings.KEYWORD_MAX_TOTAL_USD)
        return {"skipped": "budget", "harcama": round(harcama, 2)}

    from .models import Keyword
    if Keyword.objects.count() >= settings.KEYWORD_MAX_UNIQUE:
        logger.error("keyword: tekil keyword tavanı aşıldı — dispatch durdu")
        return {"skipped": "keyword_limit"}

    with _run("dispatch_keyword_batches", lock_ttl=900) as run:
        if run is None:
            return
        per_req = settings.KEYWORD_PATTERNS_PER_REQUEST
        kalip_tavan = per_req * settings.KEYWORD_REQUESTS_PER_BATCH

        kaliplar = list(
            TenderNamePattern.objects.filter(durum="pending")
            .order_by("-ihale_sayisi", "pk")
            .only("id", "kalip_norm")[:kalip_tavan]
        )
        if not kaliplar:
            run.note = "gönderilecek kalıp yok"
            return {"kaliplar": 0}

        api_key = get_api_key()
        sema = keyword_schema(SEKTOR_KODLARI)
        istekler = []
        for i in range(0, len(kaliplar), per_req):
            grup = kaliplar[i:i + per_req]
            metinler = [k.kalip_norm for k in grup]
            oneriler = kw_mod.oneri_keywordleri(
                metinler, limit=settings.KEYWORD_ONERI_LIMIT)
            istekler.append({
                "custom_id": f"kw-{i // per_req}",
                "params": {
                    "model": settings.KEYWORD_AI_MODEL,
                    "max_tokens": settings.KEYWORD_MAX_TOKENS,
                    # ⚠️ System prompt SABİT + `cache_control` → prompt cache tutar.
                    # Öneri listesi (değişken) bilerek user mesajında; system'e
                    # konsaydı cache her istekte geçersiz olur, girdi maliyeti ~10×
                    # artardı.
                    "system": [{"type": "text", "text": KEYWORD_BATCH_SYSTEM,
                                "cache_control": {"type": "ephemeral"}}],
                    "messages": [{
                        "role": "user",
                        "content": keyword_user_mesaji(
                            [(k.id, k.kalip_norm) for k in grup], oneriler),
                    }],
                    "output_config": {"format": {"type": "json_schema", "schema": sema}},
                },
            })

        sonuc = batch_olustur(api_key, istekler)
        kayit = KeywordBatch.objects.create(
            batch_id=sonuc.id, durum="created", model=settings.KEYWORD_AI_MODEL,
            istek_sayisi=len(istekler), kalip_sayisi=len(kaliplar),
        )
        TenderNamePattern.objects.filter(pk__in=[k.pk for k in kaliplar]).update(
            durum="queued", batch=kayit)

        run.items = len(kaliplar)
        run.note = f"batch={sonuc.id} istek={len(istekler)} harcama=${harcama:.2f}"
        logger.info("keyword batch gönderildi: %s (%s kalıp)", sonuc.id, len(kaliplar))
        return {"batch_id": sonuc.id, "kaliplar": len(kaliplar),
                "istekler": len(istekler)}


@shared_task(name="ekap.tasks.poll_keyword_batches")
def poll_keyword_batches():
    """
    Açık batch'lerin durumunu yoklar; süresi dolanları `pending`'e iade eder.

    ⚠️ Anthropic batch'leri 24 saatte sona erer. Süresi dolan bir batch'in kalıpları
    `queued`'da bırakılırsa **sonsuza dek orada kalır** ve o kalıplar hiç keyword
    almaz — sessiz veri kaybı. Bu yüzden `KEYWORD_BATCH_MAX_HOURS` geçince kalıplar
    iade edilir ve `deneme` artırılır (2 denemeden sonra `error`).
    """
    from ai.services.claude import batch_durum, get_api_key

    from .models import KeywordBatch, TenderNamePattern

    aktif = list(KeywordBatch.objects.filter(durum__in=["created", "in_progress"]))
    if not aktif:
        return {"aktif": 0}

    with _run("poll_keyword_batches", lock_ttl=300) as run:
        if run is None:
            return
        api_key = get_api_key()
        biten = suresi_dolan = 0
        for kayit in aktif:
            yas = timezone.now() - kayit.created_at
            if yas > timedelta(hours=settings.KEYWORD_BATCH_MAX_HOURS):
                kayit.durum = "expired"
                kayit.note = f"{yas.total_seconds() / 3600:.0f} saat sonra süresi doldu"
                kayit.save(update_fields=["durum", "note"])
                # ⚠️ `deneme` artışı ile birlikte iade: kalıcı olarak bozuk bir kalıp
                # sonsuz döngüye girmesin.
                TenderNamePattern.objects.filter(batch=kayit, durum="queued").update(
                    durum="pending", batch=None, deneme=models_F_deneme())
                TenderNamePattern.objects.filter(
                    batch=kayit, durum="pending", deneme__gte=2).update(
                    durum="error", hata="batch iki kez süresi doldu")
                suresi_dolan += 1
                continue
            try:
                uzak = batch_durum(api_key, kayit.batch_id)
            except Exception as exc:               # noqa: BLE001
                logger.warning("batch durumu alınamadı (%s): %s", kayit.batch_id, exc)
                continue
            durum = getattr(uzak, "processing_status", "")
            if durum == "ended":
                kayit.durum = "ended"
                kayit.ended_at = timezone.now()
                kayit.save(update_fields=["durum", "ended_at"])
                biten += 1
            elif durum and kayit.durum != "in_progress":
                kayit.durum = "in_progress"
                kayit.save(update_fields=["durum"])

        run.items = len(aktif)
        run.note = f"biten={biten} expired={suresi_dolan}"
        return {"aktif": len(aktif), "biten": biten, "expired": suresi_dolan}


def models_F_deneme():
    """`deneme = deneme + 1` — `poll_keyword_batches` içinde okunabilirlik için."""
    from django.db.models import F

    return F("deneme") + 1


@shared_task(name="ekap.tasks.process_keyword_results")
def process_keyword_results(max_seconds=None):
    """
    Tamamlanmış batch'lerin sonuçlarını `Keyword` + `TenderNamePattern`'e yazar.

    ⚠️ **Yeniden akıtılabilir olmak zorunda.** Sonuç akışı baştan okunur (API kısmi
    okuma sunmuyor); süre bütçesi dolarsa görev kesilir ve bir sonraki tur akışı
    BAŞTAN okur. Bu yüzden `durum="ok"` kalıplar atlanır — aksi hâlde her tur aynı
    ilk N kalıbı yeniden işler ve batch hiç bitmez.

    ⚠️ Her sonuç kendi `id`'sini taşır (`TenderNamePattern.pk`). Konuma göre eşleme
    YAPILMAZ: model 25 kalıbı yanlış sıraya koyarsa sonuçlar sessizce yanlış ihalelere
    yazılırdı ve bunu fark etmenin yolu olmazdı.
    """
    import json
    import time

    from django.utils import timezone as tz

    from ai.services.claude import batch_sonuclari, get_api_key

    from . import keywords as kw_mod
    from .models import KeywordBatch, TenderNamePattern

    bitenler = list(KeywordBatch.objects.filter(durum="ended"))
    if not bitenler:
        return {"batch": 0}

    with _run("process_keyword_results", lock_ttl=600) as run:
        if run is None:
            return
        if max_seconds is None:
            max_seconds = settings.KEYWORD_PROCESS_MAX_SECONDS
        deadline = time.monotonic() + max_seconds
        api_key = get_api_key()
        toplam_ok = toplam_hata = 0
        timed_out = False

        for kayit in bitenler:
            in_tok = out_tok = 0
            try:
                akis = batch_sonuclari(api_key, kayit.batch_id)
            except Exception as exc:               # noqa: BLE001
                logger.warning("batch sonucu alınamadı (%s): %s", kayit.batch_id, exc)
                continue

            for oge in akis:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                tip = getattr(oge.result, "type", "")
                if tip != "succeeded":
                    toplam_hata += 1
                    continue
                mesaj = oge.result.message
                in_tok += getattr(mesaj.usage, "input_tokens", 0)
                out_tok += getattr(mesaj.usage, "output_tokens", 0)
                ham = "".join(b.text for b in mesaj.content
                              if getattr(b, "type", "") == "text")
                try:
                    veri = json.loads(ham)
                except json.JSONDecodeError:
                    toplam_hata += 1
                    logger.warning("batch %s: JSON parse edilemedi", kayit.batch_id)
                    continue
                toplam_ok += _kalip_sonuclarini_yaz(veri.get("sonuclar") or [], kayit)

            KeywordBatch.objects.filter(pk=kayit.pk).update(
                input_tokens=kayit.input_tokens + in_tok,
                output_tokens=kayit.output_tokens + out_tok,
            )
            if timed_out:
                break
            # Batch bitti: kalan `queued` kalıplar sonuç dönmemiş demektir → iade.
            TenderNamePattern.objects.filter(batch=kayit, durum="queued").update(
                durum="pending", deneme=models_F_deneme())
            KeywordBatch.objects.filter(pk=kayit.pk).update(
                durum="processed", islendi_at=tz.now(),
                basarili=toplam_ok, hatali=toplam_hata)

        run.items = toplam_ok
        run.errors = toplam_hata
        run.note = f"batch={len(bitenler)} {'süre doldu' if timed_out else ''}"[:1000]
        return {"islenen": toplam_ok, "hata": toplam_hata, "timed_out": timed_out}


def _kalip_sonuclarini_yaz(sonuclar, kayit):
    """Bir isteğin sonuçlarını kalıplara yazar → yazılan kalıp sayısı."""
    from django.utils import timezone as tz

    from . import keywords as kw_mod
    from .constants import SEKTORLER
    from .models import TenderNamePattern

    idler = [s.get("id") for s in sonuclar if isinstance(s.get("id"), int)]
    if not idler:
        return 0
    # ⚠️ Yalnızca BU batch'in kalıpları — model uydurma/başka bir id döndürürse
    # sonuç sessizce yanlış kalıba yazılırdı.
    mevcut = {k.pk: k for k in TenderNamePattern.objects.filter(
        pk__in=idler, batch=kayit).only("id", "durum")}

    yazilan = 0
    for s in sonuclar:
        kalip = mevcut.get(s.get("id"))
        if kalip is None or kalip.durum == "ok":       # bilinmeyen id ya da yeniden akış
            continue
        kanonikler = kw_mod.kanonik_liste(s.get("keywords"), azami=8)
        guven = s.get("guven")
        try:
            guven = float(guven)
        except (TypeError, ValueError):
            guven = 0.0
        sektor = s.get("sektor") or ""
        if sektor not in SEKTORLER:
            sektor = ""
        # ⚠️ Sektör fallback: AI "diger" derse ya da geçersiz değer dönerse
        # deterministik sözlük denenir. Ölçümde bazı örneklerde deterministik daha
        # isabetliydi ("istinat duvarı" → AI İnşaat, sözlük Yol/Altyapı).
        if sektor in ("", "diger"):
            det_s, puan = kw_mod.sektor_tahmin(kalip.kalip_norm, kanonikler)
            if puan:
                sektor = det_s

        if guven < settings.KEYWORD_MIN_GUVEN or not kanonikler:
            TenderNamePattern.objects.filter(pk=kalip.pk).update(
                durum="skipped", guven=guven, sektor=sektor,
                islendi_at=tz.now(), model=kayit.model)
            continue

        eslesme = kw_mod.keyword_upsert(kanonikler)
        TenderNamePattern.objects.filter(pk=kalip.pk).update(
            durum="ok", keyword_ids=[eslesme[m] for m in kanonikler if m in eslesme],
            sektor=sektor, guven=guven, islendi_at=tz.now(), model=kayit.model)
        yazilan += 1
    return yazilan


@shared_task(name="ekap.tasks.propagate_tender_keywords")
def propagate_tender_keywords(max_tenders=200000, max_seconds=None, batch_size=2000):
    """
    Kalıp sözlüğündeki keyword'leri ihalelere yayar (~5M `TenderKeyword` satırı).

    ⚠️ Boru hattının **tek yazma-ağır aşaması** → gece penceresi
    (`KEYWORD_PROPAGATE_START/END`). Gündüz koşarsa Postgres buffer cache'ini kirletip
    ihale aramasını diske düşürür; `sync_contractors` süpürmesinde ölçülmüş bir arıza
    (heap cache isabeti %53, olması gereken >%99).

    ⚠️ Yüklenici süpürmesi ve pro-kolon doldurma bitmeden başlamaz — üçü aynı gecede
    koşarsa üçü de yavaşlar. `KEYWORD_PROPAGATE_IGNORE_SWEEP=True` acil kaçıştır.

    ⚠️ "İşlendi" işareti için yeni kolon YOK: `Tender.sektor != ""` yeterli. Şema
    sektörü zorunlu kılıyor (en kötü `"diger"`), imleç de zaten var. `ignore_conflicts`
    sayesinde tur tekrarı zararsızdır.
    """
    import time

    from .models import TenderKeyword, TenderNamePattern

    cp = SyncCheckpoint.objects.filter(name="tender_keywords").first()
    if cp is not None and cp.done:
        return {"skipped": "tamamlandi"}

    if not settings.KEYWORD_PROPAGATE_IGNORE_SWEEP:
        for ad in ("contractors", "tender_fields"):
            onceki = SyncCheckpoint.objects.filter(name=ad).first()
            if onceki is not None and not onceki.done:
                logger.info("propagate_tender_keywords: %s sürüyor, sıra bekleniyor", ad)
                return {"skipped": f"{ad}_active"}

    hour = timezone.localtime().hour
    if not (settings.KEYWORD_PROPAGATE_START <= hour < settings.KEYWORD_PROPAGATE_END):
        return {"skipped": "peak_hours", "hour": hour}

    with _run("propagate_tender_keywords", lock_ttl=600) as run:
        if run is None:
            return
        cp, _ = SyncCheckpoint.objects.get_or_create(name="tender_keywords")
        if cp.done:
            return {"skipped": "tamamlandi"}
        if max_seconds is None:
            max_seconds = settings.KEYWORD_PROPAGATE_MAX_SECONDS

        last_pk = int((cp.extra or {}).get("last_tender_pk") or 0)
        qs = (Tender.objects.filter(pk__gt=last_pk).exclude(kalip_hash="")
              .only("pk", "kalip_hash", "sektor").order_by("pk")[:max_tenders])

        # Kalıp sözlüğü tur boyunca bellekte tutulur — ihale başına SELECT atmak
        # 1M satırda sessiz N+1 olurdu.
        sozluk = {}
        processed = baglanan = errors = 0
        baglar, sektorlu = [], []
        deadline = time.monotonic() + max_seconds
        timed_out = False

        for tender in qs.iterator(chunk_size=2000):
            last_pk = max(last_pk, tender.pk)
            processed += 1
            try:
                kayit = sozluk.get(tender.kalip_hash, False)
                if kayit is False:
                    kayit = (TenderNamePattern.objects
                             .filter(kalip_hash=tender.kalip_hash, durum="ok")
                             .only("keyword_ids", "sektor").first())
                    sozluk[tender.kalip_hash] = kayit
                if kayit is None:
                    continue
                for kid in kayit.keyword_ids or []:
                    baglar.append(TenderKeyword(tender_id=tender.pk, keyword_id=kid))
                if kayit.sektor and tender.sektor != kayit.sektor:
                    tender.sektor = kayit.sektor
                    sektorlu.append(tender)
                baglanan += 1
            except Exception as exc:               # noqa: BLE001
                errors += 1
                logger.warning("propagate hatası (pk=%s): %s", tender.pk, exc)

            if len(baglar) >= batch_size:
                TenderKeyword.objects.bulk_create(baglar, ignore_conflicts=True)
                baglar = []
            if len(sektorlu) >= batch_size:
                Tender.objects.bulk_update(sektorlu, ["sektor"])
                sektorlu = []
            if time.monotonic() >= deadline:
                timed_out = True
                break

        if baglar:
            TenderKeyword.objects.bulk_create(baglar, ignore_conflicts=True)
        if sektorlu:
            Tender.objects.bulk_update(sektorlu, ["sektor"])

        if processed == 0 and errors == 0 and not timed_out:
            cp.done = True
        cp.extra = {**(cp.extra or {}), "last_tender_pk": last_pk}
        cp.save()

        run.items = baglanan
        run.errors = errors
        run.note = (f"taranan={processed} bağlanan={baglanan} imleç={last_pk} "
                    f"{'süre doldu' if timed_out else ''}")[:1000]
        return {"taranan": processed, "baglanan": baglanan, "cursor": last_pk,
                "timed_out": timed_out, "done": cp.done}


@shared_task(name="ekap.tasks.refresh_keyword_df")
def refresh_keyword_df():
    """
    `Keyword.kullanim_sayisi` (df) ve `pasif` bayrağını yeniden hesaplar.

    ⚠️ Bu görev benzerlik sorgusunun **performans regülatörüdür**, kozmetik değil.
    Sorgunun maliyeti taranan index tuple sayısıyla, o da probe'a giren keyword'lerin
    df toplamıyla orantılı. İki uçtaki keyword'ler `pasif` işaretlenir:

      * `df == 1` → tanımı gereği hiçbir kesişim üretemez (tek ihalede var).
      * `df > KEYWORD_MAX_DF` → o kadar yaygın ki ayırt etmiyor, ama probe'a girerse
        on binlerce tuple taratır.

    Ayrıca öneri listesinin eşiği (`kullanim_sayisi >= 2`) bu sayaca dayanır.
    """
    from django.db.models import Count

    from .models import Keyword

    with _run("refresh_keyword_df", lock_ttl=1800) as run:
        if run is None:
            return
        sayimlar = (Keyword.objects.annotate(n=Count("ihale_baglari"))
                    .values_list("id", "n", "kullanim_sayisi", "pasif"))
        guncel, pasif_sayi = [], 0
        for kid, n, eski, eski_pasif in sayimlar.iterator(chunk_size=5000):
            pasif = (n <= 1) or (n > settings.KEYWORD_MAX_DF)
            if n != eski or pasif != eski_pasif:
                guncel.append(Keyword(id=kid, kullanim_sayisi=n, pasif=pasif))
            pasif_sayi += bool(pasif)
            if len(guncel) >= 5000:
                Keyword.objects.bulk_update(guncel, ["kullanim_sayisi", "pasif"])
                guncel = []
        if guncel:
            Keyword.objects.bulk_update(guncel, ["kullanim_sayisi", "pasif"])

        toplam = Keyword.objects.count()
        run.items = toplam
        run.note = f"toplam={toplam} pasif={pasif_sayi}"
        return {"keyword": toplam, "pasif": pasif_sayi}
