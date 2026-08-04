"""Celery uygulaması — config paketi."""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("ihaletakip")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# ── Celery Beat — periyodik görevler ───────────────────
app.conf.beat_schedule = {
    # Eski AI analiz cache kayıtlarını temizle (her gün 03:00)
    "cleanup-expired-analyses": {
        "task": "ai.tasks.cleanup_expired_analyses",
        "schedule": crontab(hour=3, minute=0),
        "kwargs": {"days": 30},
    },
    # ── Bildirim servisi (kademeli düzen, kullanıcı başına tek özet push) ──
    # İhale Asistanı: günlük öneri digest'i + push (her gün 07:00 — ekap sync sonrası)
    "assistant-match-recommendations": {
        "task": "assistant.tasks.match_recommendations",
        "schedule": crontab(hour=7, minute=0),
    },
    # OKAS önerisi: kayıtlı ihalelerin OKAS kodlarıyla son 24s yayınlanan ihaleler
    # (her gün 08:00 — Free/Pro herkese; asistan önerisinden ayrı, premium değil)
    "recommend-by-saved-okas": {
        "task": "tenders.tasks.recommend_by_saved_okas",
        "schedule": crontab(hour=8, minute=0),
    },
    # İhale alarmları: ihale günü / doküman değişikliği / sonuçlandı (her gün 09:00)
    "check-tender-alarms": {
        "task": "tenders.tasks.check_tender_alarms",
        "schedule": crontab(hour=9, minute=0),
    },
    # Kayıtlı filtre eşleşmesi: filtreye uyan yeni ihaleler (her gün 10:00)
    "check-saved-filter-matches": {
        "task": "tenders.tasks.check_saved_filter_matches",
        "schedule": crontab(hour=10, minute=0),
    },
    # Favori idare eşleşmesi: favori idarelerin yeni yayınladığı ihaleler (her gün 11:00)
    "check-favorite-authority-matches": {
        "task": "tenders.tasks.check_favorite_authority_matches",
        "schedule": crontab(hour=11, minute=0),
    },
    # 30 günden eski okunmuş bildirimleri temizle (her gün 04:00)
    "cleanup-old-notifications": {
        "task": "tenders.tasks.cleanup_old_notifications",
        "schedule": crontab(hour=4, minute=0),
        "kwargs": {"days": 30},
    },
    # ── EKAP veri toplama ──────────────────────────────
    # Güncel ihaleler — GÜNDÜZ 4 kez: 06:30 / 10:30 / 13:30 / 16:30.
    # ⚠️ Saatler backfill penceresinin (19:00–06:00) DIŞINDA seçildi: `ekap` kuyruğu tek
    # concurrency'lidir; backfill koşarken tetiklenen sync_recent kuyrukta bekler. Üretimde
    # tam bu yaşandı — sync_recent her gün biraz daha kaydı (23:00 → 00:36 → 05:29 → 11:47)
    # ve bir gün hiç koşmadı; yeni ilanlar geç girdiği için bildirim eşleşmeleri sıfırlandı.
    # 06:30'daki tur ayrıca bildirimlere veri yetiştirir: 07/08/09/10/11'deki asistan/OKAS/
    # alarm/filtre/idare görevlerinden ÖNCE günün ilanlarını DB'ye alır.
    # ⚠️ `crontab(minute=..., hour=...)` kartezyen çarpımdır: dakikayı liste yapmayın
    # (`minute="30,0"` + `hour="6,10"` → 4 değil 4×2 = 8 çalışma).
    "ekap-sync-recent": {
        "task": "ekap.tasks.sync_recent",
        "schedule": crontab(minute=30, hour="6,10,13,16"),
    },
    # Akıllı detay yenileme — her 3 saatte bir (yalnızca son 1 yıl; EKAP_REFRESH_YEARS)
    "ekap-refresh-stale": {
        "task": "ekap.tasks.refresh_stale",
        "schedule": crontab(minute=30, hour="*/3"),
    },
    # Geçmiş doldurma (backfill) — AKŞAM 19:00'dan sabah 06:00'ya, 15 dk'da bir.
    # ⚠️ Pencere ZORUNLU, keyfi değil: `ekap` kuyruğu tek concurrency'lidir ve backfill uzun
    # sürer. Tüm gün koştuğunda `sync_recent` ve detay görevleri arkasında sıraya giriyor,
    # güncel ilanlar saatlerce (giderek: günlerce) gecikiyordu → "bugün ilan edilen ihale = 0"
    # → filtre/idare/OKAS/asistan bildirimlerinin hepsi birden susuyordu. Gündüz kuyruk
    # güncel veriye ayrılır; arşiv doldurma akşam/gece 11 saatte ilerler.
    # 5 yıl tabanına ulaşınca görev DB kontrolüyle anında döner → boşta maliyeti yok. EKAP
    # yavaş/yanıtsızsa sayfa hatasını zarifçe yutar (kısmi ilerlemeyi kaydeder, sonraki
    # tetikte devam eder). Kilit (1 sa) üst üste binmeyi, throttle (~1 istek/sn) + tek
    # concurrency EKAP'ı korur.
    "ekap-backfill": {
        "task": "ekap.tasks.backfill",
        "schedule": crontab(minute="*/15", hour="19-23,0-5"),
    },
    # OKAS kodları — haftalık (Pazartesi 05:00)
    # Sözleşmeleri firmalara bağlar. EKAP'a gitmez (detail_raw arşivinden çalışır) →
    # `celery` kuyruğuna yönlendirilir (bkz. settings.CELERY_TASK_ROUTES) ve EKAP
    # worker'ını bloklamaz. Redis kilidi üst üste binmeyi önler.
    # ⚠️ Gündüz koruması **saat penceresiyle** yapılır, aralıkla değil: süpürme yalnızca
    # 00:00-07:00'de çalışır (`CONTRACTOR_SWEEP_START/END`), gündüz görev anında
    # "atlandı" dönüp çıkar (bedava). Bu yüzden 5 dk aralık güvenlidir ve gece penceresini
    # doldurur — 10 dk × 90 sn kurgusu gecenin %85'ini boşa harcıyordu.
    # Artımlı mod (süpürme bitince) her saat çalışır ama kısa bütçelidir (90 sn).
    "ekap-sync-contractors": {
        "task": "ekap.tasks.sync_contractors",
        "schedule": crontab(minute="*/5"),
    },
    # "İhalede geçen idare_id" kümesini sıcak tutar (idare_detsis filtresinin kesişimi).
    # ⚠️ İstek yolunda hesaplanınca ~40 sn sürüyordu; 10 dk'da bir tazelenince cache
    # (TTL 30 dk) hiç boşalmaz ve kullanıcı bu maliyeti hiç ödemez. EKAP'a gitmez →
    # `celery` kuyruğu (bkz. settings.CELERY_TASK_ROUTES).
    "ekap-refresh-idare-id-set": {
        "task": "ekap.tasks.refresh_idare_id_set",
        "schedule": crontab(minute="*/10"),
    },
    "ekap-sync-okas": {
        "task": "ekap.tasks.sync_okas",
        "schedule": crontab(hour=5, minute=0, day_of_week=1),
    },
    # DETSIS kurumlar — haftalık (Pazartesi 05:30)
    "ekap-sync-authorities": {
        "task": "ekap.tasks.sync_authorities",
        "schedule": crontab(hour=5, minute=30, day_of_week=1),
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
