"""
Kullanıcı verisine YAZAN işlemler — tek kaynak.

İki çağıran var: mevcut REST view'ları (`tenders/views.py`) ve İhale Asistanı'nın
onay kartı ucu (`assistant/views.py::AssistantActionExecuteView`). Mantık iki yere
kopyalanırsa zamanla ayrışır: view'daki premium kuralı sıkılaşır, asistanınki eski
kalır ve asistan üzerinden Pro özelliği bedavaya açılır.

⚠️ `require_premium` BURADA çağrılır ve bir DRF `APIException`'dır (403). Bu güvenlidir
çünkü her iki çağıran da bir HTTP isteğinin içindedir. **Celery görevinden çağırmayın** —
orada exception 403'e dönüşmez, görevi çökertir. Asistanın araçları bu fonksiyonları
ÇAĞIRMAZ; yalnızca kullanıcının onaylayacağı bir öneri üretir.
"""
from accounts.premium import MSG_ALARM, MSG_FILTER_ALARM, require_premium

from ..models import (
    FavoriteAuthority,
    FavoriteContractor,
    SavedFilter,
    SavedTender,
    TenderAlarm,
)


def ihale_kaydet(user, *, tender_ikn, **alanlar):
    """
    İhaleyi kullanıcının kayıtlılarına ekler (upsert).

    Premium GEREKMEZ — kaydetme Free dahil herkese ve sınırsızdır.
    Aynı İKN tekrar gelirse günceller; `group` gönderilmezse mevcut klasör korunur
    (yeni kayıtta null = "Genel").
    """
    kayit, olusturuldu = SavedTender.objects.update_or_create(
        user=user, tender_ikn=tender_ikn, defaults={"tender_ikn": tender_ikn, **alanlar}
    )
    return kayit, olusturuldu


def alarm_kur(user, *, tender_id, **alanlar):
    """
    İhale alarmı kurar/günceller. **Pro'ya özeldir.**

    (Kurma/güncelleme kilitli; listeleme ve silme serbesttir — mevcut view davranışı.)
    """
    require_premium(user, MSG_ALARM)
    alarm, olusturuldu = TenderAlarm.objects.update_or_create(
        user=user, tender_id=tender_id, defaults={"tender_id": tender_id, **alanlar}
    )
    return alarm, olusturuldu


def filtre_alarm_izni(user, alarm):
    """
    Filtre kaydetme serbesttir; ancak ALARM açıksa Pro gerekir (yeni ihale bildirimi).

    Ayrı fonksiyon: `SavedFilterListCreateView` nesneyi `serializer.save()` ile yaratır
    (DRF yanıtı için `serializer.instance` gerekir), yalnızca KURALI paylaşır.
    """
    from ..tasks import _alarm_enabled

    if _alarm_enabled(alarm):
        require_premium(user, MSG_FILTER_ALARM)


def filtre_kaydet(user, *, name, filters, alarm=None, tags=None):
    """Kayıtlı filtre oluşturur. Alarm açıksa Pro gerekir."""
    filtre_alarm_izni(user, alarm)
    return SavedFilter.objects.create(
        user=user, name=name, filters=filters or {}, alarm=alarm, tags=tags or []
    )


# ══════════════════════════════════════════════════════════════════════════════
# Faz 3 — asistanın önerebildiği diğer eylemler
#
# Hepsi Free dahil herkese açıktır (bildirim gönderimi Pro'dur, kaydın kendisi
# değil — favori idare/firma asimetrisi). Bu yüzden burada `require_premium`
# YOKTUR; asistan ucu zaten `MSG_CHAT` ile Pro'ya kilitli.
# ══════════════════════════════════════════════════════════════════════════════


def enrich_authority(detsis_no):
    """`detsis_no`'dan `ekap.Authority` bulup ad/idare_id/has_items döndürür (yoksa boş)."""
    from ekap.models import Authority

    a = Authority.objects.filter(detsis_no=detsis_no).first()
    if not a:
        return {}
    return {"ad": a.ad, "idare_id": a.idare_id or None, "has_items": a.has_items}


def idare_favori_ekle(user, *, detsis_no, alarm=True):
    """İdareyi favorilere ekler (upsert). Ad/idare_id sunucuda zenginleştirilir."""
    defaults = enrich_authority(detsis_no)
    defaults["alarm"] = bool(alarm)
    return FavoriteAuthority.objects.update_or_create(
        user=user, detsis_no=str(detsis_no), defaults=defaults
    )


def firma_takip_et(user, *, contractor_id, alarm=True):
    """Yüklenici firmayı takibe alır (upsert)."""
    from ekap.models import Contractor

    firma = Contractor.objects.filter(pk=contractor_id).first()
    if firma is None:
        raise ValueError("Firma bulunamadı.")
    return FavoriteContractor.objects.update_or_create(
        user=user, contractor=firma, defaults={"alarm": bool(alarm)}
    )


def klasor_bul_veya_olustur(user, name):
    """
    Adı verilen klasörü bulur; yoksa oluşturur.

    ⚠️ Kurallar `TenderGroupSerializer.validate` ile AYNI olmak zorunda (Türkçe
    duyarsız benzersizlik, "Genel" rezerve, kullanıcı başına 20 klasör). Serializer
    doğrudan kullanılamıyor: `context["request"]` istiyor ve asistan yolu bir
    Celery görevinden de çağrılabiliyor. Kural değişirse İKİ yer de güncellenmeli.
    """
    from ..models import DEFAULT_TENDER_GROUP_NAME, MAX_TENDER_GROUPS, TenderGroup
    from ..serializers import _tr_fold

    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("Klasör adı boş olamaz.")
    if len(name) > 60:
        name = name[:60]
    folded = _tr_fold(name)
    if folded == _tr_fold(DEFAULT_TENDER_GROUP_NAME):
        return None  # "Genel" bir satır değildir: group=None demektir
    mevcut = TenderGroup.objects.filter(user=user).only("name")
    for g in mevcut:
        if _tr_fold(g.name) == folded:
            return g
    if len(mevcut) >= MAX_TENDER_GROUPS:
        raise ValueError(f"En fazla {MAX_TENDER_GROUPS} klasör oluşturabilirsiniz.")
    return TenderGroup.objects.create(user=user, name=name)


def ihale_tasi(user, *, tender_ikn, klasor=None):
    """Kayıtlı ihaleyi başka klasöre taşır. `klasor` boşsa "Genel"e (group=None) döner."""
    kayit = SavedTender.objects.filter(user=user, tender_ikn=tender_ikn).first()
    if kayit is None:
        raise ValueError("Bu ihale kayıtlarınızda yok.")
    kayit.group = klasor_bul_veya_olustur(user, klasor) if klasor else None
    kayit.save(update_fields=["group"])
    return kayit


# Silme eşlemesi: tür → (model, arama alanı). Asistan yalnızca bu beş türü önerebilir.
_SILME = {
    "ihale": (SavedTender, "tender_ikn"),
    "alarm": (TenderAlarm, "tender_id"),
    "filtre": (SavedFilter, "pk"),
    "firma": (FavoriteContractor, "contractor_id"),
    "idare": (FavoriteAuthority, "detsis_no"),
}


def kaydi_sil(user, *, tur, anahtar):
    """
    Kullanıcının bir kaydını siler.

    ⚠️ Silme GERİ ALINAMAZ; bu yüzden asistan yolunda daima onay kartından geçer
    (`assistant/tools/write.py::kayit_sil_oner`). Kayıt zaten yoksa hata DEĞİL
    `(0, ...)` döner — kullanıcı iki kez onaylarsa ikincisi sessizce geçmeli.
    """
    esleme = _SILME.get(tur)
    if not esleme:
        raise ValueError(f"Bilinmeyen kayıt türü: {tur}")
    model, alan = esleme
    silinen, _ = model.objects.filter(user=user, **{alan: anahtar}).delete()
    return silinen
