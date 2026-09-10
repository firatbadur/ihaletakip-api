"""EKAP yardımcı araçları (HTTP'den bağımsız, her yerden çağrılabilir)."""
from ekap.tools.ocr import (
    PSM_SAYFA,
    PSM_TEK_KELIME,
    PSM_TEK_SATIR,
    OCRHatasi,
    resimden_metin_cikar,
)

__all__ = [
    "resimden_metin_cikar",
    "OCRHatasi",
    "PSM_SAYFA",
    "PSM_TEK_SATIR",
    "PSM_TEK_KELIME",
]
