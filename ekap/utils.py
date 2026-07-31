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


# ── Para ayrıştırma ────────────────────────────────────────
# ⚠️ EKAP **aynı nesnede iki farklı sayı formatı** karıştırır:
#   sozlesmeBilgiList[].sozlesmeBedeli  → "18,128.00 TRY"  (EN: `,` binlik, `.` ondalık)
#   sozlesmeBilgiList[].yaklasikMaliyet → "529.820,00 TRY" (TR: `.` binlik, `,` ondalık)
# Eski sürüm koşulsuz TR kuralı uyguluyordu → "18,128.00" Decimal('18.128') oluyordu (18.13
# olarak kaydediliyordu) ve "2,017,840.00" InvalidOperation ile NULL'a düşüyordu. Bu yüzden
# ayrıştırıcı artık **formatı tespit eder**.
#
# ⚠️ Bundan da önemlisi: EKAP'ın `yaklasikMaliyet` / `kisimList[].*` string'leri **bozuk
# üretilir** — float'ın ondalık noktası silinip yeniden gruplanır:
#   repr(11454672.76) → "1145467276" → "1.145.467.276,00 TRY"   (100× şişmiş)
#   repr(4991250.0)   → "49912500"   → "49.912.500,00 TRY"      (10× şişmiş)
# Ölçek kayması float'ın ondalık hane sayısına bağlı olduğu için **hiçbir bölenle geri
# alınamaz**. Bu alanlar sayısala ÇEVRİLMEZ; doğru yaklaşık maliyetin tek kaynağı Sonuç
# İlanı HTML'idir (bkz. `ekap/sonuc_ilani.py`).


def _decimal_or_none(s) -> Decimal | None:
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_money(value) -> Decimal | None:
    """
    Formatlı para string'ini Decimal'e çevirir — **format tespitlidir**.
    "1.234.567,89 TL" (TR) ve "1,234,567.89 TRY" (EN) ikisi de doğru çözülür.
    Başarısızsa None döner (asla exception fırlatmaz).
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return _decimal_or_none(str(value))

    s = str(value).strip()
    if not s:
        return None
    # Sadece rakam, nokta, virgül, eksi bırak (TL/TRY/₺/boşluk temizlenir)
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None

    neg = s.startswith("-")
    s = s.lstrip("-")

    has_dot, has_comma = "." in s, "," in s

    if has_dot and has_comma:
        # İkisi de varsa **son görünen** ondalık ayırıcıdır; diğeri binliktir.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        parts = s.split(sep)
        if len(parts) > 2:
            # Birden çok ayırıcı → kesin binlik ("1,234,567" / "1.234.567")
            s = s.replace(sep, "")
        else:
            # Tek ayırıcı: son grubun uzunluğu karar verir.
            # 3 hane → binlik ("18,128" = 18128), değilse ondalık ("12,50" = 12.50)
            if len(parts[1]) == 3:
                s = parts[0] + parts[1]
            else:
                s = f"{parts[0]}.{parts[1]}"

    result = _decimal_or_none(s)
    if result is None:
        return None
    return -result if neg else result


def parse_money_value(value, *, zero_is_null: bool = True) -> Decimal | None:
    """
    EKAP'ın hazır sayısal `*Degeri` alanları için (`sozlesmeBedeliDegeri` vb.).
    Bunlar string'lerin aksine güvenilirdir — string ayrıştırmaya tercih edilmelidir.

    EKAP "bilinmiyor"u `0.0` ile temsil ediyor (`yaklasikMaliyetDegeri` DAİMA 0.0) →
    varsayılan olarak None'a çevrilir.
    """
    if value is None or isinstance(value, str):
        return None
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return None
    result = _decimal_or_none(str(value))
    if result is None:
        return None
    if zero_is_null and result == 0:
        return None
    return result


def pick_money(*candidates) -> Decimal | None:
    """
    İlk None-olmayan adayı döner. Kullanım:
        pick_money(parse_money_value(s.get("sozlesmeBedeliDegeri")),
                   parse_money(s.get("sozlesmeBedeli")))
    """
    for c in candidates:
        if c is not None:
            return c
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
