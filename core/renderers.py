"""
Global yanıt zarfı renderer'ı.

Her başarılı yanıtı {success, message, data} yapısına sarar.
- View zaten `api_response()` ile sardıysa (resp._enveloped) dokunmaz.
- Exception handler'ın ürettiği hata gövdesi (success içeren) dokunulmaz.
- 4xx/5xx düz gövdeler hata zarfına çevrilir.
"""
from rest_framework.renderers import JSONRenderer

from .response import extract_message


class EnvelopeJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        renderer_context = renderer_context or {}
        response = renderer_context.get("response")
        status_code = getattr(response, "status_code", 200)

        already = getattr(response, "_enveloped", False) or (
            isinstance(data, dict) and "success" in data and "data" in data
        )

        if already:
            payload = data
        elif status_code >= 400:
            payload = {
                "success": False,
                "message": extract_message(data),
                "data": None,
                "errors": data,
            }
        else:
            payload = {"success": True, "message": "", "data": data}

        return super().render(payload, accepted_media_type, renderer_context)
