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

def saved_filter_match(
    *, filter_name: str, count: int, saat: int = 24, first_title: str | None = None
) -> tuple[str, str]:
    """
    Bir filtreye uyan, **son `saat` saatte sisteme kaydedilen** ihaleler için bildirim.
    Başlık = filtre adı.

    ⚠️ Metin pencereyi açıkça söyler ("son 24 saatte") çünkü sayı **doğrulanabilir**
    olmalı: bildirime basınca mobil listeyi tam o kayıt penceresine kısıyor
    (`Notification.pencere_bas/bit` → `created_at_min/max`) ve kullanıcı iki sayıyı
    karşılaştırıyor. Belirsiz bir "bulundu" ifadesi, kullanıcının hangi kümeye
    bakacağını bilememesine yol açıyordu (üretimde bildirildi 2026-09-24).
    ⚠️ Metin "dün"/"bugün" DEMEZ: referans **kayıt tarihi**dir ve pencere takvim
    gününe oturmaz (pazartesi 08:00'deki pencere pazar 08:00'de başlar). Takvim günü
    ifadesi kullanmak, ölçülmüş hafta sonu deliğini metne taşımak olurdu.
    ⚠️ `count` penceredeki TOPLAMdır, "sana yeni olanlar" değil — bkz.
    `tenders.tasks.check_saved_filter_matches`.
    `first_title` kullanılmaz (bildirime basınca tek ihale DEĞİL, pencerenin listesi açılır).
    """
    name = clip(filter_name or "Kayıtlı Filtre")
    body = f"{name} filtrenize uygun son {saat} saatte {count} ihale yayımlandı."
    return name, body


# ── Birleşik özet: kullanıcı başına TEK bildirim ───────

def saved_filters_ozet(*, count: int, saat: int = 24) -> tuple[str, str]:
    """Kullanıcının TÜM kayıtlı filtrelerinin birleşimi için tek bildirim.

    ⚠️ **Filtre adı GEÇMEZ** — ürün gereksinimi: kullanıcı "şu filtrenin, bu filtrenin"
    değil "filtrelerinize uygun" görmek istiyor. Testle çivilenmiştir.
    ⚠️ "son N saatte" **korunur**: sayı doğrulanabilir olmalı, çünkü mobil aynı kayıt
    penceresini açıyor ve kullanıcı iki sayıyı karşılaştırıyor.
    ⚠️ `count` penceredeki **tekilleştirilmiş** toplamdır: 2 filtreye uyan ihale 1 sayılır.
    """
    return (
        "Size Uygun İhaleler",
        f"Kayıtlı filtrelerinize uygun son {saat} saatte {count} ihale yayımlandı.",
    )


def authorities_ozet(*, count: int, saat: int = 24) -> tuple[str, str]:
    """Kullanıcının TÜM favori idarelerinin birleşimi için tek bildirim.

    ⚠️ İdare adı GEÇMEZ (aynı gerekçe: `saved_filters_ozet`).
    """
    return (
        "Takip Ettiğiniz İdareler",
        f"Takip ettiğiniz idareler son {saat} saatte {count} ihale yayımladı.",
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


# ⚠️ `free_teaser` KALDIRILDI (2026-09-29): tek çağıranı `weekly_free_teaser` idi ve
# o görev, filtre + favori idare alarmları Free'ye açılınca geçersiz kaldı (bkz.
# `tenders/tasks.py` içindeki kaldırma notu). Yeniden bir Free→Pro teaser'ı
# yazılırsa metni de yeniden yazılmalı: eski gövde "kayıtlı filtrenize / favori
# idarenize uygun N ihale" diyordu, oysa bugün Free üye tam olarak onları alıyor.


# ── Favori idare: yeni ihale yayını ────────────────────

def authority_match(*, authority_name: str, count: int, saat: int = 24,
                    first_title: str | None = None) -> tuple[str, str]:
    """Favori idarenin **son `saat` saatte sisteme giren** ihaleleri; başlık = idare adı.

    ⚠️⚠️ Metin eskiden "Bugün" diyordu. Pencere 2026-09-28'de **kayıt tarihine**
    taşındığı için bu artık YANLIŞ olurdu: hafta sonu EKAP yayın yapmıyor, kayıtlar
    pazartesi gece düşüyor → pazartesi bildirilen bir ihalenin yayım tarihi cuma
    olabilir. "Bugün" demek, `saved_filter_match`'te düzeltilen dürüstlük sorununun
    aynısını idare tarafında tekrarlamak olurdu.
    ⚠️ `count` penceredeki TOPLAMdır, "sana yeni olanlar" değil.
    """
    title = clip(authority_name or "Favori İdare")
    if count == 1 and first_title:
        body = f"Yeni ihale: {clip(first_title, 80)}"
    else:
        body = f"Son {saat} saatte {count} ihale yayımladı"
    return title, body
