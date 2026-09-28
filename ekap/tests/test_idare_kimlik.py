"""
İhale detayında idare kimliği — `ekap.views._attach_idare_kimlik`.

⚠️⚠️ **Mobil iki ayrı alana bakıyor** (`TenderDetail/components/IhaleBilgileriTab.js`):

    İdare adına dokunma (tüm ihaleleri listele) → `data.idareId || idare.id`
    "İdare Raporu" düğmesi                     → `detsisNo || idareId`

Mobil kaynaklı ihalelerin ham payload'ında `idareId` **hiç yok** (mobil API vermiyor;
değer `Tender.idare_id` kolonuna ad/ata-yolu eşleştirmesiyle yazılıyor). Kolondan
yanıta taşınmazsa idare adına dokunmak `if (!idareId) return` ile **sessizce**
hiçbir şey yapmaz — üretimde yaşandı (2026-09-28): `detsis_no` yazılmıştı, rapor
düğmesi çalışıyordu, dokunma ölüydü.
"""
from django.test import TestCase

from ekap.models import Authority, Tender
from ekap.utils import normalize_tr
from ekap.views import _attach_idare_kimlik

# Mobil detayının gerçek şekli: `idareId` YOK, yalnızca adlar var.
MOBIL_PAYLOAD = {
    "ikn": "2026/1789437",
    "idareAdi": "TEKİRDAĞ SU VE KANALİZASYON İDARESİ GENEL MÜDÜRLÜĞÜ "
                "TİCARET İŞLERİ DAİRESİ BAŞKANLIĞI İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ",
    "idare": {"il": {"id": 316, "adi": "TEKİRDAĞ"}},
    "ihaleBilgi": {"ikn": "2026/1789437"},
}


class IdareKimligiTest(TestCase):
    def setUp(self):
        ad = "İHALE İŞLERİ ŞUBE MÜDÜRLÜĞÜ"
        Authority.objects.create(detsis_no="18148693", parent_detsis="63229239", ad=ad,
                                 ad_norm=normalize_tr(ad), idare_id="91646")
        self.tender = Tender.objects.create(
            ikn="2026/1789437", ekap_id="mobil:2026-1789437",
            ihale_adi="Sürekli İzleme Merkezi", idare_adi=MOBIL_PAYLOAD["idareAdi"],
            idare_id="91646", idare_kaynak="yol", detay_kaynak="mobil",
        )

    def _coz(self):
        import copy
        return _attach_idare_kimlik(copy.deepcopy(MOBIL_PAYLOAD), self.tender)

    def test_idareId_kolondan_yazilir(self):
        """⚠️ Mobilin 'idare adına dokunma' eylemi YALNIZCA bunu okur."""
        self.assertEqual(self._coz()["idareId"], "91646")

    def test_detsis_no_da_yazilir(self):
        self.assertEqual(self._coz()["idare"]["detsis_no"], "18148693")

    def test_detsis_no_zaten_doluysa_idareId_YINE_yazilir(self):
        """⚠️ Regresyon: eski kod `detsis_no` doluysa erken çıkıp `idareId`yi atlıyordu."""
        import copy
        p = copy.deepcopy(MOBIL_PAYLOAD)
        p["idare"]["detsis_no"] = "18148693"
        self.assertEqual(_attach_idare_kimlik(p, self.tender)["idareId"], "91646")

    def test_idare_blogu_yoksa_bile_idareId_yazilir(self):
        """Bazı mobil detaylarında `idare` bloğu boş/eksik gelebiliyor."""
        p = {"ikn": "2026/1789437"}
        self.assertEqual(_attach_idare_kimlik(p, self.tender)["idareId"], "91646")

    def test_ham_payloaddaki_id_kolonu_ezmez(self):
        """v2 kayıtlarında ham `idareId` gerçektir — öncelikli kalmalı."""
        import copy
        p = copy.deepcopy(MOBIL_PAYLOAD)
        p["idareId"] = "12345"
        self.assertEqual(_attach_idare_kimlik(p, self.tender)["idareId"], "12345")

    def test_idare_id_bilinmiyorsa_alan_UYDURULMAZ(self):
        """⚠️ Eşleşmeyen ihalelerde (belirsiz ad) alan yazılmamalı."""
        import copy
        bos = Tender.objects.create(ikn="2026/999", ekap_id="mobil:2026-999",
                                    ihale_adi="X", idare_adi="Y", idare_id="",
                                    detay_kaynak="mobil")
        p = _attach_idare_kimlik(copy.deepcopy(MOBIL_PAYLOAD), bos)
        self.assertNotIn("idareId", p)
