"""
ekap serializer'ları — çıktı EKAP alan isimleriyle birebir tutulur ki mobildeki
mevcut mapper'lar (TenderList/helpers.js) neredeyse değişmeden çalışsın.
"""
from rest_framework import serializers

from .models import Announcement, Authority, City, Contract, Contractor, OkasCode, Tender


def _dec(value):
    """Decimal → string (JSON'da hassasiyet kaybı olmasın), None → None."""
    return str(value) if value is not None else None


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


# ── Yüklenici (firma) ──────────────────────────────────
# ⚠️ Alan adları **snake_case Türkçe**, EKAP camelCase DEĞİL. camelCase kuralı yalnızca
# EKAP'ın kendi JSON'unu yansıtan payload'lar için vardır (mobil mapper'ları korumak
# üzere). EKAP'ta yüklenici nesnesi YOKTUR → yansıtılacak şekil de yok. Bizim
# tanımladığımız varlıklar zaten snake_case (AuthorityNodeSerializer, CitySerializer).

class ContractorListSerializer(serializers.Serializer):
    """Firma listesi item'ı — sıralama anahtarları denormalize sütunlardan gelir."""

    def to_representation(self, c: Contractor):
        return {
            "id": c.pk,
            "ad": c.kanonik_ad,
            "kind": c.kind,
            "kind_aciklama": c.get_kind_display(),
            "il_adi": c.il_adi or None,
            "sozlesme_sayisi": c.sozlesme_sayisi,
            # ⚠️ ihale_sayisi ≠ sozlesme_sayisi: kısımlı ihalede bir firma birden çok
            # kısım alabilir (3 sözleşme / 1 ihale).
            "ihale_sayisi": c.ihale_sayisi,
            "toplam_sozlesme_bedeli": _dec(c.toplam_sozlesme_bedeli),
            "son_sozlesme_tarihi": (
                c.son_sozlesme_tarihi.isoformat() if c.son_sozlesme_tarihi else None
            ),
            "ortalama_indirim_orani": _dec(c.ortalama_indirim_orani),
            # Yaklaşık maliyet kapsamı kısmi → ortalama tek başına yanıltıcı olmasın
            "indirim_orani_ornek_sayisi": c.indirim_orani_ornek_sayisi,
            "uye_sayisi": c.uye_sayisi,
            "ortak_girisim_sayisi": c.ortak_girisim_sayisi,
        }


class ContractSerializer(serializers.Serializer):
    """
    Sözleşme (yüklenicinin kazandığı iş).

    ⚠️ Kısım tutarları **bilerek yoktur** — EKAP onları bozuk ölçekte üretiyor
    (bkz. `ekap.models.ContractSection` docstring).
    """

    def to_representation(self, c: Contract):
        tender = c.tender
        bedel, en_dusuk = c.sozlesme_bedeli_num, c.en_dusuk_teklif_num
        return {
            "id": c.pk,
            "ekap_sozlesme_id": c.ekap_sozlesme_id,
            "sozlesme_tarihi": c.sozlesme_tarihi.isoformat() if c.sozlesme_tarihi else None,
            "sozlesme_bedeli": _dec(bedel),
            "en_dusuk_teklif": _dec(en_dusuk),
            "en_yuksek_teklif": _dec(c.en_yuksek_teklif_num),
            # Yalnızca Sonuç İlanı'ndan gelir; yoksa null + kaynak "" → istemci
            # "veri yok" gösterebilsin (sessiz boşluk olmasın).
            "yaklasik_maliyet": _dec(c.yaklasik_maliyet_num),
            "yaklasik_maliyet_kaynak": c.yaklasik_maliyet_kaynak or None,
            "ihale_yaklasik_maliyet": _dec(c.tender_yaklasik_maliyet_num),
            "indirim_orani": _dec(c.indirim_orani),
            "rekabet_araligi": self._rekabet_araligi(c),
            "teklif_sayisi": c.teklif_sayisi,
            "gecerli_teklif_sayisi": c.gecerli_teklif_sayisi,
            "dokuman_indiren_sayisi": c.dokuman_indiren_sayisi,
            # False → en düşük teklifi veren elenmiş demektir (anomali bayrağı)
            "en_dusuk_teklifi_veren_mi": (
                bool(bedel == en_dusuk) if (bedel is not None and en_dusuk is not None) else None
            ),
            "fesih": c.fesih_string or None,
            "tasfiye_transfer": c.tasfiye_transfer_string or None,
            "ihale": {
                "ekap_id": tender.ekap_id,
                "ikn": tender.ikn,
                "ihale_adi": tender.ihale_adi,
                "idare_adi": tender.idare_adi,
                "idare_id": tender.idare_id or None,
                "ihale_il_adi": tender.ihale_il_adi,
                "ihale_tip": tender.ihale_tip,
                "ihale_tipi_aciklama": tender.ihale_tipi_aciklama,
                "ihale_tarihi": tender.ihale_tarihi.isoformat() if tender.ihale_tarihi else None,
            },
            "kisimlar": [
                {"ekap_kisim_id": k.ekap_kisim_id, "kisim_adi": k.kisim_adi}
                for k in c.kisimlar.all()
            ],
        }

    @staticmethod
    def _rekabet_araligi(c: Contract):
        """(en yüksek − en düşük) / en düşük — teklif sahasının ne kadar geniş olduğu."""
        lo, hi = c.en_dusuk_teklif_num, c.en_yuksek_teklif_num
        if lo is None or hi is None or lo <= 0:
            return None
        return str(round((hi - lo) / lo, 4))


class TenderContractSerializer(ContractSerializer):
    """İhale detayındaki sözleşme — yüklenici nesnesi gömülü gelir."""

    def to_representation(self, c: Contract):
        data = super().to_representation(c)
        y = c.yuklenici
        data["yuklenici_ham_ad"] = c.yuklenici_adi
        # null olabilir (henüz çözülmemiş satır) — mobil tolere etmeli
        data["yuklenici"] = None if y is None else {
            "id": y.pk,
            "ad": y.kanonik_ad,
            "kind": y.kind,
            "kind_aciklama": y.get_kind_display(),
            "sozlesme_sayisi": y.sozlesme_sayisi,
            # ⚠️ Burada `.select_related("uye")` ÇAĞIRMAYIN. View
            # `prefetch_related("yuklenici__uyelikler__uye")` ile üyeleri zaten getiriyor;
            # queryset'i değiştiren her çağrı (select_related dahil) prefetch cache'ini
            # ıskalar ve sözleşme başına taze sorgu atar (ölçüm: 18 satır → 21 sorgu).
            # Düz `.all()` cache'i kullanır; `m.uye` de prefetch zincirinden gelir.
            "uyeler": [
                {"id": m.uye.pk, "ad": m.uye.kanonik_ad, "pilot": m.pilot}
                for m in y.uyelikler.all()
            ],
        }
        return data
