"""
Standart API yanıt zarfı: {success, message, data}

Tüm endpoint'ler bu yapıda döner. View'lar düz `Response(data)` dönebilir;
`EnvelopeJSONRenderer` bunu otomatik sarar. Özel mesaj/durum gerektiğinde
`api_response()` yardımcısı kullanılır.
"""
from rest_framework.response import Response


def api_response(data=None, message="", success=True, status=200):
    """Zarfı açıkça oluşturur (renderer bunu tekrar sarmaz)."""
    resp = Response(
        {"success": success, "message": message, "data": data},
        status=status,
    )
    resp._enveloped = True  # renderer'a "zaten sarıldı" işareti
    return resp


def extract_message(data, default="İşlem başarısız."):
    """DRF hata gövdesinden okunabilir tek bir mesaj çıkarır."""
    if isinstance(data, dict):
        if "detail" in data:
            d = data["detail"]
            if isinstance(d, (list, tuple)):
                return "; ".join(str(x) for x in d)
            return str(d)
        for key, val in data.items():
            if isinstance(val, (list, tuple)) and val:
                return f"{key}: {val[0]}"
            return f"{key}: {val}"
    if isinstance(data, (list, tuple)) and data:
        return str(data[0])
    if data:
        return str(data)
    return default
