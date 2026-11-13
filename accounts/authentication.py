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


_KURAL_ANAHTAR = "kimlik_esleme:kurallar:v1"
_KURAL_TTL = 300

# Token kararı
DOGAL = "dogal"   # eşleme yok → token'daki kullanıcı
ESLE = "esle"     # başka bir kullanıcıya çevrilir
RED = "red"       # reddedilir


def _kurallar() -> list[tuple[int, float | None, float | None, int | None]]:
    """`KimlikEsleme` satırları (uid, iat_bas_ts, iat_bit_ts, hedef_id) — önbellekli.

    Satırların çoğu ortamda hiç olmadığı için her istekte DB'ye gidilmez; boş liste de
    önbelleğe yazılır. Önbellek arızasında DB'den okunur (güvenlik kararı önbelleğe
    bağlı kalmasın).
    """
    try:
        kurallar = cache.get(_KURAL_ANAHTAR)
    except Exception:  # pragma: no cover - altyapı arızası
        kurallar = None
    if kurallar is not None:
        return kurallar
    from .models import KimlikEsleme

    kurallar = [
        (k.eski_uid,
         k.iat_bas.timestamp() if k.iat_bas else None,
         k.iat_bit.timestamp() if k.iat_bit else None,
         k.hedef_id)
        for k in KimlikEsleme.objects.all()
    ]
    try:
        cache.set(_KURAL_ANAHTAR, kurallar, _KURAL_TTL)
    except Exception:  # pragma: no cover
        pass
    return kurallar


def kurallari_unut() -> None:
    """Önbelleği sil (ORM yazımında sinyal çağırır; ham SQL'den sonra elle çağrılmalı)."""
    try:
        cache.delete(_KURAL_ANAHTAR)
    except Exception:  # pragma: no cover
        logger.warning("kimlik eşleme önbelleği silinemedi", exc_info=True)


def token_karari(payload: dict) -> tuple[str, int | None]:
    """Token için karar: (DOGAL, None) | (ESLE, hedef_id) | (RED, None).

    Önce `KimlikEsleme` kuralları (sunucu dönüşü eşlemesi), eşleşme yoksa ortam
    değişkenli `ESKI_TOKEN_*` koruması. Kural bu korumanın ÖNÜNDEDİR: korumanın
    reddettiği bir aralığı kural açıkça bir kullanıcıya bağlayabilir.
    """
    try:
        uid = int(payload.get(api_settings.USER_ID_CLAIM))
    except (TypeError, ValueError):
        uid = None
    if uid is not None:
        uid_kurallari = [k for k in _kurallar() if k[0] == uid]
        if uid_kurallari:
            iat = payload.get("iat")
            try:
                iat = float(iat)
            except (TypeError, ValueError):
                # Kurallı bir UID'de kaynağı kanıtlanamayan token'a güvenilmez.
                return RED, None
            for _uid, bas, bit, hedef in uid_kurallari:
                if (bas is None or iat >= bas) and (bit is None or iat < bit):
                    return (RED, None) if hedef is None else (ESLE, hedef)
    if eski_sunucu_tokeni_mi(payload):
        return RED, None
    return DOGAL, None


def _reddet(payload: dict) -> None:
    logger.warning("eski sunucu token'ı reddedildi (uid=%s, iat=%s)",
                   payload.get(api_settings.USER_ID_CLAIM), payload.get("iat"))
    raise InvalidToken({"detail": ESKI_TOKEN_MESAJ, "code": "token_not_valid"})


def _eski_tokeni_reddet(payload: dict) -> None:
    if token_karari(payload)[0] == RED:
        _reddet(payload)


def _hedef_kullanici(hedef_id: int):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=hedef_id).first()
    if user is None or not user.is_active:
        raise InvalidToken({"detail": ESKI_TOKEN_MESAJ, "code": "token_not_valid"})
    return user


class EskiTokenKorumaliRefreshSerializer(TokenRefreshSerializer):
    """Yenileme ucu: eski sunucu token'ı yeni (geçerli görünen) bir token'a dönüşmesin;
    eşlenmiş token ise HEDEF kullanıcı için yeni bir çifte dönüşsün.

    ⚠️ Yenileme `iat`'i SIFIRLAR — bu kontrol olmasaydı tek bir yenileme eski token'ı
    yedekte üretilmiş gibi gösterir ve erişim katmanındaki kontrolü atlatırdı.
    ⚠️ Eşlemede kütüphanenin `validate`'i KULLANILAMAZ: döndürmede aynı payload'ı
    (aynı `user_id`) yeniden imzalar. Burada hedef için `for_user` ile yeni çift üretilir,
    eski token kara listeye alınır (tek kullanımlık kalsın).
    """

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])  # imza + süre + kara liste
        karar, hedef_id = token_karari(refresh.payload)
        if karar == RED:
            _reddet(refresh.payload)
        if karar == DOGAL:
            return super().validate(attrs)

        user = _hedef_kullanici(hedef_id)
        if api_settings.BLACKLIST_AFTER_ROTATION:
            try:
                refresh.blacklist()
            except AttributeError:  # pragma: no cover - blacklist uygulaması yoksa
                pass
        yeni = self.token_class.for_user(user)
        logger.info("eşlenmiş token yenilendi (uid=%s → %s)",
                    refresh.payload.get(api_settings.USER_ID_CLAIM), user.pk)
        return {"access": str(yeni.access_token), "refresh": str(yeni)}

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

    def get_user(self, validated_token):
        karar, hedef_id = token_karari(validated_token.payload)
        if karar == ESLE:
            return _hedef_kullanici(hedef_id)
        return super().get_user(validated_token)

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
