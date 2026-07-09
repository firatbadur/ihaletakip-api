"""
Global exception handler.

DRF'in yakaladığı tüm hataları {success:false, message, data:null, errors}
zarfına çevirir. DRF'in yakalayamadığı (beklenmeyen) hatalar da 500 zarfına
dönüştürülür — böylece istemci HER ZAMAN JSON alır, asla ham HTML 500 değil.
"""
import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .response import extract_message

logger = logging.getLogger("ihaletakip")


def custom_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    if response is None:
        # DRF'in tanımadığı beklenmeyen hata → 500 zarfı
        view = context.get("view").__class__.__name__ if context.get("view") else "?"
        logger.exception("Beklenmeyen hata [%s]: %s", view, exc)
        resp = Response(
            {
                "success": False,
                "message": "Beklenmeyen bir sunucu hatası oluştu.",
                "data": None,
                "errors": {"detail": str(exc)},
            },
            status=500,
        )
        resp._enveloped = True
        return resp

    detail = response.data
    response.data = {
        "success": False,
        "message": extract_message(detail),
        "data": None,
        "errors": detail,
    }
    response._enveloped = True
    return response
