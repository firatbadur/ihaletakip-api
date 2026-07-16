"""EKAP verisi için yardımcılar — para/tarih parse + Türkçe arama normalizasyonu."""
import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation


# ── Türkçe arama normalizasyonu ────────────────────────────
# Büyük/küçük harf ve Türkçe karakterleri ASCII'ye katlar; böylece arama
# **Türkçe-i güvenli ve aksan-duyarsız** olur:
#   "bilgi işlem" == "BİLGİ İŞLEM" == "Bilgi İşlem" == "bilgi islem"
# Neden gerekli: DB `icontains`/ILIKE Türkçe İ↔i, ş↔s katlamasını yapmaz →
# kullanıcı küçük harf Türkçe yazınca hiçbir şey eşleşmiyordu (EKAP eşleşiyor).
# Model tarafında normalize edilmiş sütuna (`*_norm`) yazılır, sorgu da normalize
# edilip `__contains` ile aranır (iki taraf da lowercase ASCII → DB-bağımsız).
_TR_FOLD = str.maketrans({
    "İ": "i", "I": "i", "ı": "i",
    "Ş": "s", "ş": "s",
    "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u",
    "Ö": "o", "ö": "o",
    "Ç": "c", "ç": "c",
})


def normalize_tr(text) -> str:
    """Metni Türkçe+aksan-duyarsız arama biçimine indirger (lowercase ASCII)."""
    if not text:
        return ""
    return str(text).translate(_TR_FOLD).lower().strip()


def parse_money(value) -> Decimal | None:
    """
    EKAP'ın formatlı para string'ini Decimal'e çevirir.
    Örn: "1.234.567,89 TL" → Decimal('1234567.89'). Başarısızsa None.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # Sadece rakam, nokta, virgül bırak (TL, boşluk, ₺ vb. temizlenir)
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    # Türkçe format: nokta binlik, virgül ondalık → normalize
    s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_ekap_datetime(value) -> datetime | None:
    """
    EKAP tarih string'ini timezone-aware datetime'a çevirir.
    Desteklenen: "23.03.2027 14:00", "23.03.2027", ISO ("2010-11-05T00:00:00[+03:00]").
    """
    if not value:
        return None
    s = str(value).strip()

    # ISO
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass

    # DD.MM.YYYY [HH:mm]
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=dt_timezone.utc)
        except ValueError:
            continue
    return None
