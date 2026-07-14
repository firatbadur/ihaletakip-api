"""
FCM push gönderici.

Eski `ihaletakip-scheduler` servisinin `app/firebase/fcm.py` mantığının senkron
(Celery içi) portu. `firebase_admin` **lazy** import edilir → `manage.py check` ve
hafif komutlar bu bağımlılık olmadan çalışır (kodlama kuralı).

`FCM_CREDENTIALS` ayarı boşsa ya da dosya yoksa gönderim **no-op**'tur (push devre
dışı; çağıran taraf yine de uygulama-içi `Notification` satırını yazar).
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from django.conf import settings

logger = logging.getLogger("ihaletakip")

# Gönderim sonucu durumları
SENT = "sent"
INVALID_TOKEN = "invalid_token"  # token ölü → çağıran fcm_token'ı temizlemeli
ERROR = "error"
DISABLED = "disabled"  # kimlik yok → push kapalı

# firebase_admin app tekildir; süreç başına bir kez başlatılır.
_app = None
_app_lock = threading.Lock()
_init_failed = False


def _get_app():
    """Firebase app'ini tek seferlik başlatır. Kimlik yoksa None döner (no-op)."""
    global _app, _init_failed

    if _app is not None:
        return _app
    if _init_failed:
        return None

    with _app_lock:
        if _app is not None:
            return _app
        if _init_failed:
            return None

        cred_path = getattr(settings, "FCM_CREDENTIALS", "") or ""
        if not cred_path:
            logger.info("FCM devre dışı: FCM_CREDENTIALS tanımsız.")
            _init_failed = True
            return None

        import os

        if not os.path.exists(cred_path):
            logger.warning("FCM devre dışı: kimlik dosyası bulunamadı (%s).", cred_path)
            _init_failed = True
            return None

        try:
            import firebase_admin
            from firebase_admin import credentials

            options = {}
            project_id = getattr(settings, "FCM_PROJECT_ID", "") or ""
            if project_id:
                options["projectId"] = project_id

            _app = firebase_admin.initialize_app(
                credentials.Certificate(cred_path), options
            )
            logger.info("FCM başlatıldı (project=%s).", project_id or "?")
        except Exception:
            logger.exception("FCM başlatma hatası — push devre dışı.")
            _init_failed = True
            return None

    return _app


def is_enabled() -> bool:
    """Push gönderimi mümkün mü (kimlik yüklendi mi)?"""
    return _get_app() is not None


def send_fcm(token: str, title: str, body: str, data: dict[str, Any] | None = None) -> str:
    """
    Tek bir cihaza FCM push gönderir. Sonuç durumunu döner (SENT/INVALID_TOKEN/ERROR/DISABLED).

    `data` değerleri iOS uyumu için string'e çevrilir. Ölü token durumunda INVALID_TOKEN
    döner → çağıran `user.fcm_token`'ı temizlemeli.
    """
    app = _get_app()
    if app is None:
        return DISABLED
    if not token:
        return DISABLED

    from firebase_admin import messaging

    # Tüm data alanları string olmalı (iOS şartı).
    str_data = {
        k: ("" if v is None else str(v))
        for k, v in (data or {}).items()
    }

    android = messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(
            channel_id="ihaletakip",
            sound="default",
        ),
    )
    apns = messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(sound="default", content_available=True),
        ),
    )
    message = messaging.Message(
        token=token,
        notification=messaging.Notification(title=title, body=body),
        data=str_data,
        android=android,
        apns=apns,
    )

    try:
        message_id = messaging.send(message)
        logger.debug("fcm gönderildi id=%s", message_id)
        return SENT
    except messaging.UnregisteredError:
        logger.info("fcm token kayıtsız (unregistered) — temizlenecek.")
        return INVALID_TOKEN
    except messaging.SenderIdMismatchError:
        logger.info("fcm sender id uyuşmazlığı — token temizlenecek.")
        return INVALID_TOKEN
    except ValueError as exc:
        # Geçersiz token biçimi (client-side doğrulama) → ölü kabul et.
        logger.info("fcm geçersiz token: %s", exc)
        return INVALID_TOKEN
    except Exception as exc:
        # firebase-admin 6.x: bozuk/kayıtsız token InvalidArgumentError verir.
        # Yalnızca token kaynaklı ise ölü say (mesaj kurulum hatasını ERROR bırak).
        from firebase_admin import exceptions as fa_exc

        if isinstance(exc, fa_exc.InvalidArgumentError) and "registration token" in str(exc).lower():
            logger.info("fcm geçersiz kayıt token'ı — temizlenecek: %s", exc)
            return INVALID_TOKEN
        logger.exception("fcm gönderim hatası.")
        return ERROR
