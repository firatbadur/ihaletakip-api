"""
Türkçe bildirim mesaj şablonları.

Eski `ihaletakip-scheduler` servisinin `app/notifications/templates.py`'sinden uyarlandı.
Her şablon `(title, body)` ikilisi döner. `ekap.Tender` alan adları kullanılır
(`ihale_adi`, `idare_adi`, `ikn`, `ekap_id`).
"""
from __future__ import annotations

from datetime import timedelta


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


# ── Alarm: ihale başına birleşik bildirim (her ihale için ayrı push) ──

def alarm_tender(tender, events) -> tuple[str, str]:
    """
    Tek bir ihale için birleşik alarm bildirimi. `events` = {"reminder","document","completed"}
    alt kümesi. Başlık = ihale adı, gövde = o ihalede tetiklenen olayların birleşimi. Her ihale
    için ayrı push atıldığından (birleşik kullanıcı özeti DEĞİL) tıklanınca ihale detayı açılır.
    """
    parts: list[str] = []
    if "reminder" in events:
        parts.append("Bugün ihale günü")
    if "document" in events:
        parts.append("Doküman güncellendi")
    if "completed" in events:
        parts.append("İhale sonuçlandı")
    body = " · ".join(parts) if parts else "İhale güncellemesi"
    return clip(tender.ihale_adi or "İhale Hatırlatıcısı"), body


# ── Alarm: kullanıcı başına birleşik özet (artık kullanılmıyor; ileride lazım olursa dursun) ──

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

_AYLAR = (
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)


def gun_etiketi(gun) -> str:
    """Bir tarihi kullanıcıya okunur zaman ifadesine çevirir ("dün", "bugün", "23 Eylül").

    ⚠️ Gövdedeki zaman ifadesi **süs değil sözleşmedir**: bildirime basınca mobil
    listeyi tam o güne kısıyor (`Notification.ilan_gun`) ve kullanıcı ekrandaki
    tarihlerle bildirimdeki ifadeyi karşılaştırıyor. "Bugün" yazıp dünü saymak,
    2026-09-24'te ölçülen "sayı tutmuyor" arızasının kılık değiştirmiş hâlidir.
    """
    from django.utils import timezone

    if gun is None:
        return ""
    bugun = timezone.localdate()
    if gun == bugun:
        return "bugün"
    if gun == bugun - timedelta(days=1):
        return "dün"
    return f"{gun.day} {_AYLAR[gun.month]} günü"


def saved_filter_match(
    *, filter_name: str, count: int, gun=None, first_title: str | None = None
) -> tuple[str, str]:
    """
    Bir filtreye uyan, **`gun` tarihinde yayımlanan** ihaleler için bildirim.
    Başlık = filtre adı.

    ⚠️ Metin zamanı açıkça söyler ("dün", "bugün") çünkü sayı **doğrulanabilir**
    olmalı: bildirime basınca mobil listeyi `Notification.ilan_gun` gününe kısıyor
    ve kullanıcı iki sayıyı karşılaştırıyor. Belirsiz bir "bulundu" ifadesi,
    kullanıcının hangi kümeye bakacağını bilememesine yol açıyordu (üretimde
    bildirildi 2026-09-24).
    ⚠️ `count` o günün TOPLAMIdır, "sana yeni olanlar" değil — bkz.
    `tenders.tasks.check_saved_filter_matches`.
    ⚠️ `gun` verilmezse zaman ifadesi yazılmaz; **"bugün" varsayılmaz**, çünkü
    varsayılan yanlış gün göstermenin en sessiz yoludur.
    `first_title` kullanılmaz (bildirime basınca tek ihale DEĞİL, o günün listesi açılır).
    """
    name = clip(filter_name or "Kayıtlı Filtre")
    ne_zaman = gun_etiketi(gun)
    zaman = f"{ne_zaman} " if ne_zaman else ""
    body = f"{name} filtrenize uygun {zaman}{count} ihale yayımlandı."
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


# ── Takip edilen firma: yeni sözleşme ──────────────────

def contractor_match(*, firma_adi: str, count: int, ihale_adi: str | None = None) -> tuple[str, str]:
    """Takip edilen firma yeni iş aldığında; başlık = firma adı."""
    title = clip(firma_adi or "Takip Edilen Firma")
    if count == 1 and ihale_adi:
        body = f"Yeni iş aldı: {clip(ihale_adi, 80)}"
    else:
        body = f"{count} yeni sözleşme imzaladı"
    return title, body


# ── Free teaser: kaçırılan eşleşmelerin haftalık özeti ─

def free_teaser(*, ihale: int, filtre: int, idare: int) -> tuple[str, str]:
    """
    Ücretsiz üyeye haftada bir: "bu hafta neyi kaçırdın".

    ⚠️ Sayılar **gerçek** olmalı — abartılmış ya da uydurulmuş bir teaser, kullanıcı Pro
    olup karşılığını göremeyince güveni kalıcı olarak bozar. Sıfır eşleşmede bu şablon
    hiç çağrılmaz (bkz. `weekly_free_teaser`).

    Gövde yalnızca **dolu olan** kaynakları sayar; "0 idare" gibi boş bir parça yazılmaz.
    """
    parcalar = []
    if filtre:
        parcalar.append(f"{filtre} kayıtlı filtrenize")
    if idare:
        parcalar.append(f"{idare} favori idarenize")
    kaynak = " ve ".join(parcalar) if parcalar else "ilgi alanlarınıza"
    return (
        "Bu hafta kaçırdıklarınız",
        f"{kaynak} uygun {ihale} yeni ihale yayımlandı. Pro ile hepsini görün.",
    )


# ── Favori idare: yeni ihale yayını ────────────────────

def authority_match(*, authority_name: str, count: int, first_title: str | None = None) -> tuple[str, str]:
    """Favori idarenin **bugün** yayımladığı ihaleler için; başlık = idare adı.

    ⚠️ "bugün" ifadesi bilinçli: `count` o günün toplamıdır ve kullanıcı bunu
    listedeki tarihlerden doğrulayabilmelidir (bkz. `saved_filter_match`).
    """
    title = clip(authority_name or "Favori İdare")
    if count == 1 and first_title:
        body = f"Bugün yeni ihale: {clip(first_title, 80)}"
    else:
        body = f"Bugün {count} ihale yayımladı"
    return title, body
