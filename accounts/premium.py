"""
Abonelik (Pro/premium) kapılama altyapısı.

Uygulama abonelik usulüdür: bazı özellikler yalnızca Pro kullanıcılara açıktır.
Free (ücretsiz) kullanıcılar sınırlandırılır ama üzülmez — kısıtlanan uçlar net bir
Türkçe mesaj ve makine-okunur `errors.code = "premium_required"` ile **403** döner.
Mobil bu kodu görünce kullanıcıya abonelik paketlerini sunar.

Premium durumu `User.is_premium` (bkz. accounts.models) üzerinden okunur. Bu modül
yalnızca kapılama yardımcılarını ve mesaj sabitlerini tutar — model importu yapmaz.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import APIException

# ── Free (ücretsiz) katman limitleri ───────────────────
# Free kullanıcı en fazla bu kadar filtre/ihale kaydedebilir; fazlası Pro ister.
FREE_SAVED_FILTER_LIMIT = 3
FREE_SAVED_TENDER_LIMIT = 3

# ── Türkçe kullanıcı mesajları (uçlara özel) ───────────
MSG_SAVED_FILTER_LIMIT = (
    f"Ücretsiz üyelikte en fazla {FREE_SAVED_FILTER_LIMIT} filtre kaydedebilirsiniz. "
    "Sınırsız filtre kaydetmek için Pro'ya geçin."
)
MSG_SAVED_TENDER_LIMIT = (
    f"Ücretsiz üyelikte en fazla {FREE_SAVED_TENDER_LIMIT} ihale kaydedebilirsiniz. "
    "Sınırsız ihale kaydetmek için Pro'ya geçin."
)
MSG_CHAT = (
    "İhale Asistanı ile sohbet Pro aboneliğe özeldir. Profilinizi oluşturabilirsiniz; "
    "asistanla sohbet etmek için Pro'ya geçin."
)
MSG_ANALYSIS = (
    "Yapay zeka doküman analizi Pro aboneliğe özeldir. Dokümanı indirebilirsiniz; "
    "analiz için Pro'ya geçin."
)
MSG_SUPPORT = (
    "Destek talebi oluşturma Pro aboneliğe özeldir. Pro'ya geçerek destek ekibimize "
    "ulaşabilirsiniz."
)


class PremiumRequired(APIException):
    """
    Pro aboneliği gerektiren bir işlem Free kullanıcı tarafından denendi.

    Zarf: {success:false, message:<mesaj>, data:null,
           errors:{code:"premium_required", detail:<mesaj>}} — HTTP 403.
    `errors.code` mobilde abonelik paketlerini açmak için kullanılır.
    """

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Bu özellik Pro aboneliğe özeldir."
    default_code = "premium_required"

    def __init__(self, detail: str | None = None):
        message = str(detail or self.default_detail)
        # detail bir dict → global exception handler bunu `errors` alanına koyar,
        # `message` alanına da içteki "detail"i çıkarır (bkz. core.response.extract_message).
        super().__init__({"code": self.default_code, "detail": message})


def require_premium(user, message: str | None = None) -> None:
    """Kullanıcı premium değilse PremiumRequired (403) fırlatır."""
    if not getattr(user, "is_premium", False):
        raise PremiumRequired(message)


def enforce_free_limit(user, *, current_count: int, limit: int, message: str) -> None:
    """
    Free kullanıcı için sayı-tabanlı limiti uygular. `current_count` yeni kayıt
    eklenmeden ÖNCEki mevcut kayıt sayısıdır; `>= limit` ise Pro istenir.
    Premium kullanıcılar hiç sınırlanmaz.
    """
    if getattr(user, "is_premium", False):
        return
    if current_count >= limit:
        raise PremiumRequired(message)
