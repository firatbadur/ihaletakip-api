"""
Türkçe bildirim mesaj şablonları.

Eski `ihaletakip-scheduler` servisinin `app/notifications/templates.py`'sinden uyarlandı.
Her şablon `(title, body)` ikilisi döner. `ekap.Tender` alan adları kullanılır
(`ihale_adi`, `idare_adi`, `ikn`, `ekap_id`).
"""
from __future__ import annotations


def clip(text: str | None, limit: int = 60) -> str:
    """Metni `limit` karaktere kırpar (ellipsis ile)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ── Alarm: tekil ihale hatırlatıcıları ─────────────────

def reminder_day(tender) -> tuple[str, str]:
    return "İhale Günü", f"Bugün ihale günü: {clip(tender.ihale_adi or 'İhale')}"


def document_change(tender) -> tuple[str, str]:
    return "Doküman Güncellendi", f"{clip(tender.ihale_adi or 'İhale')} dokümanı güncellendi"


def completed(tender) -> tuple[str, str]:
    return "İhale Sonuçlandı", f"{clip(tender.ihale_adi or 'İhale')} ihalesi tamamlandı"


# ── Alarm: kullanıcı başına birleşik özet push ─────────
# 09:00 alarm görevinde tek push atmak için, kullanıcının o günkü tüm olayları özetlenir.

def alarm_summary(
    *, reminder_count: int, document_count: int, completed_count: int
) -> tuple[str, str]:
    """
    Birden çok alarm olayını tek push'ta özetler. Tek olay varsa doğal cümle,
    çok olay varsa noktalı liste kurar.
    """
    parts: list[str] = []
    if reminder_count:
        parts.append(
            "1 ihalenizin günü bugün" if reminder_count == 1
            else f"{reminder_count} ihalenizin günü bugün"
        )
    if document_count:
        parts.append(
            "1 ihalede doküman değişikliği" if document_count == 1
            else f"{document_count} ihalede doküman değişikliği"
        )
    if completed_count:
        parts.append(
            "1 ihale sonuçlandı" if completed_count == 1
            else f"{completed_count} ihale sonuçlandı"
        )
    body = " · ".join(parts) if parts else "İhale güncellemeleriniz var"
    return "İhale Hatırlatıcıları", body


# ── Kayıtlı filtre: yeni ihale eşleşmesi ───────────────

def saved_filter_match(*, filter_name: str, count: int, first_title: str | None = None) -> tuple[str, str]:
    """
    Bir filtreye uyan yeni ihale(ler) için bildirim. Başlık = filtre adı, gövde =
    "{filtre} filtrenize uygun N adet ihale bulundu." (ör. "Otomasyon filtrenize uygun
    5 adet ihale bulundu."). `first_title` artık kullanılmaz (bildirime basınca tek ihale
    DEĞİL, filtrenin sonuç listesi açılır — bkz. `filter_id` derin bağlantısı).
    """
    name = clip(filter_name or "Kayıtlı Filtre")
    body = f"{name} filtrenize uygun {count} adet ihale bulundu."
    return name, body


def okas_recommendation(*, count: int) -> tuple[str, str]:
    """
    Kayıtlı ihalelerin OKAS kodlarına göre günlük öneri bildirimi (Free/Pro herkese).
    Bildirime basınca `okas_kodlar` ile OKAS arama sonuçları açılır.
    """
    return (
        "Size Özel İhaleler",
        f"İlgilendiğiniz kategorilerde {count} yeni ihale yayınlandı.",
    )


# ── Favori idare: yeni ihale yayını ────────────────────

def authority_match(*, authority_name: str, count: int, first_title: str | None = None) -> tuple[str, str]:
    """Favori idarenin yayınladığı yeni ihale(ler) için başlık = idare adı."""
    title = clip(authority_name or "Favori İdare")
    if count == 1 and first_title:
        body = f"Yeni ihale: {clip(first_title, 80)}"
    else:
        body = f"{count} yeni ihale yayımlandı"
    return title, body
