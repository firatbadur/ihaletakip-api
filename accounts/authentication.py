"""JWT kimlik doğrulaması + etkinlik izi (`User.last_seen_at`)."""
import logging

from django.core.cache import cache
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication

logger = logging.getLogger(__name__)

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
