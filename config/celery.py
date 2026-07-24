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
    # Güncel ihaleler — her gece 02:00
    "ekap-sync-recent": {
        "task": "ekap.tasks.sync_recent",
        "schedule": crontab(hour=2, minute=0),
    },
    # Akıllı detay yenileme — her 3 saatte bir (yalnızca son 1 yıl; EKAP_REFRESH_YEARS)
    "ekap-refresh-stale": {
        "task": "ekap.tasks.refresh_stale",
        "schedule": crontab(minute=30, hour="*/3"),
    },
    # Geçmiş doldurma (backfill) — TÜM GÜN, 15 dk'da bir. 5 yıl tabanına ulaşınca
    # görev DB kontrolüyle anında döner → boşta maliyeti yok. EKAP gün içinde
    # yavaş/yanıtsız olabildiğinden görev sayfa hatasını zarifçe yutar (kısmi
    # ilerlemeyi kaydeder, sonraki tetikte kaldığı yerden devam eder). Kilit (1 sa)
    # üst üste binmeyi, throttle (~1 istek/sn) + tek concurrency EKAP'ı korur.
    "ekap-backfill": {
        "task": "ekap.tasks.backfill",
        "schedule": crontab(minute="*/15"),
    },
    # OKAS kodları — haftalık (Pazartesi 05:00)
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
