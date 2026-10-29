"""
Destek yazışması: talep → destek yanıtı → bildirim/push → kullanıcı yanıtı.

⚠️ Arıza (2026-10-29): admin `reply` alanına yazıyordu ama hiçbir bildirim
tetiklenmiyor, mobil yanıtı hiç göstermiyordu → kullanıcı yanıtı ÖĞRENEMİYORDU.
Bu dosya, yanıtın kullanıcıya ulaştığı her halkayı kilitler.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from accounts.models import PushDevice
from core import support
from core.models import SupportMessage, SupportTicket
from tenders.models import Notification
from tenders.services import notify, push

User = get_user_model()
URL = "/api/v1/support/"
IZOLE_CACHE = override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)


def _veri(resp):
    return resp.json()["data"]


@IZOLE_CACHE
class DestekAkisiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.pro = User.objects.create_user(
            username="pro", email="pro@test.local", password="x",
            subscription_tier=User.Tier.PRO,
        )
        self.free = User.objects.create_user(username="free", email="free@test.local", password="x")
        self.yonetici = User.objects.create_superuser(
            username="admin", email="admin@test.local", password="x"
        )

    def _ac(self, user=None, **ek):
        self.client.force_login(user or self.pro)
        govde = {"message": "Kayıtlı aramamın bildirimi gelmiyor.", **ek}
        return self.client.post(URL, govde, content_type="application/json")

    # ── Açma ───────────────────────────────────────────────────────────────
    def test_talep_acmak_ilk_mesaji_yazar(self):
        r = self._ac(category="bildirim", meta={"platform": "ios", "x": "atilir"})
        self.assertEqual(r.status_code, 201)
        d = _veri(r)
        self.assertEqual(d["status"], "Bekliyor")
        self.assertEqual(d["category"], "bildirim")
        self.assertEqual([m["sender"] for m in d["messages"]], ["user"])
        t = SupportTicket.objects.get(pk=d["id"])
        self.assertEqual(t.email, "pro@test.local")
        self.assertEqual(t.meta, {"platform": "ios"})  # izinli olmayan anahtar düşer
        self.assertIsNotNone(t.last_message_at)

    def test_eski_istemci_sozlesmesi_calisir(self):
        r = self._ac(email="baska@test.local", phone="05551234567")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(SupportTicket.objects.get().category, "diger")

    def test_free_talep_acamaz(self):
        r = self._ac(user=self.free)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(SupportTicket.objects.exists())

    # ── Destek yanıtı → kullanıcıya ulaşır ──────────────────────────────────
    def test_destek_yaniti_bildirim_ve_push_uretir(self):
        t = support.talep_ac(self.pro, message="Bildirim gelmiyor, yardım?")
        PushDevice.objects.create(user=self.pro, token="tok-1")
        with patch.object(push, "send_fcm", return_value=push.SENT) as gonder, \
                self.captureOnCommitCallbacks(execute=True):
            support.destek_yaniti(t, "Ayarlar > Bildirimler'i açın.", author=self.yonetici)

        t.refresh_from_db()
        self.assertEqual(t.status, "Yanıtlandı")
        self.assertTrue(t.user_unread)
        n = Notification.objects.get(user=self.pro)
        self.assertEqual((n.type, n.support_ticket_id), ("support", t.pk))
        gonder.assert_called_once()
        data = gonder.call_args.args[3]
        self.assertEqual(data["type"], "support")
        self.assertEqual(data["supportTicketId"], t.pk)

    def test_destek_push_pacing_e_takilmaz(self):
        """Sessiz saat + minimum aralık destek yanıtını YUTMAZ (kuyruk yok, kaybolurdu)."""
        PushDevice.objects.create(user=self.pro, token="tok-1")
        t = support.talep_ac(self.pro, message="Bildirim gelmiyor, yardım?")
        m = SupportMessage.objects.create(ticket=t, sender="staff", body="Yanıt")
        with patch.object(notify, "_in_quiet_hours", return_value=True), \
                patch.object(push, "send_fcm", return_value=push.SENT):
            self.assertFalse(notify.push_to_user(self.pro, title="ihale özeti"))
            self.assertTrue(support.kullaniciya_bildir(m))

    def test_kullanici_listesinde_bildirim_alani_gelir(self):
        t = support.talep_ac(self.pro, message="Bildirim gelmiyor, yardım?")
        with self.captureOnCommitCallbacks(execute=True):
            support.destek_yaniti(t, "Yanıt", author=self.yonetici)
        self.client.force_login(self.pro)
        satir = _veri(self.client.get("/api/v1/notifications/"))[0]
        self.assertEqual(satir["support_ticket_id"], t.pk)

    # ── Okuma + kullanıcı yanıtı ────────────────────────────────────────────
    def test_detay_okundu_sayar_ve_sayac_duser(self):
        t = support.talep_ac(self.pro, message="Bildirim gelmiyor, yardım?")
        support.destek_yaniti(t, "Yanıt", author=self.yonetici)
        self.client.force_login(self.pro)
        self.assertEqual(_veri(self.client.get(f"{URL}unread-count/"))["unread"], 1)
        self.assertTrue(_veri(self.client.get(URL))[0]["unread"])
        d = _veri(self.client.get(f"{URL}{t.pk}/"))
        self.assertEqual([m["sender"] for m in d["messages"]], ["user", "staff"])
        self.assertEqual(_veri(self.client.get(f"{URL}unread-count/"))["unread"], 0)

    def test_kullanici_yaniti_bekliyora_dondurur_kapaliyi_acar(self):
        t = support.talep_ac(self.pro, message="Bildirim gelmiyor, yardım?")
        support.destek_yaniti(t, "Çözüldü mü?", author=self.yonetici, status="Kapandı")
        self.client.force_login(self.pro)
        r = self.client.post(f"{URL}{t.pk}/messages/", {"body": "Hayır, hâlâ gelmiyor"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201)
        d = _veri(r)
        self.assertEqual(d["status"], "Bekliyor")
        self.assertEqual(d["messages"][-1]["sender"], "user")

    def test_suresi_biten_pro_yazismayi_surdurebilir(self):
        t = support.talep_ac(self.free, message="Pro iken açtığım talep")
        self.client.force_login(self.free)
        r = self.client.post(f"{URL}{t.pk}/messages/", {"body": "Devam"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201)

    def test_baskasinin_talebi_404(self):
        t = support.talep_ac(self.free, message="Benim talebim burada")
        self.client.force_login(self.pro)
        self.assertEqual(self.client.get(f"{URL}{t.pk}/").status_code, 404)
        r = self.client.post(f"{URL}{t.pk}/messages/", {"body": "x"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_kapat(self):
        t = support.talep_ac(self.pro, message="Bildirim gelmiyor, yardım?")
        self.client.force_login(self.pro)
        self.assertEqual(_veri(self.client.post(f"{URL}{t.pk}/close/"))["status"], "Kapandı")

    # ── Admin ────────────────────────────────────────────────────────────────
    def test_admin_yanit_kutusu_mesaj_ekler_ve_bildirir(self):
        t = support.talep_ac(self.pro, message="<script>x</script> yardım lütfen")
        self.client.force_login(self.yonetici)
        sayfa = self.client.get(f"/admin/core/supportticket/{t.pk}/change/")
        self.assertEqual(sayfa.status_code, 200)
        self.assertNotContains(sayfa, "<script>x</script>")  # gövde kaçırılır
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(
                f"/admin/core/supportticket/{t.pk}/change/",
                {"status": "Bekliyor", "yeni_yanit": "Merhaba, inceliyoruz."},
            )
        self.assertEqual(r.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.status, "Yanıtlandı")
        son = t.messages.last()
        self.assertEqual((son.sender, son.author), ("staff", self.yonetici))
        self.assertTrue(Notification.objects.filter(support_ticket_id=t.pk).exists())

    def test_admin_yanitsiz_kaydet_bildirim_atmaz(self):
        t = support.talep_ac(self.pro, message="Bildirim gelmiyor, yardım?")
        self.client.force_login(self.yonetici)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"/admin/core/supportticket/{t.pk}/change/",
                             {"status": "İşlemde", "yeni_yanit": ""})
        t.refresh_from_db()
        self.assertEqual(t.status, "İşlemde")
        self.assertFalse(Notification.objects.exists())
