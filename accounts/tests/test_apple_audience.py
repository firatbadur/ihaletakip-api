"""
Apple Sign-In audience listesi — web portalı (Services ID) + iOS (bundle id).

Mobil girişin değişmeden çalışması bu testin asıl korumasıdır: `APPLE_CLIENT_IDS`
env'e hiç yazılmazsa varsayılan yalnızca bundle id'dir.
"""
import time
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, override_settings

from accounts.services.apple import (
    APPLE_ISSUER,
    AppleAuthError,
    verify_apple_identity_token,
)

_ANAHTAR = rsa.generate_private_key(public_exponent=65537, key_size=2048)

IOS = "com.envisoft.ihaletakip"
WEB = "com.envisoft.ihaletakip.web"


def _token(aud):
    simdi = int(time.time())
    return jwt.encode(
        {"iss": APPLE_ISSUER, "aud": aud, "sub": "000123.abc", "email": "a@b.c",
         "email_verified": "true", "iat": simdi, "exp": simdi + 600},
        _ANAHTAR, algorithm="RS256",
    )


def _dogrula(token):
    sahte = mock.Mock()
    sahte.get_signing_key_from_jwt.return_value = mock.Mock(key=_ANAHTAR.public_key())
    with mock.patch("jwt.PyJWKClient", return_value=sahte):
        return verify_apple_identity_token(token)


class AppleAudienceTest(SimpleTestCase):
    @override_settings(APPLE_CLIENT_ID=IOS, APPLE_CLIENT_IDS=[IOS, WEB])
    def test_web_services_id_kabul(self):
        self.assertEqual(_dogrula(_token(WEB))["sub"], "000123.abc")

    @override_settings(APPLE_CLIENT_ID=IOS, APPLE_CLIENT_IDS=[IOS, WEB])
    def test_ios_bundle_id_hala_kabul(self):
        self.assertEqual(_dogrula(_token(IOS))["sub"], "000123.abc")

    @override_settings(APPLE_CLIENT_ID=IOS, APPLE_CLIENT_IDS=[IOS, WEB])
    def test_bilinmeyen_audience_red(self):
        with self.assertRaises(AppleAuthError):
            _dogrula(_token("com.baskasi.app"))

    @override_settings(APPLE_CLIENT_ID=IOS, APPLE_CLIENT_IDS=[IOS])
    def test_varsayilan_yalniz_ios_web_reddedilir(self):
        """Env'e liste yazılmadan deploy edilirse davranış eskisiyle aynı kalır."""
        self.assertEqual(_dogrula(_token(IOS))["sub"], "000123.abc")
        with self.assertRaises(AppleAuthError):
            _dogrula(_token(WEB))
