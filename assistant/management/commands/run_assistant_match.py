"""
İhale Asistanı günlük eşleştirmesini elle çalıştırır (celery beat beklemeden).

Kullanım:
    python manage.py run_assistant_match           # son 1 gün (beat ile aynı)
    python manage.py run_assistant_match --days 7  # son 7 gün (geniş pencere)

Beat henüz çalışmadıysa veya profiller yeni güncellendiyse önerileri hemen üretmek
için kullanılır. Her aktif profil için TenderRecommendation + bildirim + digest üretir.
"""
from django.core.management.base import BaseCommand

from assistant.tasks import match_recommendations


class Command(BaseCommand):
    help = "İhale Asistanı öneri eşleştirmesini elle çalıştırır."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=1,
            help="Son kaç günde ilan edilen ihaleler taransın (varsayılan 1).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        self.stdout.write(f"Eşleştirme çalışıyor (son {days} gün)…")
        result = match_recommendations(since_days=days)
        self.stdout.write(
            self.style.SUCCESS(
                f"Tamamlandı: {result.get('profiles', 0)} profil işlendi, "
                f"{result.get('recommendations', 0)} yeni öneri üretildi."
            )
        )
