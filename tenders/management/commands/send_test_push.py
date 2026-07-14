"""
Bir kullanıcıya test push bildirimi gönderir (FCM uçtan uca doğrulama).

Kullanım:
    python manage.py send_test_push <user_id>
    python manage.py send_test_push <user_id> --title "Başlık" --body "Gövde"
    python manage.py send_test_push <user_id> --raw   # pacing kapılarını atla, doğrudan FCM

Kayıtlı `fcm_token` yoksa ya da `FCM_CREDENTIALS` tanımsızsa uyarı basar. Ölü token
tespit edilirse (pacing'li modda) kullanıcının `fcm_token`'ı temizlenir.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Bir kullanıcıya test push bildirimi gönderir."

    def add_arguments(self, parser):
        parser.add_argument("user_id", type=int, help="Kullanıcı id'si.")
        parser.add_argument("--title", default="Test Bildirimi", help="Push başlığı.")
        parser.add_argument(
            "--body", default="İhaleTakip push bildirimleri çalışıyor. 🎉",
            help="Push gövdesi.",
        )
        parser.add_argument(
            "--raw", action="store_true",
            help="Pacing kapılarını (sessiz saat/limit/idem) atla, doğrudan FCM gönder.",
        )

    def handle(self, *args, **options):
        from tenders.services import push as push_mod

        User = get_user_model()
        try:
            user = User.objects.get(pk=options["user_id"])
        except User.DoesNotExist:
            raise CommandError(f"Kullanıcı bulunamadı: id={options['user_id']}")

        if not push_mod.is_enabled():
            self.stdout.write(self.style.WARNING(
                "FCM devre dışı (FCM_CREDENTIALS tanımsız veya dosya yok). "
                "Push gönderilemez; uygulama-içi bildirim yine yazılabilir."
            ))
            return

        token = (user.fcm_token or "").strip()
        if not token:
            self.stdout.write(self.style.WARNING(
                f"Kullanıcının kayıtlı fcm_token'ı yok (id={user.pk})."
            ))
            return

        title = options["title"]
        body = options["body"]
        data = {"type": "info"}

        if options["raw"]:
            status = push_mod.send_fcm(token, title, body, data)
            if status == push_mod.SENT:
                self.stdout.write(self.style.SUCCESS("Push gönderildi (raw)."))
            else:
                self.stdout.write(self.style.ERROR(f"Push gönderilemedi: {status}"))
            return

        from tenders.services import notify

        ok = notify.push_to_user(user, title=title, body=body, data=data)
        if ok:
            self.stdout.write(self.style.SUCCESS("Push gönderildi."))
        else:
            self.stdout.write(self.style.WARNING(
                "Push atılmadı — pacing kapısı (sessiz saat/limit/aralık), tercih kapalı "
                "veya token geçersiz olabilir. Doğrudan denemek için --raw kullan."
            ))
