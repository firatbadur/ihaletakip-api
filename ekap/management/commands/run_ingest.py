"""
Toplama görevlerini Celery olmadan (senkron) manuel tetikler.

Kullanım:
    python manage.py run_ingest --task recent --pages 1
    python manage.py run_ingest --task recent --days 7 --pages 40 --defer
    python manage.py run_ingest --task backfill --pages 2
    python manage.py run_ingest --task okas
    python manage.py run_ingest --task authorities
    python manage.py run_ingest --task detail --ekap-id 123456
"""
from django.core.management.base import BaseCommand, CommandError

from ekap import tasks


class Command(BaseCommand):
    help = "EKAP toplama görevlerini senkron çalıştırır (yerel test)."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True,
                            choices=["recent", "backfill", "okas", "authorities", "detail", "refresh"])
        parser.add_argument("--pages", type=int, default=1)
        # ⚠️ `recent` penceresi vars. EKAP_RECENT_DAYS (3 gün). Toplama birkaç gün
        # durduysa (ör. EKAP imza şeması değişikliği) boşluk 3 günden uzundur ve
        # varsayılan pencere onu KAPATMAZ — bu bayrakla geriye doğru genişletilir.
        parser.add_argument("--days", type=int,
                            help="recent: kaç gün geriye bakılsın (vars. EKAP_RECENT_DAYS)")
        # Detayları senkron çekmek 1 istek/sn tavanında yüzlerce kayıtta dakikalar
        # sürer; --defer ile detay `ekap_oncelik` kuyruğuna atılır (worker çeker).
        parser.add_argument("--defer", action="store_true",
                            help="detayları senkron çekme, kuyruğa at (Celery çalışıyorsa)")
        parser.add_argument("--ekap-id", type=str)

    def handle(self, *args, **o):
        task = o["task"]
        # defer_detail=False → detaylar da senkron çekilsin (Celery gerekmez)
        if task == "recent":
            res = tasks.sync_recent(days=o.get("days"), max_pages=o["pages"],
                                    defer_detail=o["defer"])
        elif task == "backfill":
            res = tasks.backfill(max_pages=o["pages"], defer_detail=o["defer"])
        elif task == "okas":
            res = tasks.sync_okas()
        elif task == "authorities":
            res = tasks.sync_authorities()
        elif task == "refresh":
            res = tasks.refresh_stale(defer_detail=o["defer"])
        elif task == "detail":
            if not o.get("ekap_id"):
                raise CommandError("--ekap-id gerekli")
            from ekap.client import EkapV2Client
            from ekap import sync
            sync.sync_detail(o["ekap_id"], EkapV2Client())
            res = {"detail": o["ekap_id"]}
        self.stdout.write(self.style.SUCCESS(f"✅ {task} tamamlandı: {res}"))
