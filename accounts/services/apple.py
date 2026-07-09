"""
Apple Sign-In doğrulama.

Mobil istemci `@invertase/react-native-apple-authentication` ile alınan
`identityToken`'ı (JWT) gönderir. Apple'ın public key'leriyle imzayı,
issuer ve audience'ı (bundle id) doğrularız.
"""
from django.conf import settings

APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"


class AppleAuthError(Exception):
    pass


def verify_apple_identity_token(identity_token: str) -> dict:
    """
    Apple identityToken'ı doğrula ve kullanıcı bilgilerini döndür.

    Returns:
        {sub, email, email_verified}
    Raises:
        AppleAuthError
    """
    import jwt  # PyJWT
    from jwt import PyJWKClient

    if not identity_token:
        raise AppleAuthError("identityToken eksik.")

    audience = settings.APPLE_CLIENT_ID

    try:
        jwk_client = PyJWKClient(APPLE_KEYS_URL)
        signing_key = jwk_client.get_signing_key_from_jwt(identity_token)
        claims = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=APPLE_ISSUER,
        )
    except jwt.PyJWTError as e:
        raise AppleAuthError(f"Apple token doğrulanamadı: {e}") from e
    except Exception as e:  # ağ / key hatası
        raise AppleAuthError(f"Apple key doğrulama hatası: {e}") from e

    return {
        "sub": claims.get("sub"),
        "email": claims.get("email", ""),
        "email_verified": str(claims.get("email_verified", "false")).lower() == "true",
    }
