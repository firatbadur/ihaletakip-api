"""
Destek e-postalarını talep açmadan dener (veri kirletmez).

    python manage.py destek_eposta_test                 # ekip örneği → tanımlı alıcılar
    python manage.py destek_eposta_test --kuru          # yalnızca alıcı + konu
    python manage.py destek_eposta_test --ticket 42     # gerçek talebin ekip e-postası
    python manage.py destek_eposta_test --tur yanit --alici ben@ornek.com

Konu `[TEST]` önekiyle gider. Gönderim, üretimdeki gerçek yoldan (`SaglayiciBackend`:
aktif sağlayıcı + engelli adres süzgeci) geçer.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core import destek_eposta
from core.models import SupportMessage, SupportTicket


class Command(BaseCommand):
    help = "Destek ekip / yanıt e-postasını örnek içerikle gönderir (talep açmaz)."

    def add_arguments(self, parser):
        parser.add_argument("--tur", choices=["ekip", "yanit"], default="ekip")
        parser.add_argument("--alici", help="Alıcı (ekip örneğinde vars. tanımlı alıcılar).")
        parser.add_argument("--ticket", type=int, help="Örnek yerine bu talebi kullan.")
        parser.add_argument("--kuru", action="store_true", help="Göndermeden yalnızca yazdır.")

    def _ticket(self, pk):
        if pk:
            t = SupportTicket.objects.select_related("user").filter(pk=pk).first()
            if t is None:
                raise CommandError(f"Talep yok: #{pk}")
            return t
        return SupportTicket(
            pk=0, email="ornek@ihaletakip.com", category="bildirim", status="Bekliyor",
            message="Kayıtlı aramamın bildirimi sabahları gelmiyor, yardımcı olur musunuz?",
            meta={"platform": "ios", "app_version": "2.4.0", "os_version": "26.0"},
        )

    def handle(self, *args, **o):
        ticket = self._ticket(o["ticket"])
        if o["tur"] == "ekip":
            to = [o["alici"]] if o["alici"] else destek_eposta.alicilar()
            if not to:
                raise CommandError(
                    f"Alıcı tanımlı değil: Uygulama Ayarları → "
                    f"`{destek_eposta.AYAR_ANAHTARI}` ya da SUPPORT_NOTIFY_EMAILS.")
            e = destek_eposta.ekip_epostasi(ticket, ticket.message, yeni=True, gecmis=[], to=to)
        else:
            if not o["alici"]:
                raise CommandError("--tur yanit için --alici gerekli.")
            mesaj = SupportMessage(
                ticket=ticket, sender=SupportMessage.SENDER_STAFF, created_at=timezone.now(),
                body="Merhaba, Ayarlar > Bildirimler ekranından filtre bildirimini açık "
                     "tutmanız yeterli. Sorun sürerse bize tekrar yazın.",
            )
            e = destek_eposta.yanit_epostasi(mesaj, to=o["alici"])
        e.subject = f"[TEST] {e.subject}"
        self.stdout.write(f"alıcı: {', '.join(e.to)}\nkonu : {e.subject}")
        if o["kuru"]:
            self.stdout.write("kuru çalıştırma — gönderilmedi")
            return
        n = e.send(fail_silently=False)
        self.stdout.write(self.style.SUCCESS("gönderildi") if n else
                          self.style.WARNING("gönderilmedi (adres engelli olabilir)"))
