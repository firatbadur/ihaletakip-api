"""
EKAP v2 istek imzalama — AES-192-CBC.

Mobil istemcideki `src/api/v1/calls.js:10-42` (CryptoJS) mantığının Python
birebir karşılığı. EKAP v2 her isteği bu 4 header ile doğrular:

    X-Custom-Request-Guid : düz metin v4 GUID
    X-Custom-Request-R8id : AES-CBC(GUID) ciphertext, Base64
    X-Custom-Request-Siv  : IV, Base64
    X-Custom-Request-Ts   : AES-CBC(unix_ms) ciphertext, Base64

Anahtar CryptoJS'te `enc.Utf8.parse(...)` ile WordArray olarak veriliyor →
salt/KDF yok, ham AES-192 anahtarı. R8id/Ts sadece ciphertext'tir (IV öneki yok),
CryptoJS `.ciphertext.toString(Base64)` ile aynı.
"""
import base64
import os
import uuid

from django.conf import settings
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _key_bytes() -> bytes:
    key = settings.EKAP_SIGNING_KEY.encode("utf-8")
    # 24 byte = AES-192. Anahtar uzunluğu değişirse net hata ver.
    if len(key) not in (16, 24, 32):
        raise ValueError(
            f"EKAP_SIGNING_KEY {len(key)} byte; 16/24/32 olmalı (varsayılan 24)."
        )
    return key


def _aes_cbc_b64(plaintext: str, iv: bytes) -> str:
    """PKCS7 padding + AES-CBC şifreleme, ciphertext'i Base64 döndürür."""
    padder = sym_padding.PKCS7(algorithms.AES.block_size).padder()
    data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_key_bytes()), modes.CBC(iv)).encryptor()
    ct = encryptor.update(data) + encryptor.finalize()
    return base64.b64encode(ct).decode("ascii")


def generate_signing_headers(now_ms: int | None = None) -> dict:
    """EKAP v2 için 4 imza header'ını üretir."""
    import time

    guid = str(uuid.uuid4())
    iv = os.urandom(16)
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))

    return {
        "X-Custom-Request-Guid": guid,
        "X-Custom-Request-R8id": _aes_cbc_b64(guid, iv),
        "X-Custom-Request-Siv": base64.b64encode(iv).decode("ascii"),
        "X-Custom-Request-Ts": _aes_cbc_b64(ts, iv),
    }


def decrypt_cbc_b64(ciphertext_b64: str, iv_b64: str) -> str:
    """Test/doğrulama için — R8id/Ts'i çözüp düz metni döndürür."""
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ciphertext_b64)
    decryptor = Cipher(algorithms.AES(_key_bytes()), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(algorithms.AES.block_size).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
