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

from ..models import SavedFilter, SavedTender, TenderAlarm


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
