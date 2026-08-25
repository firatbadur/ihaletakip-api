"""
EKAP v2 istek imzalama — AES-CBC.

EKAP her isteği bir imza başlığı ailesiyle doğrular. Başlık **adları ve anahtar
rotasyona tabidir** (bkz. `keyfetch.py`); burada sabit olan yalnızca *algoritma*:

    <guid>   : düz metin v4 GUID
    <r8id>   : AES-CBC(GUID) ciphertext, Base64
    <iv>     : IV, Base64
    <ts>     : AES-CBC(unix_ms) ciphertext, Base64
    <method> : AES-CBC("POST") ciphertext            — opsiyonel (2026-08-25'te geldi)
    <path>   : AES-CBC(URL yolu) ciphertext           — opsiyonel (aynı tarih)

Anahtar CryptoJS'te `enc.Utf8.parse(...)` ile WordArray olarak veriliyor →
salt/KDF yok, ham AES anahtarı (24 bayt AES-192 idi, şimdi 32 bayt AES-256).
Ciphertext IV öneki taşımaz; CryptoJS `.ciphertext.toString(Base64)` ile aynıdır.

⚠️ **`method`/`path` boş geçilemez.** EKAP 2026-08-25 rotasyonunda bunları
ekledi; imzalanan yol **istek yolunun birebir aynısı** olmalı (`/b_ihalearama/...`),
aksi hâlde `HTTP 401 "HataKodu: 1200"`.

⚠️ **Rotasyon geçmişi** (hepsi tek haftada, ikisi tek günde):
  • `X-Custom-Request-Guid/R8id/Siv/Ts`, anahtar `Qm2Lt…` (24 bayt) — 21 Ağustos'a dek
  • `X-Correlation-Id/X-Csrf-Token/X-Session-Id/X-Trace-Id`, anahtar `Kj9Px…` (24 bayt)
  • `X-Ekap-Sec-1..6`, anahtar `pfS7X…` (32 bayt) + method/path imzası
Bu yüzden şema artık **koda gömülmez**, çalışma anında keşfedilir.
"""
import base64
import os
import uuid

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import keyfetch


def _key_bytes(key: str) -> bytes:
    raw = key.encode("utf-8")
    if len(raw) not in (16, 24, 32):
        raise ValueError(
            f"EKAP imza anahtarı {len(raw)} bayt; 16/24/32 olmalı."
        )
    return raw


def _aes_cbc_b64(plaintext: str, iv: bytes, key: bytes) -> str:
    """PKCS7 padding + AES-CBC şifreleme, ciphertext'i Base64 döndürür."""
    padder = sym_padding.PKCS7(algorithms.AES.block_size).padder()
    data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = encryptor.update(data) + encryptor.finalize()
    return base64.b64encode(ct).decode("ascii")


def generate_signing_headers(method: str = "POST", path: str = "",
                             now_ms: int | None = None) -> dict:
    """Geçerli şemaya göre imza başlıklarını üretir.

    ``path`` istekle **birebir aynı** URL yolu olmalıdır (sorgu dizesi hariç).
    """
    import time

    scheme = keyfetch.resolve()
    key = _key_bytes(scheme["key"])
    names = scheme["headers"]

    guid = str(uuid.uuid4())
    iv = os.urandom(16)
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))

    headers = {
        names["guid"]: guid,
        names["r8id"]: _aes_cbc_b64(guid, iv, key),
        names["iv"]: base64.b64encode(iv).decode("ascii"),
        names["ts"]: _aes_cbc_b64(ts, iv, key),
    }
    # Şema bu ikisini tanımıyorsa (eski sürüm) gönderilmez — fazladan başlık
    # eklemek de imzayı bozabilir.
    if names.get("method"):
        headers[names["method"]] = _aes_cbc_b64(method.upper(), iv, key)
    if names.get("path") and path:
        headers[names["path"]] = _aes_cbc_b64(path, iv, key)
    return headers


def decrypt_cbc_b64(ciphertext_b64: str, iv_b64: str, key: str | None = None) -> str:
    """Test/doğrulama için — ciphertext'i çözüp düz metni döndürür."""
    key_bytes = _key_bytes(key or keyfetch.resolve()["key"])
    iv = base64.b64decode(iv_b64)
    ct = base64.b64decode(ciphertext_b64)
    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(algorithms.AES.block_size).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
