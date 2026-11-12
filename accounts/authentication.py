"""JWT kimlik doğrulaması + etkinlik izi (`User.last_seen_at`)."""
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings

logger = logging.getLogger(__name__)

ESKI_TOKEN_MESAJ = "Oturumunuz sona erdi, lütfen yeniden giriş yapın."


def eski_sunucu_tokeni_mi(payload: dict) -> bool:
    """Token, yedek sunucuda başka birine verilmiş bir ID'yi taşıyan eski bir token mı?

    Gerekçe: `config/settings.py` → "Eski sunucu token'ı koruması". Ayar kapalıyken
    (varsayılan) her zaman False.
    """
    uid_min = getattr(settings, "ESKI_TOKEN_UID_MIN", 0)
    sinir = getattr(settings, "ESKI_TOKEN_IAT_SINIRI", None)
    if not uid_min or sinir is None:
        return False
    try:
        uid = int(payload.get(api_settings.USER_ID_CLAIM))
    except (TypeError, ValueError):
        return False
    uid_max = getattr(settings, "ESKI_TOKEN_UID_MAX", 0)
    if uid < uid_min or (uid_max and uid > uid_max):
        return False
    iat = payload.get("iat")
    if iat is None:
        # Bu aralıkta kaynağı kanıtlanamayan token'a güvenilmez.
        return True
    try:
        return float(iat) < sinir.timestamp()
    except (TypeError, ValueError):
        return True


def _eski_tokeni_reddet(payload: dict) -> None:
    if eski_sunucu_tokeni_mi(payload):
        logger.warning("eski sunucu token'ı reddedildi (uid=%s, iat=%s)",
                       payload.get(api_settings.USER_ID_CLAIM), payload.get("iat"))
        raise InvalidToken({"detail": ESKI_TOKEN_MESAJ, "code": "token_not_valid"})


class EskiTokenKorumaliRefreshSerializer(TokenRefreshSerializer):
    """Yenileme ucu: eski sunucu token'ı yeni (geçerli görünen) bir token'a dönüşmesin.

    ⚠️ Yenileme `iat`'i SIFIRLAR — bu kontrol olmasaydı tek bir yenileme eski token'ı
    yedekte üretilmiş gibi gösterir ve erişim katmanındaki kontrolü atlatırdı.
    """

    def validate(self, attrs):
        _eski_tokeni_reddet(self.token_class(attrs["refresh"]).payload)
        return super().validate(attrs)

# Kullanıcı başına günlük işaret; TTL bir günden biraz uzun (gün sınırında
# saat dilimi kayması yüzünden ikinci bir UPDATE atılmasın diye).
_GORULDU_PREFIX = "seen:"
_GORULDU_TTL = 90000  # 25 saat


class SonGorulmeJWTAuthentication(JWTAuthentication):
    """
    Standart SimpleJWT doğrulaması + `last_seen_at` damgası.

    ⚠️ Damga kullanıcı başına GÜNDE BİR KEZ yazılır. Her istekte UPDATE atmak
    kabul edilemez: mobil uygulama sık istek gönderir ve `accounts_user`
    üzerinde gereksiz yazma yükü (write amplification) oluşur. İşaret Redis'te
    `cache.add` (SETNX) ile **atomik** rezerve edilir → eşzamanlı istekler tek
    UPDATE'e düşer.

    ⚠️ Damga yazımı ASLA kimlik doğrulamasını düşürmez: Redis/DB kaynaklı her
    hata yutulur ve loglanır. Etkinlik ölçümü uğruna API'yi kırmayız.
    """

    def get_validated_token(self, raw_token):
        token = super().get_validated_token(raw_token)
        _eski_tokeni_reddet(token.payload)
        return token

    def authenticate(self, request):
        sonuc = super().authenticate(request)
        if sonuc is not None:
            self._damgala(sonuc[0])
        return sonuc

    @staticmethod
    def _damgala(user) -> None:
        if user is None or not user.pk:
            return
        simdi = timezone.now()
        anahtar = f"{_GORULDU_PREFIX}{user.pk}:{timezone.localdate(simdi).isoformat()}"
        try:
            if not cache.add(anahtar, 1, _GORULDU_TTL):
                return  # bugün zaten damgalandı
            # `save()` DEĞİL: tam satır yazımı ve model sinyalleri tetiklenmesin.
            type(user).objects.filter(pk=user.pk).update(last_seen_at=simdi)
        except Exception:  # pragma: no cover - altyapı arızası
            logger.warning("last_seen_at güncellenemedi (uid=%s)", user.pk, exc_info=True)
