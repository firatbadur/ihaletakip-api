"""
Bildirim görevlerini elle çalıştırır (celery beat beklemeden).

Kullanım:
    python manage.py run_notifications --job alarms   # ihale alarmları (günü/doküman/sonuç)
    python manage.py run_notifications --job filters   # kayıtlı filtre eşleşmeleri
    python manage.py run_notifications --job all       # ikisi de

Beat çalışmıyorken ya da test amaçlı bildirim üretmek için kullanılır.
"""
from django.core.management.base import BaseCommand

from tenders.tasks import check_saved_filter_matches, check_tender_alarms


class Command(BaseCommand):
    help = "Bildirim görevlerini (alarmlar / kayıtlı filtreler) elle çalıştırır."

    def add_arguments(self, parser):
        parser.add_argument(
            "--job",
            choices=["alarms", "filters", "all"],
            default="all",
            help="Çalıştırılacak görev (varsayılan: all).",
        )

    def handle(self, *args, **options):
        job = options["job"]

        if job in ("alarms", "all"):
            self.stdout.write("İhale alarmları kontrol ediliyor…")
            res = check_tender_alarms()
            self.stdout.write(self.style.SUCCESS(
                f"  alarmlar: {res.get('alarms', 0)} alarm, "
                f"{res.get('users_notified', 0)} kullanıcı, {res.get('pushed', 0)} push"
            ))

        if job in ("filters", "all"):
            self.stdout.write("Kayıtlı filtre eşleşmeleri kontrol ediliyor…")
            res = check_saved_filter_matches()
            self.stdout.write(self.style.SUCCESS(
                f"  filtreler: {res.get('filters', 0)} filtre, "
                f"{res.get('users_notified', 0)} kullanıcı, {res.get('pushed', 0)} push"
            ))
