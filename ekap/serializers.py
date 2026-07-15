"""
ekap serializer'ları — çıktı EKAP alan isimleriyle birebir tutulur ki mobildeki
mevcut mapper'lar (TenderList/helpers.js) neredeyse değişmeden çalışsın.
"""
from rest_framework import serializers

from .models import Announcement, Authority, City, OkasCode, Tender


class EkapTenderListSerializer(serializers.Serializer):
    """Arama listesi item'ı — EKAP `list` şeklinde."""

    def to_representation(self, t: Tender):
        return {
            "id": t.ekap_id,
            "ikn": t.ikn,
            "ihaleAdi": t.ihale_adi,
            "idareAdi": t.idare_adi,
            "ihaleIlAdi": t.ihale_il_adi,
            "ihaleTarihSaat": t.ihale_tarih_saat,
            "ihaleTip": str(t.ihale_tip) if t.ihale_tip is not None else None,
            "ihaleTipAciklama": t.ihale_tipi_aciklama,
            "ihaleUsulAciklama": t.ihale_usul_aciklama,
            "ihaleDurum": str(t.ihale_durum) if t.ihale_durum is not None else None,
            "ihaleDurumAciklama": t.ihale_durum_aciklama,
            "dokumanSayisi": t.dokuman_sayisi,
            "ilanVarMi": t.ilan_var_mi,
        }


class EkapAnnouncementSerializer(serializers.Serializer):
    def to_representation(self, a: Announcement):
        return {
            "id": a.ekap_ilan_id,
            "ilanTip": str(a.ilan_tip) if a.ilan_tip is not None else None,
            "ilanTarihi": a.ilan_tarihi.isoformat() if a.ilan_tarihi else None,
            "baslik": a.baslik,
            "veriHtml": a.veri_html,
            "istekliAdi": a.istekli_adi,
        }


class OkasCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = OkasCode
        fields = ["kod", "adi", "adi_eng"]


class AuthorityNodeSerializer(serializers.Serializer):
    """
    DETSIS ağaç düğümü. `detsis_no` ağaç anahtarı; `idare_id` ihale filtre anahtarı
    (dal düğümünde null); `has_items` çocuğu var mı (mobilde expand chevron'u).
    `path` yalnızca aramada dolar — kök→ebeveyn ata adları (breadcrumb).
    """

    def to_representation(self, a: Authority):
        paths = self.context.get("paths") or {}
        return {
            "detsis_no": a.detsis_no,
            "idare_id": a.idare_id or None,
            "ad": a.ad,
            "parent_detsis": a.parent_detsis or None,
            "has_items": a.has_items,
            "seviye": a.seviye,
            "path": paths.get(a.detsis_no, []),
        }


class CitySerializer(serializers.ModelSerializer):
    class Meta:
        model = City
        fields = ["ekap_il_id", "plaka", "ad", "is_big_city"]
