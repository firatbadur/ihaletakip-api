"""
E-posta sağlayıcı seçimi — admin'den (Uygulama Ayarları → `eposta_saglayici`) değiştirilir.

Amazon SES sandbox'tayken (yalnızca doğrulanmış adreslere gönderir) doğrulama kodları
yeni kullanıcıya ulaşamıyordu → geçici olarak Resend kullanılır, SES üretim onayı gelince
admin'den `ses` seçilir. İki sağlayıcı da SMTP konuşur; değişen yalnızca sunucu ve kimlik.

- Değerler: `ses` | `resend`. Satır yoksa `EMAIL_SAGLAYICI` (vars. `ses`).
- Seçim her bağlantı açılışında okunur (deploy/yeniden başlatma gerekmez).
- ⚠️ Bilinmeyen değer ya da kimliği eksik sağlayıcı → SES'e düşülür ve loglanır: yazım
  hatası e-postaları tümden durdurmasın.
- ⚠️ Geri dönen e-posta / şikâyet bildirimleri (`accounts/services/ses_sns.py`) yalnızca
  SES'ten gelir; Resend'deyken bu bildirimler toplanmaz.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend

logger = logging.getLogger("ihaletakip")

AYAR_ANAHTARI = "eposta_saglayici"
SAGLAYICILAR = ("ses", "resend")


def _kimlik(saglayici: str) -> dict:
    if saglayici == "resend":
        return {
            "host": settings.RESEND_SMTP_HOST,
            "port": settings.RESEND_SMTP_PORT,
            "username": "resend",
            "password": settings.RESEND_API_KEY,
        }
    return {
        "host": settings.EMAIL_HOST,
        "port": settings.EMAIL_PORT,
        "username": settings.EMAIL_HOST_USER,
        "password": settings.EMAIL_HOST_PASSWORD,
    }


def aktif_saglayici() -> str:
    """Admin'de seçili sağlayıcı (geçersiz/eksikse `ses`)."""
    from core.models import AppSetting

    try:
        secim = (AppSetting.get(AYAR_ANAHTARI, "") or "").strip().lower()
    except Exception:  # noqa: BLE001 — DB okunamazsa varsayılanla gönderilsin
        logger.exception("e-posta sağlayıcı ayarı okunamadı")
        secim = ""
    secim = secim or (settings.EMAIL_SAGLAYICI or "ses").strip().lower()
    if secim not in SAGLAYICILAR:
        logger.error("e-posta sağlayıcı geçersiz (%r) → ses kullanılıyor", secim)
        return "ses"
    if secim != "ses" and not _kimlik(secim)["password"]:
        logger.error("e-posta sağlayıcı %s seçili ama kimliği yok → ses kullanılıyor", secim)
        return "ses"
    return secim


class SaglayiciBackend(SMTPEmailBackend):
    """Seçili sağlayıcının SMTP sunucusuna bağlanan Django e-posta backend'i."""

    def __init__(self, host=None, port=None, username=None, password=None, **kwargs):
        self.saglayici = aktif_saglayici()
        kimlik = _kimlik(self.saglayici)
        super().__init__(
            host=host or kimlik["host"],
            port=port or kimlik["port"],
            username=kimlik["username"] if username is None else username,
            password=kimlik["password"] if password is None else password,
            **kwargs,
        )
