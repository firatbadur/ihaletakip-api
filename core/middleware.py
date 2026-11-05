"""IP kara listesi — `/api/` isteklerini DRF'e girmeden keser (`core.guvenlik`)."""
import json
import logging

from django.http import HttpResponse

logger = logging.getLogger("ihaletakip")


class ErisimEngeliMiddleware:
    """
    Etkin `ErisimEngeli(tur=ip)` içindeki istemciye 403 `ip_blocked` zarfı döner.

    IP: portal anahtarı doğruysa `X-IT-Client-IP` (web kullanıcısının gerçek IP'si), aksi
    hâlde nginx'in yazdığı `X-Real-IP` (Cloudflare'in gerçek IP'si). ⚠️ Web konteynerinin
    kendi adresi (Docker ağ geçidi) özel ağdır ve `guvenlik.ag_coz` onu engellemeyi reddeder.
    ⚠️ Hata isteği düşürmez: kontrol çökerse istek geçer ve loglanır.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/"):
            try:
                yanit = self._kontrol(request)
            except Exception:                               # noqa: BLE001
                logger.warning("IP engel kontrolü hatası (istek geçirildi)", exc_info=True)
                yanit = None
            if yanit is not None:
                return yanit
        return self.get_response(request)

    @staticmethod
    def _kontrol(request):
        from .guvenlik import MESAJ_IP, ip_engeli, isabet_say, olay_yaz
        from .throttling import istemci_ip

        ip = istemci_ip(request)
        engel_id = ip_engeli(ip)
        if engel_id is None:
            return None
        isabet_say(engel_id)
        olay_yaz("engel_isabet", kimlik=f"ip:{ip}", ip=ip, request=request,
                 ayrinti={"engel": engel_id})
        govde = {"success": False, "message": MESAJ_IP, "data": None,
                 "errors": {"code": "ip_blocked", "detail": MESAJ_IP}}
        return HttpResponse(json.dumps(govde, ensure_ascii=False), status=403,
                            content_type="application/json; charset=utf-8")
