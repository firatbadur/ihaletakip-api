"""
iyzico REST istemcisi — Checkout Form (tek çekim, 3D Secure) + iade.

Resmî `iyzipay` paketi bilinçli olarak KULLANILMADI: üç uç için bağımlılık eklemek
yerine küçük bir istemci (imza ~10 satır). `requests` kullanılır (iyzico'da TLS
parmak izi engeli yok — EKAP kuralı burada geçerli değil).

**İmza = IYZWSv2** (HMAC-SHA256):
    imza   = hex(HMAC(secret, randomKey + uri_yolu + json_gövde))
    başlık = "IYZWSv2 " + base64("apiKey:<k>&randomKey:<r>&signature:<imza>")
⚠️ İstek belgelerine göre iyzico iki farklı biçim anlatıyor; bu biçim v2'dir.
**Sandbox'ta doğrulanmalı** — yanlışsa iyzico her isteğe `errorCode=1000/1001`
("Geçersiz imza") döner, sessiz başarısızlık olmaz.

⚠️ Güven, iyzico'ya **bizim** attığımız TLS sorgusundadır (`checkout_detail`): web'in
ilettiği token tek başına hiçbir şey kanıtlamaz. Yanıttaki `signature` alanı ek bir
kontroldür; biçimi sandbox'ta doğrulanana kadar uyuşmazlık yalnızca LOGLANIR
(`IYZICO_YANIT_IMZA_ZORUNLU=False`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger("ihaletakip")

_TIMEOUT = 20
YOL_INIT = "/payment/iyzipos/checkoutform/initialize/auth/ecom"
YOL_DETAY = "/payment/iyzipos/checkoutform/auth/ecom/detail"
YOL_IADE = "/payment/refund"


class IyzicoError(Exception):
    """Ağ hatası / 5xx / anahtar yok — iyzico'nun bir karar vermediği durum."""


def fiyat(deger) -> str:
    """iyzico fiyat biçimi: nokta ondalık, 2 hane (`"1200.00"`)."""
    return f"{Decimal(str(deger)).quantize(Decimal('0.01'))}"


def yetkilendirme(api_key: str, secret: str, yol: str, govde: str, rnd: str) -> str:
    imza = hmac.new(
        secret.encode(), (rnd + yol + govde).encode(), hashlib.sha256
    ).hexdigest()
    ham = f"apiKey:{api_key}&randomKey:{rnd}&signature:{imza}"
    return "IYZWSv2 " + base64.b64encode(ham.encode()).decode()


def _post(yol: str, govde: dict) -> dict:
    import requests

    api_key = settings.IYZICO_API_KEY
    secret = settings.IYZICO_SECRET_KEY
    if not api_key or not secret:
        raise IyzicoError("IYZICO_API_KEY / IYZICO_SECRET_KEY tanımlı değil.")
    # ⚠️ İmzalanan gövde ile gönderilen gövde BİREBİR aynı bayt dizisi olmalı.
    metin = json.dumps(govde, ensure_ascii=False, separators=(",", ":"))
    rnd = f"{int(time.time() * 1000)}{secrets.token_hex(4)}"
    try:
        yanit = requests.post(
            settings.IYZICO_BASE_URL.rstrip("/") + yol,
            data=metin.encode(),
            headers={
                "Authorization": yetkilendirme(api_key, secret, yol, metin, rnd),
                "x-iyzi-rnd": rnd,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise IyzicoError(f"iyzico isteği başarısız: {e}") from e
    if yanit.status_code >= 500:
        raise IyzicoError(f"iyzico HTTP {yanit.status_code}")
    try:
        veri = yanit.json()
    except ValueError as e:
        raise IyzicoError(f"iyzico yanıtı JSON değil (HTTP {yanit.status_code})") from e
    if veri.get("status") != "success":
        # ⚠️ Gövdeyi loglamıyoruz: alıcı kimlik numarası/adresi içerebilir (KVKK).
        logger.info("iyzico %s: status=%s kod=%s", yol, veri.get("status"), veri.get("errorCode"))
    return veri


def yanit_imzasi_gecerli(veri: dict) -> bool | None:
    """Checkout detay yanıtının `signature`'ı. Alan yoksa None (karar verilemez).

    Sıra iyzico belgesinden: paymentStatus, paymentId, currency, basketId,
    conversationId, paidPrice, price, token — `:` ile birleştirilir. Fiyatlar sondaki
    sıfırlar atılmış biçimde imzalanır (`1200.0` → `1200`).
    """
    imza = veri.get("signature")
    if not imza:
        return None

    def f(x):
        if x is None:
            return ""
        d = Decimal(str(x)).normalize()
        return format(d, "f")

    parcalar = [
        veri.get("paymentStatus") or "", veri.get("paymentId") or "",
        veri.get("currency") or "", veri.get("basketId") or "",
        veri.get("conversationId") or "", f(veri.get("paidPrice")),
        f(veri.get("price")), veri.get("token") or "",
    ]
    beklenen = hmac.new(
        settings.IYZICO_SECRET_KEY.encode(), ":".join(parcalar).encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(beklenen, imza)


def checkout_baslat(govde: dict) -> dict:
    return _post(YOL_INIT, govde)


def checkout_detay(token: str, conversation_id: str = "") -> dict:
    govde = {"locale": "tr", "token": token}
    if conversation_id:
        govde["conversationId"] = conversation_id
    return _post(YOL_DETAY, govde)


def iade(transaction_id: str, tutar, para_birimi: str, conversation_id: str, ip: str) -> dict:
    return _post(YOL_IADE, {
        "locale": "tr",
        "conversationId": conversation_id,
        "paymentTransactionId": transaction_id,
        "price": fiyat(tutar),
        "currency": para_birimi,
        "ip": ip or "127.0.0.1",
    })
