"""Kişi başı EKAP kotası (`ekap.mobil.kota`) için ortak HTTP yanıtı + güvenlik olayı."""
import logging

from rest_framework.response import Response

logger = logging.getLogger("ihaletakip")


def kota_yaniti(e):
    """
    İndirme uçları için **429** + `errors.code="ekap_quota"` + `X-IT-Hata` başlığı.

    ⚠️ Başlık şart: mobil indirmeleri `RNFS.downloadFile` ile yapıyor ve gövdeyi okuyamıyor,
    yalnızca durum kodu ve başlıkları görüyor (`docs/mobil-guvenlik-sayfa-siniri.md`).
    """
    resp = Response(
        {"success": False, "message": e.kullanici_mesaji, "data": None,
         "errors": {"code": e.kod, "detail": e.kullanici_mesaji}},
        status=429,
    )
    resp._enveloped = True
    resp["X-IT-Hata"] = e.kod
    return resp


def kota_olayi(request, ist, kota, yol):
    """Kotası dolan kimliği güvenlik günlüğüne yazar (dakikada tek satır)."""
    try:
        from core.guvenlik import olay_yaz

        olay_yaz("ekap_kota", ist=ist, request=request,
                 ayrinti={"yer": yol, "seviye": getattr(kota, "seviye", None)})
    except Exception:                                       # noqa: BLE001
        logger.warning("kota olayı yazılamadı", exc_info=True)
