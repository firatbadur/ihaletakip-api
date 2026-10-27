"""
Amazon SES geri bildirimi — SNS → `POST /eposta/ses-bildirim/`.

SES, kalıcı bounce (adres yok) ve spam şikâyetini SNS topic'ine yayımlar; topic bu uca
HTTPS ile gönderir. ⚠️ SES, bounce oranı ~%5'i ya da şikâyet oranı ~%0,1'i aşan hesabı
ASKIYA ALIR → bu adreslere bülten göndermeye devam etmek tüm e-postayı (doğrulama
kodları dahil) riske atar.

**Güvenlik** — uç kimliksizdir, bu yüzden:
1. `TopicArn` `settings.SES_SNS_TOPIC_ARNS` içinde olmalı (boşsa uç KAPALI);
2. SNS imzası doğrulanır: sertifika yalnızca `https://sns.<bölge>.amazonaws.com/*.pem`
   adresinden indirilir (aksi hâlde saldırgan kendi sertifikasıyla imzalardı),
   SignatureVersion 1 (SHA1) / 2 (SHA256), kanonik metin AWS belgesindeki alan sırasıyla.
Abonelik onayı (`SubscriptionConfirmation`) imza doğrulandıktan SONRA `SubscribeURL`
çağrılarak otomatik yapılır.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("ihaletakip")

_SERTIFIKA_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_ALANLAR = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp",
                                 "Token", "TopicArn", "Type"),
    "UnsubscribeConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp",
                                "Token", "TopicArn", "Type"),
}


class SnsHatasi(Exception):
    pass


def _guvenli_aws_url(url: str) -> bool:
    u = urlparse(url or "")
    return u.scheme == "https" and bool(_SERTIFIKA_HOST.match(u.hostname or ""))


def _sertifika(url: str) -> bytes:
    if not (_guvenli_aws_url(url) and urlparse(url).path.endswith(".pem")):
        raise SnsHatasi("sertifika adresi AWS SNS değil")
    anahtar = f"sns:sert:{url}"
    pem = cache.get(anahtar)
    if pem is None:
        import requests

        r = requests.get(url, timeout=5)
        r.raise_for_status()
        pem = r.content
        cache.set(anahtar, pem, 24 * 3600)
    return pem


def imza_dogrula(mesaj: dict) -> None:
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    alanlar = _ALANLAR.get(mesaj.get("Type"))
    if not alanlar:
        raise SnsHatasi(f"bilinmeyen tür: {mesaj.get('Type')}")
    metin = "".join(f"{k}\n{mesaj[k]}\n" for k in alanlar if k in mesaj)
    surum = str(mesaj.get("SignatureVersion"))
    if surum not in ("1", "2"):
        raise SnsHatasi(f"imza sürümü desteklenmiyor: {surum}")
    ozet = hashes.SHA1() if surum == "1" else hashes.SHA256()
    sert = x509.load_pem_x509_certificate(_sertifika(mesaj.get("SigningCertURL", "")))
    try:
        sert.public_key().verify(base64.b64decode(mesaj.get("Signature", "")),
                                 metin.encode(), padding.PKCS1v15(), ozet)
    except (InvalidSignature, ValueError) as e:
        raise SnsHatasi("imza geçersiz") from e


def _kaydet(email: str, tur: str, alt_tur: str, aciklama: str, ses_id: str) -> None:
    from django.contrib.auth import get_user_model

    from accounts.models import EpostaGeriBildirim

    email = (email or "").strip()
    # SES alıcıyı "Ad <adres>" biçiminde verebilir.
    if "<" in email:
        email = email.split("<", 1)[1].rstrip(">").strip()
    if not email:
        return
    kapat = tur != EpostaGeriBildirim.Tur.GECICI
    kapandi = kapat and get_user_model().objects.filter(
        email__iexact=email, email_notifications=True).update(email_notifications=False) > 0
    EpostaGeriBildirim.objects.create(
        email=email.lower()[:254], tur=tur, alt_tur=alt_tur[:64], aciklama=aciklama[:500],
        ses_mesaj_id=ses_id[:128], bulten_kapatildi=kapandi)
    logger.warning("SES geri bildirimi: %s %s/%s bülten_kapatıldı=%s",
                   email, tur, alt_tur, kapandi)


def isle(govde: bytes) -> str:
    """SNS isteğini doğrulayıp işler; dönüş kısa bir durum metni. Hata: `SnsHatasi`."""
    from accounts.models import EpostaGeriBildirim

    izinli = set(getattr(settings, "SES_SNS_TOPIC_ARNS", None) or [])
    if not izinli:
        raise SnsHatasi("uç kapalı (SES_SNS_TOPIC_ARNS boş)")
    try:
        mesaj = json.loads(govde)
    except ValueError as e:
        raise SnsHatasi("JSON değil") from e
    if mesaj.get("TopicArn") not in izinli:
        raise SnsHatasi(f"izinsiz topic: {mesaj.get('TopicArn')}")
    imza_dogrula(mesaj)

    tur = mesaj["Type"]
    if tur == "SubscriptionConfirmation":
        import requests

        url = mesaj.get("SubscribeURL", "")
        if not _guvenli_aws_url(url):
            raise SnsHatasi("SubscribeURL AWS değil")
        requests.get(url, timeout=5).raise_for_status()
        logger.info("SNS aboneliği onaylandı: %s", mesaj["TopicArn"])
        return "abonelik_onaylandi"
    if tur != "Notification":
        return "yok_sayildi"

    try:
        olay = json.loads(mesaj.get("Message") or "{}")
    except ValueError:
        return "yok_sayildi"
    # Kimlik bildirimi `notificationType`, yapılandırma kümesi olayı `eventType` kullanır.
    olay_turu = olay.get("notificationType") or olay.get("eventType")
    ses_id = (olay.get("mail") or {}).get("messageId", "")
    if olay_turu == "Bounce":
        b = olay.get("bounce") or {}
        kalici = b.get("bounceType") == "Permanent"
        for r in b.get("bouncedRecipients") or []:
            _kaydet(r.get("emailAddress", ""),
                    EpostaGeriBildirim.Tur.BOUNCE if kalici else EpostaGeriBildirim.Tur.GECICI,
                    f"{b.get('bounceType', '')}/{b.get('bounceSubType', '')}",
                    r.get("diagnosticCode", ""), ses_id)
        return "bounce"
    if olay_turu == "Complaint":
        c = olay.get("complaint") or {}
        for r in c.get("complainedRecipients") or []:
            _kaydet(r.get("emailAddress", ""), EpostaGeriBildirim.Tur.SIKAYET,
                    c.get("complaintFeedbackType", ""), "", ses_id)
        return "sikayet"
    return "yok_sayildi"
