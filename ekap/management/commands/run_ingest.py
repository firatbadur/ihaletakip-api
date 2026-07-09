"""
Toplama görevlerini Celery olmadan (senkron) manuel tetikler.

Kullanım:
    python manage.py run_ingest --task recent --pages 1
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
        parser.add_argument("--ekap-id", type=str)

    def handle(self, *args, **o):
        task = o["task"]
        # defer_detail=False → detaylar da senkron çekilsin (Celery gerekmez)
        if task == "recent":
            res = tasks.sync_recent(max_pages=o["pages"], defer_detail=False)
        elif task == "backfill":
            res = tasks.backfill(max_pages=o["pages"], defer_detail=False)
        elif task == "okas":
            res = tasks.sync_okas()
        elif task == "authorities":
            res = tasks.sync_authorities()
        elif task == "refresh":
            res = tasks.refresh_stale(defer_detail=False)
        elif task == "detail":
            if not o.get("ekap_id"):
                raise CommandError("--ekap-id gerekli")
            from ekap.client import EkapV2Client
            from ekap import sync
            sync.sync_detail(o["ekap_id"], EkapV2Client())
            res = {"detail": o["ekap_id"]}
        self.stdout.write(self.style.SUCCESS(f"✅ {task} tamamlandı: {res}"))
