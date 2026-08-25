"""
Eylem araçları — YAZMAZLAR, yalnızca kullanıcının onaylayacağı bir ÖNERİ üretirler.

Her araç bir `AssistantAction` satırı oluşturur ve `ctx.oneriler`'e ekler; kart mesaj
payload'ına konur, kullanıcı butona bastığında gerçek yazma
`POST /assistant/actions/<id>/execute/` içinde, `tenders/services/actions.py` üzerinden
yapılır.

⚠️ Bu dosyada `SavedTender.objects.create` benzeri bir çağrı OLMAMALI. Modelin doğrudan
yazmasına izin vermek, halüsinasyonu kullanıcının verisine yazmak demektir; ayrıca
`require_premium` Celery içinde 403'e dönüşmediği için Pro kapısı da delinir.
"""
import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger("ihaletakip")

# Öneri ömrü. Kullanıcı sohbete ertesi gün dönebiliyor; ama bayat bir öneriyi
# uygulamak (ör. tarihi geçmiş ihaleye alarm) sessiz bir hatadır.
VARSAYILAN_OMUR = timedelta(days=7)


def _hata(mesaj, **ek):
    return {"ok": False, "hata": mesaj, **ek}


def _oneri_olustur(ctx, tur, params, ozet, expires_at=None):
    from assistant.models import AssistantAction

    if ctx.user is None:
        return _hata("Kullanıcı bağlamı yok.")
    eylem = AssistantAction.objects.create(
        user=ctx.user,
        tur=tur,
        params=params,
        ozet=ozet[:250],
        expires_at=expires_at or (timezone.now() + VARSAYILAN_OMUR),
    )
    ctx.oneriler.append(eylem)
    return {
        "ok": True,
        "action_id": str(eylem.id),
        "durum": "bekliyor",
        "not": (
            "Öneri kullanıcıya onay kartı olarak gösterilecek. Mesajında bunu KISACA "
            "belirt (ör. 'İstersen kaydedeyim') ama butonu tarif etme, kart zaten görünür."
        ),
    }


def _ihale_coz(ctx, ikn):
    """
    İKN'yi kart havuzundan çözer.

    ⚠️ Havuz DIŞINDAN İKN kabul edilmez: havuz yalnızca bir aracın gerçekten döndürdüğü
    ihaleleri içerir. Aksi hâlde model uydurduğu bir İKN'yi kullanıcının kayıtlarına
    yazdırabilirdi.
    """
    kart = ctx.card_pool.get(str(ikn or "").strip())
    if not kart:
        return None, _hata(
            f"{ikn} bu sohbette bulunmuş bir ihale değil. Önce ihale_ara ya da "
            "ihale_detay ile getir, sonra öner."
        )
    return kart, None


# ── İhaleyi kaydet ─────────────────────────────────────
def ihale_kaydet_oner(ctx, ikn=None, klasor=None, **_):
    kart, hata = _ihale_coz(ctx, ikn)
    if hata:
        return hata
    return _oneri_olustur(
        ctx,
        "ihale_kaydet",
        {
            "tender_ikn": kart["ikn"],
            "tender_id": kart.get("ekap_id") or "",
            "tender_title": (kart.get("ihale_adi") or "")[:500],
            "institution": (kart.get("idare_adi") or "")[:500],
            "tender_city": kart.get("il") or "",
            "tender_date": kart.get("ihale_tarihi") or "",
            "klasor": klasor or None,
        },
        f"“{(kart.get('ihale_adi') or kart['ikn'])[:90]}” ihalesini kayıtlarınıza ekleyeyim mi?",
    )


# ── Alarm kur ──────────────────────────────────────────
def alarm_kur_oner(ctx, ikn=None, ihale_gunu=True, dokuman_degisikligi=True, **_):
    kart, hata = _ihale_coz(ctx, ikn)
    if hata:
        return hata
    if not kart.get("ekap_id"):
        return _hata("Bu ihalenin ekap_id'si yok, alarm kurulamıyor.")
    return _oneri_olustur(
        ctx,
        "alarm_kur",
        {
            "tender_id": kart["ekap_id"],
            "tender_ikn": kart["ikn"],
            "tender_title": (kart.get("ihale_adi") or "")[:500],
            "institution": (kart.get("idare_adi") or "")[:500],
            "reminder_day": bool(ihale_gunu),
            "document_change": bool(dokuman_degisikligi),
        },
        f"“{(kart.get('ihale_adi') or kart['ikn'])[:90]}” için alarm kurayım mı?",
    )


# ── Filtre kaydet ──────────────────────────────────────
def filtre_kaydet_oner(ctx, ad=None, filtreler=None, alarm=False, **_):
    """
    Aramayı kayıtlı filtre olarak önerir. `filtreler` **`ihale_ara` ile aynı adları**
    kullanır — `SavedFilter.filters` doğrudan `apply_tender_filters`'a besleniyor,
    ara çeviri yok.
    """
    from assistant.tools import TOOL_SPECS

    if not ad or not str(ad).strip():
        return _hata("Filtreye bir ad verin.")
    if not isinstance(filtreler, dict) or not filtreler:
        return _hata("filtreler boş olamaz; ihale_ara'da kullandığın parametreleri ver.")

    # ihale_ara şemasıyla aynı doğrulama: bilinmeyen anahtar sessizce yok sayılırsa
    # kullanıcı, kurduğunu sandığından FARKLI bir filtre kaydeder ve alarmı yanlış çalışır.
    spec = next((t for t in TOOL_SPECS if t["name"] == "ihale_ara"), None)
    bilinen = set((spec or {}).get("input_schema", {}).get("properties") or {})
    bilinmeyen = sorted(set(filtreler) - bilinen)
    if bilinmeyen:
        return _hata(f"Şu filtre adları geçersiz: {', '.join(bilinmeyen)}.")

    return _oneri_olustur(
        ctx,
        "filtre_kaydet",
        {"name": str(ad)[:200], "filters": filtreler, "alarm": bool(alarm)},
        (f"“{str(ad)[:60]}” aramasını kayıtlı filtre olarak ekleyeyim mi?"
         + (" (yeni ihale çıkınca bildirim gönderilir)" if alarm else "")),
    )
