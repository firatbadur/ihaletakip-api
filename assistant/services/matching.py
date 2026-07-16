"""
Kural tabanlı ihale eşleştirme — profil haritasını ekap.Tender'a uygular.

Skorlama:
  +3  anahtar kelime ihale adında geçiyor (keyword başına, en fazla 3 kelime sayılır)
  +2  OKAS kod öneki eşleşmesi
  +1  şehir eşleşmesi
  +1  ihale türü eşleşmesi
"""
import logging

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

# Katılıma açık ihaleler (bkz. ekap/views.py durum dokümantasyonu: 2/3 Katılıma Açık)
OPEN_STATUSES = [2, 3]

TENDER_TYPE_LABELS = {1: "Mal Alımı", 2: "Yapım", 3: "Hizmet", 4: "Danışmanlık"}


def match_tenders_for_profile(profile, since=None, limit: int = 10, min_score: float = 3.0) -> list:
    """
    Profil haritasına göre AÇIK (katılıma açık) ve teklifi geçmemiş ihaleleri skorlar.
    Dönen: [(tender, score, reasons), ...] — skora göre azalan, en fazla `limit` adet.

    Not: `ilan_tarihi` verisi liste senkronunda çoğu zaman boş kaldığından ona göre
    filtrelenmez. Bunun yerine durum (2/3) + teklifi geçmemiş (ihale_tarihi >= şimdi
    veya boş) kullanılır. `since` verilirse ek olarak son X günde ilan edilenlere
    daraltır (yalnızca ilan_tarihi DOLU olanlar için). Tekrar önerme dedup ile önlenir.
    """
    from ekap.models import Tender

    from ekap.utils import normalize_tr

    pm = profile.profile_map or {}
    # Türkçe-i güvenli anahtar kelime eşleştirmesi: profil keyword'leri ve ihale adı
    # aynı normalize biçime indirgenir (yalın .lower() İ↔i, ş↔s katlamaz → kaçırırdı).
    keywords = [normalize_tr(k) for k in pm.get("keywords", []) if k]
    okas_prefixes = [p for p in pm.get("okas_prefixes", []) if p]
    city_ids = profile.cities or pm.get("city_ids") or []
    tender_types = profile.tender_types or pm.get("tender_types") or []

    now = timezone.now()
    # Açık + teklif süresi geçmemiş (ihale_tarihi gelecekte ya da bilinmiyor)
    qs = Tender.objects.filter(ihale_durum__in=OPEN_STATUSES).filter(
        Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True)
    )
    # since verildiyse VE ilan_tarihi doluysa son X güne daralt (beat için opsiyonel)
    if since is not None:
        qs = qs.filter(Q(ilan_tarihi__gte=since) | Q(ilan_tarihi__isnull=True))
    # İndexli ön-filtreler: kullanıcı şehir/tür seçtiyse kapsamı daralt
    if city_ids:
        qs = qs.filter(il_id__in=city_ids)
    if tender_types:
        qs = qs.filter(ihale_tip__in=tender_types)

    # Kullanıcının zaten KAYDETTİĞİ ihaleleri önerme (SavedTender)
    from tenders.models import SavedTender

    saved_ikns = list(
        SavedTender.objects.filter(user=profile.user).values_list("tender_ikn", flat=True)
    )
    if saved_ikns:
        qs = qs.exclude(ikn__in=saved_ikns)

    qs = qs.order_by("ihale_tarihi").prefetch_related("okas_kalemleri")[:500]  # güvenlik tavanı

    scored = []
    for tender in qs:
        score = 0.0
        reasons = []

        name = tender.ihale_adi_norm or normalize_tr(tender.ihale_adi)
        hits = [kw for kw in keywords if kw in name][:3]
        for kw in hits:
            score += 3.0
            reasons.append(f"Anahtar kelime: {kw}")

        if okas_prefixes:
            okas_codes = [item.kodu for item in tender.okas_kalemleri.all() if item.kodu]
            if any(code.startswith(p) for code in okas_codes for p in okas_prefixes):
                score += 2.0
                reasons.append("OKAS kategorisi uyumlu")

        if city_ids and tender.il_id in city_ids:
            score += 1.0
            reasons.append(f"Şehir: {tender.ihale_il_adi or tender.il_id}")

        if tender_types and tender.ihale_tip in tender_types:
            score += 1.0
            reasons.append(f"Tür: {TENDER_TYPE_LABELS.get(tender.ihale_tip, tender.ihale_tip)}")

        if score >= min_score:
            scored.append((tender, score, reasons))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def tender_card(tender) -> dict:
    """Sohbet/digest mesajlarında kullanılan ihale kartı sözlüğü."""
    return {
        "ikn": tender.ikn,
        "ekap_id": tender.ekap_id,
        "ihale_adi": tender.ihale_adi,
        "idare_adi": tender.idare_adi,
        "il": tender.ihale_il_adi,
        "ihale_tarihi": tender.ihale_tarih_saat or (
            tender.ihale_tarihi.strftime("%d.%m.%Y %H:%M") if tender.ihale_tarihi else ""
        ),
        "ihale_tip": tender.ihale_tip,
    }
