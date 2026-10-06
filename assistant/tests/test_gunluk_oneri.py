"""
Günlük İhale Asistanı önerisi — 09:00, yalnız Pro, adaylar = 08:00 filtre bildirimi.

⚠️ Kontrol (2026-10-06) iki hata buldu: kelime parçası eşleşmesi ("köprü" →
"Uzunköprü", "Kron Köprü ve Protez") ve "8 yeni öneri" deyip 5 kart göstermek.
Kullanıcı kararıyla aday kümesi o sabahki filtre bildiriminin kümesine bağlandı ve
seçimi yapay zekâ yapıyor; kod, modelin döndürdüğü id'leri aday kümesiyle kesiştirir.
"""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from assistant.models import ChatMessage, CompanyProfile, TenderRecommendation
from assistant.services import matching, oneri_secim
from assistant.tasks import match_recommendations
from ekap.models import Tender
from tenders.models import Notification, SavedFilter


def _ihale(i, *, ad=None, sektor="yol_altyapi", kayit_saat_once=4):
    t = Tender.objects.create(
        ikn=f"2026/{i}", ekap_id=f"e{i}", ihale_adi=ad or f"İş {i}", sektor=sektor,
        ihale_durum=2, ihale_tarihi=timezone.now() + timedelta(days=20),
    )
    Tender.objects.filter(pk=t.pk).update(
        created_at=timezone.now() - timedelta(hours=kayit_saat_once))
    return t


def _ai(secimler):
    for x in secimler:
        x.setdefault("uygun", True)
    return {"analysis": json.dumps({"secimler": secimler}), "usage": {}}


class GunlukOneriTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="pro", email="pro@x.com", password="x",
            subscription_tier=User.Tier.PRO)
        self.profil = CompanyProfile.objects.create(
            user=self.user, company_name="Asfaltçı",
            profile_map={"keywords": ["asfalt"], "avoid": ["diş protez"]})
        self.filtre = SavedFilter.objects.create(
            user=self.user, name="Yol", alarm=True, filters={"sektor": ["yol_altyapi"]})
        self.simdi = timezone.now()
        self.pencere = (self.simdi - timedelta(hours=24), self.simdi)

    def _filtre_bildirimi(self):
        Notification.objects.create(
            user=self.user, type=Notification.Type.TENDER, title="Size Uygun İhaleler",
            filtre_idler=str(self.filtre.pk),
            pencere_bas=self.pencere[0], pencere_bit=self.pencere[1])

    def _calistir(self, secimler=None, hata=None):
        yan = {"side_effect": hata} if hata else {"return_value": _ai(secimler or [])}
        with patch("ai.services.claude.get_api_key", return_value="k"), \
             patch("ai.services.claude.call_claude", **yan), \
             patch("tenders.services.notify.push_to_user", return_value=True):
            return match_recommendations()

    # ── kapsam ──────────────────────────────────────────
    def test_filtre_bildirimi_YOKSA_oneri_ve_bildirim_yok(self):
        _ihale(1, ad="Asfalt yapım işi")
        r = self._calistir([{"id": 1, "gerekce": "x"}])
        self.assertEqual(r["filtre_yok"], 1)
        self.assertFalse(TenderRecommendation.objects.exists())

    def test_adaylar_YALNIZ_filtre_kumesi_ve_penceresinden(self):
        icerde = _ihale(1, ad="Asfalt yapım işi")
        _ihale(2, ad="Asfalt alımı", sektor="gida_catering")        # filtre dışı
        _ihale(3, ad="Asfalt serimi", kayit_saat_once=48)          # pencere dışı
        aday = oneri_secim.adaylar(self.user, {self.filtre.pk}, self.pencere)
        self.assertEqual([t.pk for t in aday], [icerde.pk])

    def test_free_kullanici_atlanir(self):
        User.objects.filter(pk=self.user.pk).update(subscription_tier=User.Tier.FREE)
        self._filtre_bildirimi()
        _ihale(1)
        self.assertEqual(self._calistir([{"id": 1}])["skipped_free"], 1)

    # ── AI çıktısına kod garantisi ─────────────────────
    def test_UYDURMA_id_atilir_ve_5_te_kesilir(self):
        self._filtre_bildirimi()
        tl = [_ihale(i) for i in range(1, 8)]
        secim = [{"id": 99999, "gerekce": "uydurma"}] + [
            {"id": t.pk, "gerekce": f"g{t.pk}"} for t in tl]
        self._calistir(secim)
        oneriler = TenderRecommendation.objects.filter(user=self.user)
        self.assertEqual(oneriler.count(), 5)
        self.assertNotIn(99999, set(oneriler.values_list("tender_id", flat=True)))

    def test_bildirim_sayisi_KART_sayisina_esit(self):
        self._filtre_bildirimi()
        tl = [_ihale(i) for i in range(1, 5)]
        self._calistir([{"id": t.pk, "gerekce": "uygun"} for t in tl])
        bildirim = Notification.objects.filter(type=Notification.Type.CHAT).get()
        kartlar = ChatMessage.objects.get().payload["tender_cards"]
        self.assertEqual(bildirim.title, f"İhale Asistanı: {len(kartlar)} öneri")
        self.assertEqual(len(kartlar), 4)
        self.assertEqual(kartlar[0]["gerekce"], "uygun")

    def test_UYGUN_DEGIL_dedigi_secim_atilir(self):
        """Üretimde görüldü: model seçip gerekçeye 'uygun değildir' yazdı."""
        self._filtre_bildirimi()
        a, b = _ihale(1), _ihale(2)
        self._calistir([{"id": a.pk, "uygun": False, "gerekce": "uygun değildir"},
                        {"id": b.pk, "gerekce": "uygun"}])
        self.assertEqual(
            list(TenderRecommendation.objects.values_list("tender_id", flat=True)), [b.pk])

    def test_AI_bos_secerse_bildirim_yok(self):
        self._filtre_bildirimi()
        _ihale(1)
        r = self._calistir([])
        self.assertEqual(r["secim_yok"], 1)
        self.assertFalse(Notification.objects.filter(type=Notification.Type.CHAT).exists())

    def test_AI_hatasinda_kural_yedegi_yine_aday_kumeden(self):
        self._filtre_bildirimi()
        uygun = _ihale(1, ad="Asfalt yapım işi")
        _ihale(2, ad="Kırtasiye alımı")
        _ihale(3, ad="Asfalt alımı", sektor="gida_catering")        # filtre dışı
        r = self._calistir(hata=RuntimeError("API yok"))
        self.assertEqual(r["yedek_kural"], 1)
        self.assertEqual(
            list(TenderRecommendation.objects.values_list("tender_id", flat=True)),
            [uygun.pk])

    def test_onceden_onerilen_tekrar_aday_olmaz(self):
        t = _ihale(1)
        TenderRecommendation.objects.create(user=self.user, tender=t, score=1,
                                            date=timezone.localdate())
        aday = oneri_secim.adaylar(self.user, {self.filtre.pk}, self.pencere,
                                   haric_ihale_idleri=[t.pk])
        self.assertEqual(aday, [])


class KelimeSiniriTest(TestCase):
    def _profil(self, **pm):
        return SimpleNamespace(profile_map=pm, cities=[], tender_types=[])

    def _t(self, ad):
        return SimpleNamespace(ihale_adi=ad, ihale_adi_norm="", il_id=None, ihale_tip=None,
                               ihale_il_adi="", okas_kalemleri=SimpleNamespace(all=lambda: []))

    def test_KELIME_PARCASI_eslesmez(self):
        b = matching.profil_baglami(self._profil(keywords=["köprü"]))
        self.assertEqual(matching.puanla(self._t("Uzunköprü İlçesi Taşınmaz"), b)[0], 0)

    def test_ekli_hali_eslesir(self):
        b = matching.profil_baglami(self._profil(keywords=["köprü"]))
        self.assertEqual(matching.puanla(self._t("Dere köprüsü yapımı"), b)[0], 3.0)

    def test_avoid_gecen_ihale_DISLANIR(self):
        b = matching.profil_baglami(self._profil(keywords=["köprü"], avoid=["protez"]))
        self.assertIsNone(matching.puanla(self._t("Kron Köprü ve Protez Hizmeti"), b))
