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


def engellileri_ayikla(email_messages) -> list:
    """Engelli (`accounts.EpostaEngeli`) alıcıları mesajlardan çıkarır.

    Alıcısı kalmayan mesaj listeden düşer. ⚠️ DB okunamazsa mesajlar olduğu gibi döner:
    kontrol hatası yüzünden doğrulama kodu yutulmasın.
    """
    from accounts.services.eposta_engel import engelli_kume, normalize

    try:
        engelli = engelli_kume(a for m in email_messages for a in m.recipients())
    except Exception:  # noqa: BLE001
        logger.exception("e-posta engel kontrolü yapılamadı — mesajlar olduğu gibi gönderiliyor")
        return list(email_messages)
    if not engelli:
        return list(email_messages)
    kalan = []
    for m in email_messages:
        for alan in ("to", "cc", "bcc"):
            setattr(m, alan, [a for a in getattr(m, alan) if normalize(a) not in engelli])
        if m.recipients():
            kalan.append(m)
        else:
            logger.info("e-posta gönderilmedi (engelli adres): %r", m.subject)
    return kalan


class SaglayiciBackend(SMTPEmailBackend):
    """Seçili sağlayıcının SMTP sunucusuna bağlanan Django e-posta backend'i.

    ⚠️ Engelli adresler burada ayıklanır — her gönderim yolu (bülten, toplu gönderim,
    doğrulama kodu, destek) bu kapıdan geçer.
    """

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

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        return super().send_messages(engellileri_ayikla(email_messages))


# ── Marka / tasarım ─────────────────────────────────────────────────────────
# Tüm e-postalar (bülten, toplu gönderim, doğrulama kodu) `eposta/duzen/<tema>.html`
# düzeninden türer. Tema admin'den seçilir (Uygulama Ayarları → `eposta_tasarim`).
# ⚠️ Renkler e-posta istemcileri yüzünden SATIR İÇİ yazılır (Gmail <style>'ı kısmen
# siler); token'lar burada tek yerde durur.
TASARIM_ANAHTARI = "eposta_tasarim"
TEMALAR = {
    "a": {
        "ad": "Kurumsal", "zemin": "#eef2f7", "govde": "#ffffff", "vurgu": "#0074cb",
        "koyu": "#003ea1", "yazi": "#16212d", "ikincil": "#4d5b69", "soluk": "#7d8a97",
        "kenar": "#e3e9f0", "satir": "#eef2f6", "etiket_zemin": "#eaf3fc",
        "etiket_yazi": "#0062ad", "kutu": "#f4f8fc", "radius": "12", "font":
        "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif",
        "baslik_font": "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif",
        "numarali": False, "kart": True,
    },
    "b": {
        "ad": "Modern", "zemin": "#e9eef7", "govde": "#ffffff", "vurgu": "#0074cb",
        "koyu": "#0a1f4d", "yazi": "#0f1b2d", "ikincil": "#475569", "soluk": "#7b8798",
        "kenar": "#e2e8f0", "satir": "#f1f5f9", "etiket_zemin": "#0074cb",
        "etiket_yazi": "#ffffff", "kutu": "#f1f6fd", "radius": "18", "font":
        "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif",
        "baslik_font": "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif",
        "numarali": False, "kart": True,
    },
    "c": {
        "ad": "Sade", "zemin": "#f6f5f2", "govde": "#ffffff", "vurgu": "#0062ad",
        "koyu": "#003ea1", "yazi": "#1c1c1c", "ikincil": "#4b4b4b", "soluk": "#8a8a8a",
        "kenar": "#e7e4dd", "satir": "#ece9e2", "etiket_zemin": "#ffffff",
        "etiket_yazi": "#0062ad", "kutu": "#f6f5f2", "radius": "4", "font":
        "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif",
        "baslik_font": "Georgia,'Times New Roman',serif",
        "numarali": True, "kart": False,
    },
}
VARSAYILAN_TEMA = "b"


def aktif_tema() -> str:
    from core.models import AppSetting

    try:
        secim = (AppSetting.get(TASARIM_ANAHTARI, "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        secim = ""
    return secim if secim in TEMALAR else VARSAYILAN_TEMA


def marka_baglami(tema: str | None = None) -> dict:
    """Her e-posta şablonunun ortak bağlamı: tema token'ları + logo + bağlantılar.

    ⚠️ Logo adresleri HASH'SİZ statik yoldur: e-posta aylar sonra açıldığında da
    görünmeli (manifest hash'i her deploy'da değişebilir). Gmail SVG göstermez → PNG.
    """
    tema = tema if tema in TEMALAR else aktif_tema()
    statik = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/static/ihaletakip/eposta"
    site = settings.EPOSTA_LINK_TABANI.rstrip("/")
    return {
        "tema": tema,
        "t": TEMALAR[tema],
        "duzen": f"eposta/duzen/{tema}.html",
        "marka": {
            "logo": f"{statik}/logo.png",
            "logo_beyaz": f"{statik}/logo-beyaz.png",
            "ikon": f"{statik}/ikon.png",
            "site": site,
            "site_ad": site.split("://", 1)[-1],
            "app_store": "https://apps.apple.com/app/id6761832614",
            "google_play": "https://play.google.com/store/apps/details?id=com.envisoft.ihaletakip",
            "sirket": "Envisoft Otomasyon Bilgi ve Bilişim Teknolojileri Ltd. Şti.",
        },
    }
