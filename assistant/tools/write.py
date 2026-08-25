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

# Tek kartla kaydedilebilecek azami ihale — `ihale_ara`nın sayfa tavanıyla aynı.
AZAMI_TOPLU = 20


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


# ══════════════════════════════════════════════════════════════════════════════
# Faz 3 eylemleri
#
# Ortak kural: öneri üretmeden ÖNCE hedefin gerçekten var olduğu doğrulanır
# (firma id'si, DETSIS no'su, silinecek kaydın kendisi). Doğrulama yapılmazsa
# model uydurduğu bir kimlik için onay kartı çıkarır, kullanıcı "Evet"e basar ve
# hata ancak yazma anında görünür — kullanıcı açısından asistan yalan söylemiş olur.
# ══════════════════════════════════════════════════════════════════════════════


# ── İhaleleri toplu kaydet ─────────────────────────────
def toplu_ihale_kaydet_oner(ctx, iknler=None, klasor=None, **_):
    """
    Birden çok ihaleyi TEK kartla kaydetmeyi önerir.

    ⚠️ İhale başına ayrı kart çıkarma: "hepsini kaydet" dendiğinde 12 kart üst üste
    binerek sohbeti kullanılamaz hâle getirir ve kullanıcı 12 kez onaylamak zorunda kalır.
    """
    if not isinstance(iknler, list) or not iknler:
        return _hata("iknler listesi boş olamaz.")
    if len(iknler) > AZAMI_TOPLU:
        return _hata(f"Tek seferde en çok {AZAMI_TOPLU} ihale kaydedilebilir.")

    kartlar, eksik = [], []
    for ikn in iknler:
        kart = ctx.card_pool.get(str(ikn or "").strip())
        (kartlar if kart else eksik).append(kart or ikn)
    if eksik:
        return _hata(
            f"Şu İKN'ler bu sohbette bulunmuş ihaleler değil: {', '.join(map(str, eksik))}."
        )

    return _oneri_olustur(
        ctx,
        "toplu_ihale_kaydet",
        {
            "klasor": klasor or None,
            "kayitlar": [
                {
                    "tender_ikn": k["ikn"],
                    "tender_id": k.get("ekap_id") or "",
                    "tender_title": (k.get("ihale_adi") or "")[:500],
                    "institution": (k.get("idare_adi") or "")[:500],
                    "tender_city": k.get("il") or "",
                    "tender_date": k.get("ihale_tarihi") or "",
                }
                for k in kartlar
            ],
        },
        (f"{len(kartlar)} ihaleyi "
         + (f"“{str(klasor)[:40]}” klasörüne" if klasor else "kayıtlarınıza")
         + " ekleyeyim mi?"),
    )


# ── Kayıtlı ihaleyi klasöre taşı ───────────────────────
def ihale_tasi_oner(ctx, ikn=None, klasor=None, **_):
    """Kayıtlı ihaleyi başka klasöre taşımayı önerir. `klasor` boş → "Genel"."""
    from tenders.models import SavedTender

    ikn = str(ikn or "").strip()
    if not ikn:
        return _hata("İKN gerekli.")
    kayit = SavedTender.objects.filter(user=ctx.user, tender_ikn=ikn).first()
    if kayit is None:
        return _hata(f"{ikn} kullanıcının kayıtlılarında yok; önce kaydetmeyi öner.")
    hedef = str(klasor).strip() if klasor else "Genel"
    return _oneri_olustur(
        ctx,
        "ihale_tasi",
        {"tender_ikn": ikn, "klasor": klasor or None},
        f"“{(kayit.tender_title or ikn)[:70]}” ihalesini “{hedef[:40]}” klasörüne taşıyayım mı?",
    )


# ── Firmayı takibe al ──────────────────────────────────
def firma_takip_oner(ctx, firma_id=None, alarm=True, **_):
    """
    Yüklenici firmayı takibe almayı önerir (rakip takibi).

    Kimlik `firma_ara`/`firma_profili` sonucundaki `id`'dir; burada veritabanından
    doğrulanır ve ünvan oradan okunur (modelin yazdığı ada güvenilmez).
    """
    from ekap.models import Contractor

    try:
        firma = Contractor.objects.filter(pk=int(firma_id)).only("kanonik_ad").first()
    except (TypeError, ValueError):
        firma = None
    if firma is None:
        return _hata("Geçerli bir firma id'si ver (firma_ara sonucundaki `id`).")
    return _oneri_olustur(
        ctx,
        "firma_takip",
        {"contractor_id": firma.pk, "alarm": bool(alarm)},
        f"“{firma.kanonik_ad[:70]}” firmasını takibe alayım mı?"
        + (" (yeni iş aldığında bildirim gelir)" if alarm else ""),
    )


# ── İdareyi favorilere ekle ────────────────────────────
def idare_favori_oner(ctx, detsis_no=None, alarm=True, **_):
    """İdareyi favorilere eklemeyi önerir. `detsis_no` `idare_ara` sonucundan gelir."""
    from ekap.models import Authority

    detsis_no = str(detsis_no or "").strip()
    if not detsis_no:
        return _hata("detsis_no gerekli (idare_ara sonucundan al).")
    idare = Authority.objects.filter(detsis_no=detsis_no).only("ad").first()
    if idare is None:
        return _hata(f"{detsis_no} DETSIS numarasıyla idare bulunamadı.")
    return _oneri_olustur(
        ctx,
        "idare_favori",
        {"detsis_no": detsis_no, "alarm": bool(alarm)},
        f"“{idare.ad[:70]}” idaresini favorilerinize ekleyeyim mi?"
        + (" (yeni ihale yayınladığında bildirim gelir)" if alarm else ""),
    )


# ── Kaydı sil (geri alma) ──────────────────────────────
_SIL_ETIKET = {
    "ihale": "kayıtlı ihale",
    "alarm": "alarm",
    "filtre": "kayıtlı filtre",
    "firma": "firma takibi",
    "idare": "favori idare",
}


def kayit_sil_oner(ctx, tur=None, anahtar=None, **_):
    """
    Kullanıcının bir kaydını silmeyi önerir ("yok sil onu", "alarmı kaldır").

    ⚠️ Kayıt ÖNCE bulunur: olmayan bir kayıt için "silelim mi?" diye sormak,
    kullanıcıya var olmayan bir şeyin varlığını onaylatmaktır. Bulunamazsa model
    bunu kullanıcıya söyler ve kart hiç çıkmaz.
    """
    from tenders.models import (
        FavoriteAuthority, FavoriteContractor, SavedFilter, SavedTender, TenderAlarm,
    )

    if tur not in _SIL_ETIKET:
        return _hata(f"Geçersiz tür. Seçenekler: {', '.join(_SIL_ETIKET)}.")
    anahtar = str(anahtar or "").strip()
    if not anahtar:
        return _hata("Silinecek kaydın anahtarını ver (İKN, filtre id'si, firma id'si…).")

    u = ctx.user
    ad = None
    if tur == "ihale":
        k = SavedTender.objects.filter(user=u, tender_ikn=anahtar).first()
        ad = (k.tender_title or anahtar) if k else None
    elif tur == "alarm":
        # Alarm `tender_id` (ekap_id) ile tutulur; kullanıcı İKN söyleyebilir.
        k = (TenderAlarm.objects.filter(user=u, tender_id=anahtar).first()
             or TenderAlarm.objects.filter(user=u, tender_ikn=anahtar).first())
        if k:
            anahtar = k.tender_id
            ad = k.tender_title or anahtar
    elif tur == "filtre":
        k = SavedFilter.objects.filter(user=u, pk=anahtar).first() if anahtar.isdigit() else None
        ad = k.name if k else None
    elif tur == "firma":
        k = (FavoriteContractor.objects.filter(user=u, contractor_id=anahtar)
             .select_related("contractor").first() if anahtar.isdigit() else None)
        ad = getattr(k.contractor, "kanonik_ad", anahtar) if k else None
    else:  # idare
        k = FavoriteAuthority.objects.filter(user=u, detsis_no=anahtar).first()
        ad = (k.ad or anahtar) if k else None

    if ad is None:
        return _hata(
            f"Böyle bir {_SIL_ETIKET[tur]} kaydı bulunamadı. "
            "kullanicinin_verisi ile listeleyip doğru anahtarı bul."
        )
    return _oneri_olustur(
        ctx,
        "kayit_sil",
        {"tur": tur, "anahtar": anahtar},
        f"“{str(ad)[:70]}” {_SIL_ETIKET[tur]} kaydını sileyim mi? (Geri alınamaz)",
    )
