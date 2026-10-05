"""
Bir kullanıcıya test push bildirimi gönderir (FCM uçtan uca doğrulama).

Kullanım:
    python manage.py send_test_push <user_id>
    python manage.py send_test_push <user_id> --title "Başlık" --body "Gövde"
    python manage.py send_test_push <user_id> --raw   # pacing kapılarını atla, doğrudan FCM

Kayıtlı cihaz yoksa ya da `FCM_CREDENTIALS` tanımsızsa uyarı basar. Ölü token
tespit edilirse (pacing'li modda) o cihazın `PushDevice` kaydı silinir.
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

        # ⚠️ Kullanıcının birden çok cihazı olabilir (accounts.PushDevice, 0008);
        # bu komut tek atış olduğu için EN SON kaydolan cihaza gönderir.
        cihaz = user.push_devices.order_by("-last_registered_at").first()
        if cihaz is None:
            self.stdout.write(self.style.WARNING(
                f"Kullanıcının kayıtlı push cihazı yok (id={user.pk})."
            ))
            return
        token = cihaz.token.strip()
        toplam = user.push_devices.count()
        if toplam > 1:
            self.stdout.write(f"  (kullanıcının {toplam} cihazı var; en sonuncusuna gönderiliyor)")

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
