"""
Takip edilen firma — tekil durum ucu `alarm`'ı da döndürür (2026-10-29).

Yalnız `is_favorite` dönerken mobil firma detayı açık alarmı hep "Kapalı" gösteriyordu.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from ekap.models import Contractor
from tenders.models import FavoriteContractor


class FavoriFirmaDurumTest(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="u", email="u@x.com", password="x")
        self.c = APIClient()
        self.c.force_authenticate(self.u)
        self.f = Contractor.objects.create(kanonik_ad="ÖRNEK İNŞAAT", kind="firma")

    def _durum(self):
        r = self.c.get(f"/api/v1/favorite-contractors/{self.f.pk}/")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        return j.get("data", j)

    def test_takipte_degil(self):
        self.assertEqual(self._durum(), {"is_favorite": False, "alarm": False})

    def test_alarm_durumu_doner(self):
        fav = FavoriteContractor.objects.create(user=self.u, contractor=self.f, alarm=True)
        self.assertEqual(self._durum(), {"is_favorite": True, "alarm": True})
        fav.alarm = False
        fav.save(update_fields=["alarm"])
        self.assertEqual(self._durum(), {"is_favorite": True, "alarm": False})
