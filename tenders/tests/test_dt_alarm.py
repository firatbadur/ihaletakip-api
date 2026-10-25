"""
Doğrudan temin kayıtlı filtresi + Pro alarmı.

Ürün kuralı (2026-10-25): DT filtresi KAYDETMEK Free+Pro, ALARM yalnızca Pro.
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from ekap.mobil import constants as C
from ekap.models import DogrudanTemin
from tenders.models import DtKayitliFiltre, Notification
from tenders.serializers import NotificationSerializer
from tenders.tasks import check_dt_alarms

URL = "/api/v1/dogrudan-temin/kayitli-filtreler/"
SESSIZ_YOK = dict(NOTIF_QUIET_START_HOUR=0, NOTIF_QUIET_END_HOUR=0)


def _dt(dtn, *, saat_once=1, ilan_gun_once=0, **kw):
    simdi = timezone.now()
    alan = dict(ad=f"{dtn} mazot alımı", ad_norm=f"{dtn.lower()} mazot alimi",
                idare_adi="OKUL", idare_adi_norm="okul", durum=C.DT_DURUM_ACIK, tur=1,
                il_id=265, son_teklif=simdi + timedelta(hours=3),
                ilan_tarihi=timezone.localdate() - timedelta(days=ilan_gun_once),
                son_gorulme=simdi)
    alan.update(kw)
    d = DogrudanTemin.objects.create(dtn=dtn, **alan)
    DogrudanTemin.objects.filter(pk=d.pk).update(created_at=simdi - timedelta(hours=saat_once))
    return d


class _Taban(TestCase):
    def setUp(self):
        cache.clear()
        self.free = User.objects.create_user(username="free", email="f@x.com", password="x")
        self.pro = User.objects.create_user(username="pro", email="p@x.com", password="x",
                                            subscription_tier="pro")
        self.assertFalse(self.free.is_premium)
        self.assertTrue(self.pro.is_premium)


class FiltreUcuTest(_Taban):
    def _c(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def test_FREE_kaydedebilir_alarmsiz(self):
        r = self._c(self.free).post(URL, {"ad": "Mazot", "filters": {"q": "mazot"}},
                                    format="json")
        self.assertEqual(r.status_code, 201)

    def test_FREE_alarm_acamaz_403(self):
        r = self._c(self.free).post(URL, {"ad": "Mazot", "filters": {}, "alarm": True},
                                    format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["errors"]["code"], "premium_required")
        self.assertEqual(DtKayitliFiltre.objects.count(), 0)

    def test_FREE_sonradan_alarm_acamaz_ama_ad_degistirebilir(self):
        f = DtKayitliFiltre.objects.create(user=self.free, ad="eski", alarm=True)
        c = self._c(self.free)
        self.assertEqual(c.patch(f"{URL}{f.pk}/", {"alarm": True}, format="json").status_code, 403)
        self.assertEqual(c.patch(f"{URL}{f.pk}/", {"ad": "yeni"}, format="json").status_code, 200)

    def test_PRO_alarm_acar(self):
        r = self._c(self.pro).post(URL, {"ad": "Mazot", "filters": {"il_id": [265]},
                                         "alarm": True}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_TANINMAYAN_anahtar_reddedilir(self):
        r = self._c(self.pro).post(URL, {"ad": "x", "filters": {"il": [265]}}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_baskasinin_filtresi_gorunmez(self):
        f = DtKayitliFiltre.objects.create(user=self.pro, ad="pro")
        self.assertEqual(self._c(self.free).get(f"{URL}{f.pk}/").status_code, 404)

    def test_liste_ucu_kayitli_filtreyi_uygular_ve_anonim_400(self):
        f = DtKayitliFiltre.objects.create(user=self.pro, ad="pro", filters={"tur": [3]})
        _dt("26DT1", tur=1)
        _dt("26DT2", tur=3)
        r = self._c(self.pro).get("/api/v1/ekap/dogrudan-temin/", {"kayitli_filtre": f.pk})
        self.assertEqual([x["dtn"] for x in r.json()["data"]["list"]], ["26DT2"])
        r = APIClient().get("/api/v1/ekap/dogrudan-temin/", {"kayitli_filtre": f.pk})
        self.assertEqual(r.status_code, 400)


@override_settings(**SESSIZ_YOK, NOTIF_DT_MIN_ARALIK_DK=15, NOTIF_DT_TAZELIK_SAAT=6)
class AlarmGoreviTest(_Taban):
    def setUp(self):
        super().setUp()
        self.f = DtKayitliFiltre.objects.create(user=self.pro, ad="Mazot",
                                                filters={"q": "mazot"}, alarm=True)

    def test_kullanici_basina_TEK_bildirim_ve_sayi_LISTE_ile_ayni(self):
        _dt("26DT1", son_teklif=timezone.now() + timedelta(hours=5))
        _dt("26DT2", son_teklif=timezone.now() + timedelta(hours=2))
        # İkinci filtre aynı alımlara uyuyor: tekilleştirilip bir kez sayılmalı.
        DtKayitliFiltre.objects.create(user=self.pro, ad="İl", filters={"il_id": [265]},
                                       alarm=True)
        self.assertEqual(check_dt_alarms()["bildirim"], 1)
        n = Notification.objects.get()
        self.assertEqual(n.dtn_liste, "26DT2,26DT1")    # en acil önce
        self.assertIn("2 doğrudan temin", n.body)
        r = APIClient().get("/api/v1/ekap/dogrudan-temin/", {"dtn": n.dtn_liste})
        self.assertEqual(r.json()["data"]["totalCount"], 2)
        self.assertIn("dtn_liste", NotificationSerializer(n).data)

    def test_ayni_alim_IKINCI_kez_bildirilmez(self):
        _dt("26DT1")
        check_dt_alarms()
        cache.delete(f"dt:sonbildirim:{self.pro.pk}")
        check_dt_alarms()
        self.assertEqual(Notification.objects.count(), 1)

    def test_FREE_ve_alarmsiz_filtre_bildirim_almaz(self):
        DtKayitliFiltre.objects.create(user=self.free, ad="x", filters={}, alarm=True)
        DtKayitliFiltre.objects.filter(pk=self.f.pk).update(alarm=False)
        _dt("26DT1")
        check_dt_alarms()
        self.assertEqual(Notification.objects.count(), 0)

    def test_eslesmeyen_alim_bildirilmez(self):
        _dt("26DT1", ad="Ekmek", ad_norm="ekmek", idare_adi_norm="okul")
        check_dt_alarms()
        self.assertEqual(Notification.objects.count(), 0)

    def test_SESSIZ_SAATTE_isaretlenmez_sonra_bildirilir(self):
        _dt("26DT1")
        with patch("tenders.services.notify._in_quiet_hours", return_value=True):
            self.assertEqual(check_dt_alarms(), {"atlandi": "sessiz_saat"})
        check_dt_alarms()
        self.assertEqual(Notification.objects.get().dtn_liste, "26DT1")

    def test_ARALIK_dolmadan_gelen_alim_KAYBOLMAZ_devreder(self):
        _dt("26DT1")
        check_dt_alarms()
        _dt("26DT2")
        check_dt_alarms()                     # 15 dk dolmadı → bildirim yok, işaret yok
        self.assertEqual(Notification.objects.count(), 1)
        cache.set(f"dt:sonbildirim:{self.pro.pk}", timezone.now() - timedelta(minutes=16))
        check_dt_alarms()
        self.assertEqual(Notification.objects.order_by("-pk").first().dtn_liste, "26DT2")

    def test_ILK_DOLUM_FIRTINASI_yok_eski_ilan_ve_bayat_kayit_bildirilmez(self):
        _dt("26DT1", ilan_gun_once=3)          # tam taramanın şimdi yazdığı Cuma ilanı
        _dt("26DT2", saat_once=7)              # tazelik penceresinin dışında
        check_dt_alarms()
        self.assertEqual(Notification.objects.count(), 0)

    def test_kapanmis_alim_bildirilmez(self):
        _dt("26DT1", son_teklif=timezone.now() - timedelta(minutes=1))
        check_dt_alarms()
        self.assertEqual(Notification.objects.count(), 0)
