"""
Google Sign-In doğrulama.

Mobil istemci `@react-native-google-signin` ile alınan `idToken`'ı gönderir.
Burada Google'ın imzasını ve audience'ı (client id) doğrularız.
"""
from django.conf import settings


class GoogleAuthError(Exception):
    pass


def verify_google_id_token(id_token_str: str) -> dict:
    """
    Google idToken'ı doğrula ve kullanıcı bilgilerini döndür.

    Returns:
        {sub, email, name, picture, email_verified}
    Raises:
        GoogleAuthError — geçersiz/süresi dolmuş token
    """
    # Ağır importları fonksiyon içine al (manage.py check hafif kalsın)
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    if not id_token_str:
        raise GoogleAuthError("idToken eksik.")

    client_ids = settings.GOOGLE_CLIENT_IDS
    request = google_requests.Request()

    try:
        # audience=None → imza+expiry doğrulanır, audience'ı biz kontrol ederiz
        info = google_id_token.verify_oauth2_token(id_token_str, request)
    except ValueError as e:
        raise GoogleAuthError(f"Google token doğrulanamadı: {e}") from e

    # Issuer kontrolü
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise GoogleAuthError("Geçersiz token issuer.")

    # Audience kontrolü (yapılandırılmış client id'lerden biri olmalı)
    if client_ids and info.get("aud") not in client_ids:
        raise GoogleAuthError("Token audience eşleşmiyor.")

    return {
        "sub": info.get("sub"),
        "email": info.get("email", ""),
        "name": info.get("name", ""),
        "picture": info.get("picture", ""),
        "email_verified": info.get("email_verified", False),
    }
