"""
KİK kurul kararı alarmı — mevcut ihale alarmına seçenek (`TenderAlarm.kurul_karar`).

Ürün kararı (2026-10-25): varsayılan KAPALI, mevcut alarmlarda da kapalı; Pro'ya özel
(alarm kurmak zaten Pro).
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from ekap.models import KurulKarari, Tender
from tenders.models import Notification, TenderAlarm
from tenders.tasks import check_kurul_karar_alarms

SESSIZ_YOK = dict(NOTIF_QUIET_START_HOUR=0, NOTIF_QUIET_END_HOUR=0)


def _karar(no="2026/UH.II-2688", ikn="2026/1519213", *, yayin_gun_once=3, **kw):
    alan = dict(ikn=ikn, karar_aciklama="İtirazen şikayet başvurusunun reddine",
                ihale_adi="PERSONEL HİZMETİ ALIMI", idare_adi="ANAMUR SULAMA BİRLİĞİ",
                toplanti_tarihi=timezone.now() - timedelta(days=yayin_gun_once + 1),
                yayin_tarihi=timezone.now() - timedelta(days=yayin_gun_once))
    alan.update(kw)
    return KurulKarari.objects.create(karar_no=no, **alan)


@override_settings(**SESSIZ_YOK)
class KurulAlarmTest(TestCase):
    def setUp(self):
        cache.clear()
        self.pro = User.objects.create_user(username="pro", email="p@x.com", password="x",
                                            subscription_tier="pro")
        self.free = User.objects.create_user(username="free", email="f@x.com", password="x")

    def _alarm(self, user=None, **kw):
        alan = dict(tender_id="E1", tender_ikn="2026/1519213", tender_title="Personel",
                    kurul_karar=True)
        alan.update(kw)
        return TenderAlarm.objects.create(user=user or self.pro, **alan)

    def test_yeni_karar_ihale_detayina_baglanan_TEK_bildirim(self):
        self._alarm()
        _karar()
        self.assertEqual(check_kurul_karar_alarms()["bildirim"], 1)
        n = Notification.objects.get()
        self.assertEqual((n.type, n.tender_id, n.karar_no), ("alarm", "E1", "2026/UH.II-2688"))
        self.assertIn("reddine", n.body)
        self.assertEqual(n.title, "Personel")

    def test_KALICI_dedup_redis_sifirlansa_da_tekrar_yok(self):
        self._alarm()
        _karar()
        check_kurul_karar_alarms()
        cache.clear()
        check_kurul_karar_alarms()
        self.assertEqual(Notification.objects.count(), 1)

    def test_ikinci_karar_ayrica_bildirilir(self):
        self._alarm()
        _karar()
        check_kurul_karar_alarms()
        _karar(no="2026/UH.II-2700")
        check_kurul_karar_alarms()
        self.assertEqual(Notification.objects.count(), 2)

    def test_secenek_KAPALIYSA_bildirim_yok(self):
        self._alarm(kurul_karar=False)
        _karar()
        check_kurul_karar_alarms()
        self.assertEqual(Notification.objects.count(), 0)

    def test_FREE_bildirim_almaz(self):
        self._alarm(user=self.free)
        _karar()
        check_kurul_karar_alarms()
        self.assertEqual(Notification.objects.count(), 0)

    def test_ILK_DOLUM_eski_yayinli_karar_bildirilmez(self):
        self._alarm()
        _karar(yayin_gun_once=40)            # bugün ilk kez gördük ama 40 gün önce yayımlanmış
        check_kurul_karar_alarms()
        self.assertEqual(Notification.objects.count(), 0)

    def test_ikn_tasimayan_alarm_ekap_id_ile_cozulur(self):
        Tender.objects.create(ikn="2026/1519213", ekap_id="E1")
        self._alarm(tender_ikn=None)
        _karar()
        check_kurul_karar_alarms()
        self.assertEqual(Notification.objects.get().tender_ikn, "2026/1519213")

    def test_SESSIZ_SAATTE_isaretlenmez(self):
        a = self._alarm()
        _karar()
        with patch("tenders.services.notify._in_quiet_hours", return_value=True):
            check_kurul_karar_alarms()
        a.refresh_from_db()
        self.assertEqual(a.kurul_bildirilen, "")
        check_kurul_karar_alarms()
        self.assertEqual(Notification.objects.count(), 1)


class AlarmUcuTest(TestCase):
    def setUp(self):
        self.pro = User.objects.create_user(username="pro", email="p@x.com", password="x",
                                            subscription_tier="pro")
        self.c = APIClient()
        self.c.force_authenticate(self.pro)

    def test_varsayilan_KAPALI_ve_acilabilir(self):
        r = self.c.post("/api/v1/alarms/", {"tender_id": "E1", "tender_ikn": "2026/1"},
                        format="json")
        self.assertEqual(r.status_code, 201)
        self.assertFalse(self.c.get("/api/v1/alarms/E1/").json()["data"]["kurul_karar"])
        self.c.post("/api/v1/alarms/", {"tender_id": "E1", "kurul_karar": True}, format="json")
        self.assertTrue(TenderAlarm.objects.get().kurul_karar)
        self.assertTrue(self.c.get("/api/v1/alarms/E1/").json()["data"]["kurul_karar"])
