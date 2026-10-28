"""
Deneme bitiş hatırlatması (`subscription.tasks.send_trial_reminders`).

Mobil paywall deneme çizelgesinde "deneme bitmeden bildirimle hatırlatırız" VAADİ
veriyor. Bu dosya o vaadin tutulduğunu kilitler:
  - pencere sınırları (bitişe 24–52 saat),
  - tek seferlik (ikinci tur göndermez),
  - iptal etmiş / denemede olmayan / Free kullanıcıya gitmez,
  - push gidemezse (sessiz saat vb.) işaretlenmez → sonraki tur yeniden dener.
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import PushDevice, User
from subscription.tasks import send_trial_reminders
from tenders.services import push


# Sessiz saat aralığı kapalı (start == end) → testin saatinden bağımsız.
@override_settings(NOTIF_QUIET_START_HOUR=0, NOTIF_QUIET_END_HOUR=0, NOTIF_MIN_GAP_MINUTES=0)
class DenemeHatirlatmaTests(TestCase):
    def setUp(self):
        cache.clear()
        self._n = 0

    def _kullanici(self, *, saat_kala=40, donem="TRIAL", iptal=False, tier=User.Tier.PRO):
        self._n += 1
        u = User.objects.create(
            email=f"d{self._n}@test.local",
            username=f"d{self._n}-test",
            subscription_tier=tier,
            subscription_period_type=donem,
            subscription_expires_at=timezone.now() + timedelta(hours=saat_kala),
            subscription_cancelled_at=timezone.now() if iptal else None,
        )
        PushDevice.objects.create(user=u, token=f"tok-{self._n}")
        return u

    def _calistir(self, durum=push.SENT):
        with patch.object(push, "send_fcm", return_value=durum) as gonder:
            send_trial_reminders()
        return gonder

    def test_penceredeki_deneme_kullanicisina_tek_push(self):
        u = self._kullanici(saat_kala=40)
        gonder = self._calistir()
        self.assertEqual(gonder.call_count, 1)
        _, _, _, data = gonder.call_args.args
        self.assertEqual(data["subscription"], "trial_reminder")
        u.refresh_from_db()
        self.assertIsNotNone(u.trial_reminder_sent_at)

        cache.clear()  # idem anahtarı olmasa bile alan tekrar gönderimi engellemeli
        self.assertEqual(self._calistir().call_count, 0)

    def test_pencere_disi_gonderilmez(self):
        self._kullanici(saat_kala=60)   # çok erken
        self._kullanici(saat_kala=10)   # çok geç
        self.assertEqual(self._calistir().call_count, 0)

    def test_iptal_etmis_denemede_olmayan_ve_free_atlanir(self):
        self._kullanici(iptal=True)
        self._kullanici(donem="NORMAL")
        self._kullanici(tier=User.Tier.FREE)
        self.assertEqual(self._calistir().call_count, 0)

    def test_push_gidemezse_isaretlenmez(self):
        u = self._kullanici()
        self._calistir(durum=push.ERROR)
        u.refresh_from_db()
        self.assertIsNone(u.trial_reminder_sent_at)
