"""
`teklif_verilebilir` — "açık ihale" ölçütü.

⚠️⚠️ Neden ayrı bir filtre (mobil ekip ölçtü, 2026-09-22): `ihale_durum` tek başına
"teklif verilebilir mi" sorusunu **yanıtlamıyor**. EKAP bir ihalenin durumunu Sonuç
İlanı yayımlanana kadar "Katılıma Açık"ta tutuyor → durumu 2/3 olan 14.228 ihalenin
**%86'sının teklif süresi dolmuştu**. Alt tab bu yüzden çoğunlukla teklif verilemeyen
ihaleler gösteriyordu.

Ölçüt `ihale_tarihi` (teklif son anı) + iptal dışlaması. Testler üç tuzağı kilitler:
NULL tarih (bilinmiyor) gizlenmemeli, NULL durum `exclude` yüzünden düşmemeli, iptal
edilmiş gelecek tarihli ihale açık sayılmamalı.
"""

from datetime import timedelta

from django.http import QueryDict
from django.test import TestCase
from django.utils import timezone

from ekap.models import Tender
from ekap.views import apply_tender_filters


def _t(ikn, *, saat_farki=None, durum=2):
    return Tender.objects.create(
        ikn=ikn, ekap_id=f"e{ikn[-1]}", ihale_adi=f"İş {ikn}",
        ihale_durum=durum,
        ihale_tarihi=(timezone.now() + timedelta(hours=saat_farki))
        if saat_farki is not None else None,
    )


class TeklifVerilebilirTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.gelecek = _t("2026/1", saat_farki=48)
        cls.gecmis = _t("2026/2", saat_farki=-48)
        cls.iptal_gelecek = _t("2026/3", saat_farki=48, durum=6)
        cls.iptal10_gelecek = _t("2026/4", saat_farki=48, durum=10)
        cls.durumsuz_gelecek = _t("2026/5", saat_farki=48, durum=None)
        cls.tarihsiz = _t("2026/6", saat_farki=None)
        # ⚠️ EKAP'ın asıl tuzağı: süresi dolmuş ama durumu hâlâ "Katılıma Açık".
        cls.gecmis_ama_acik_durumlu = _t("2026/7", saat_farki=-2, durum=2)

    def _iknler(self, deger):
        qs = apply_tender_filters(
            Tender.objects.all(), QueryDict(f"teklif_verilebilir={deger}")
        )
        return set(qs.values_list("ikn", flat=True))

    def test_true_yalnizca_suresi_gecmemis_ve_iptal_olmayanlar(self):
        self.assertEqual(
            self._iknler("true"),
            {"2026/1", "2026/5", "2026/6"},
        )

    def test_suresi_dolmus_ama_durumu_acik_olan_ELENIR(self):
        """⚠️ Asıl arıza buydu: durumu 2 olduğu için açık sanılıyordu."""
        self.assertNotIn("2026/7", self._iknler("true"))

    def test_iptal_edilmis_gelecek_tarihli_ELENIR(self):
        """⚠️ Üretimde 4 böyle kayıt var; "açık" göstermek boşa teklif hazırlatır."""
        acik = self._iknler("true")
        self.assertNotIn("2026/3", acik)
        self.assertNotIn("2026/4", acik)

    def test_durumu_BILINMEYEN_gelecek_tarihli_DUSMEZ(self):
        """
        Durumu bilinmeyen ihale iptal sayılmaz → listede kalmalı.

        ⚠️ **SÖZLEŞME testi**: bugün geçmesi Django'nun negasyonu NULL-güvenli
        derlemesinden gelir (`NOT (x IN (…) AND x IS NOT NULL)`) — yani kodda ek bir
        koruma olmadığı için değil, ORM garanti ettiği için. Test o garantiyi
        kilitler: negasyon elle SQL'e çevrilirse ya da ORM davranışı değişirse,
        durumu silinmiş ihaleler listeden sessizce düşerdi (bugün düzeltilen silme
        arızası tekrarlarsa tam olarak bu olur).
        """
        self.assertIn("2026/5", self._iknler("true"))

    def test_tarihi_BILINMEYEN_gizlenmez(self):
        """Veri eksikliği yüzünden gerçek bir ihaleyi gizlemek daha kötü."""
        self.assertIn("2026/6", self._iknler("true"))

    def test_false_tersini_dondurur(self):
        acik, kapali = self._iknler("true"), self._iknler("false")
        self.assertEqual(acik & kapali, set())
        self.assertEqual(
            acik | kapali,
            set(Tender.objects.values_list("ikn", flat=True)),
        )

    def test_parametre_yoksa_filtre_uygulanmaz(self):
        qs = apply_tender_filters(Tender.objects.all(), QueryDict(""))
        self.assertEqual(qs.count(), 7)

    def test_pro_kapisi_tetiklemez(self):
        """
        ⚠️ Temel filtredir: Pro listesine girerse ücretsiz kullanıcı 403 alır ve alt
        tab herkes için kapanır.
        ⚠️ `_PRO_SCHEMA_PARAMS` bir `OpenApiParameter` **listesidir** — içinde string
        aramak her zaman geçer (sahte güvence), adlar çıkarılmalı.
        """
        from ekap.views import _PRO_PARAMS, _PRO_SCHEMA_PARAMS

        sema_adlari = {p.name for p in _PRO_SCHEMA_PARAMS}
        self.assertNotIn("teklif_verilebilir", _PRO_PARAMS)
        self.assertNotIn("teklif_verilebilir", sema_adlari)
        # ⚠️ İki kümenin farkı boş olmalı (CLAUDE.md kuralı): biri güncellenip
        # diğeri unutulursa uç ya belgelenmemiş filtre kabul eder ya 403 vermez.
        self.assertEqual(_PRO_PARAMS ^ sema_adlari, set())
