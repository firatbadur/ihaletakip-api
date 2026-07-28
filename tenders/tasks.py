"""
tenders Celery görevleri — alarm kontrolü, kayıtlı filtre eşleşmesi, bildirim temizliği.

Bildirim gönderimi `tenders.services.notify` (kayıt + pacing'li push) ve
`tenders.services.templates` (Türkçe metinler) üzerinden yapılır. Bildirimler
**abonelik-başına AYRI**dır: her kayıtlı filtre / favori idare / ihale alarmı için ayrı
uygulama-içi satır + ayrı push atılır (kullanıcı başına birleşik özet DEĞİL). OKAS önerisi
istisnadır (kullanıcı başına tek özet). Çoğalmayı önlemek için her abonelik **atomik
gün-kilidiyle** (Redis `cache.add`) günde bir kez işlenir; görev yinelenmiş/interval beat ile
çok kez tetiklense bile satır/push tekrar üretilmez.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

# Abonelik-başına gün-kilidi TTL'i (Redis). Anahtarda tarih olduğundan günün bitmesini
# aşması yeter; ~1.5 gün.
_ROW_DEDUP_TTL = 36 * 3600


def _local_date(dt):
    """Aware/naive datetime → yerel tarih (None güvenli)."""
    if dt is None:
        return None
    if timezone.is_naive(dt):
        return dt.date()
    return timezone.localtime(dt).date()


def _detect_alarm_events(alarm, tender, today, settled_set):
    """
    Alarm aboneliklerine göre tetiklenen olayları döner: {"reminder", "document", "completed"}.
    İlk görüşte (snapshot null) değişim olayları tetiklenmez (yalnızca taban alınır).
    """
    events = set()

    # İhale günü — snapshot gerektirmez, her koşulda bugün mü diye bakılır.
    if alarm.reminder_day and _local_date(tender.ihale_tarihi) == today:
        events.add("reminder")

    # Doküman değişikliği — önceki ve şimdiki sayı biliniyorken fark varsa.
    if alarm.document_change:
        prev = alarm.last_dokuman_sayisi
        cur = tender.dokuman_sayisi
        if prev is not None and cur is not None and cur != prev:
            events.add("document")

    # Tamamlandı — sonuçlanmamış durumdan sonuçlanmış duruma GEÇİŞ; tek sefer.
    if alarm.completed and not alarm.completed_notified:
        prev_done = alarm.last_ihale_durum in settled_set if alarm.last_ihale_durum is not None else False
        cur_done = tender.ihale_durum in settled_set if tender.ihale_durum is not None else False
        if cur_done and not prev_done:
            events.add("completed")

    return events


@shared_task(name="tenders.tasks.check_tender_alarms")
def check_tender_alarms():
    """
    Aktif ihale alarmlarını `ekap.Tender` verisiyle karşılaştırır; ihale günü / doküman
    değişikliği / ihale sonuçlandı olaylarını tespit eder. **Her ihale için AYRI** uygulama-içi
    satır + push atılır (kullanıcı başına birleşik özet DEĞİL); tıklanınca o ihalenin detayı
    açılır. Alarm başına atomik gün-kilidi ile aynı gün yinelenmiş tetikte çoğalma olmaz.
    **İhale alarmları Pro'ya özeldir.**
    """
    from ekap.constants import DURUM_SONUCLANMIS
    from ekap.models import Tender

    from .models import Notification, TenderAlarm
    from .services import notify, templates

    today = timezone.localdate()

    # Abonelikten en az biri açık olan alarmlar (completed için henüz bildirilmemiş).
    alarms = list(
        TenderAlarm.objects.filter(
            Q(reminder_day=True)
            | Q(document_change=True)
            | Q(completed=True, completed_notified=False)
        ).select_related("user")
    )
    # İhale alarmları Pro'ya özeldir → Pro iken alarm kurup Free'ye düşen kullanıcıya
    # bildirim gitmesin (is_premium DB alanı değil, property → Python'da eleriz).
    alarms = [a for a in alarms if a.user.is_premium]
    if not alarms:
        logger.info("check_tender_alarms: aktif alarm yok")
        return {"alarms": 0, "pushed": 0}

    # tender_id (=ekap_id) ve fallback ikn → Tender (tek sefer çöz).
    ekap_ids = {a.tender_id for a in alarms if a.tender_id}
    ikns = {a.tender_ikn for a in alarms if a.tender_ikn}
    tenders_by_ekap = {t.ekap_id: t for t in Tender.objects.filter(ekap_id__in=ekap_ids)} if ekap_ids else {}
    tenders_by_ikn = {t.ikn: t for t in Tender.objects.filter(ikn__in=ikns)} if ikns else {}

    notified = 0
    pushed = 0
    for alarm in alarms:
        tender = tenders_by_ekap.get(alarm.tender_id) or tenders_by_ikn.get(alarm.tender_ikn)
        if tender is None:
            continue
        # Alarm başına gün-kilidi: aynı gün yinelenmiş tetikte tekrar işleme (satır/push çoğalmaz).
        if not cache.add(f"alarm:{alarm.user_id}:{tender.ekap_id}:{today.isoformat()}", 1, _ROW_DEDUP_TTL):
            continue
        try:
            events = _detect_alarm_events(alarm, tender, today, DURUM_SONUCLANMIS)

            # Snapshot/guard'ı her koşulda güncelle (sonraki fark tespiti için taban).
            alarm.last_dokuman_sayisi = tender.dokuman_sayisi
            alarm.last_ihale_durum = tender.ihale_durum
            update_fields = ["last_dokuman_sayisi", "last_ihale_durum"]
            if "completed" in events:
                alarm.completed_notified = True
                update_fields.append("completed_notified")
            alarm.save(update_fields=update_fields)

            if not events:
                continue

            # Bu ihaleye ait tüm olayları tek bildirimde birleştir → ihale başına tek push.
            title, body = templates.alarm_tender(tender, events)
            notify.record_notification(
                alarm.user,
                type=Notification.Type.ALARM,
                title=title,
                body=body,
                tender_id=tender.ekap_id,
                tender_ikn=tender.ikn,
                tender_title=tender.ihale_adi,
                institution=tender.idare_adi,
            )
            notified += 1
            ok = notify.push_to_user(
                alarm.user,
                title=title,
                body=body,
                data={
                    "type": Notification.Type.ALARM,
                    "tenderId": tender.ekap_id,
                    "tenderIkn": tender.ikn,
                },
                idem_key=None,  # gün-kilidi zaten tekilliği garanti eder
            )
            if ok:
                pushed += 1
        except Exception:
            logger.exception("check_tender_alarms: alarm %s işlenemedi", alarm.pk)
            continue

    logger.info(
        "check_tender_alarms: %s alarm, %s bildirim, %s push", len(alarms), notified, pushed,
    )
    return {"alarms": len(alarms), "notified": notified, "pushed": pushed}


def _alarm_enabled(alarm) -> bool:
    """SavedFilter.alarm (JSONField) alanı bildirim için açık mı?"""
    if alarm is None:
        return False
    if isinstance(alarm, bool):
        return alarm
    if isinstance(alarm, dict):
        if not alarm:
            return False
        for key in ("enabled", "active", "push", "on"):
            if key in alarm:
                return bool(alarm[key])
        return True  # boş olmayan dict → açık kabul et
    return bool(alarm)


@shared_task(name="tenders.tasks.check_saved_filter_matches")
def check_saved_filter_matches():
    """
    Alarmı açık kayıtlı filtreler için, filtreye uyan yeni ihaleleri bulur. **Her filtre için
    AYRI** uygulama-içi satır + push atılır (kullanıcı başına birleşik özet DEĞİL): 10 filtreden
    8'i eşleşirse 8 ayrı bildirim gider. Başlık = filtre adı, gövde = "{filtre} filtrenize uygun
    N adet ihale bulundu."; tıklanınca tek ihale DEĞİL, `filter_id` ile o filtrenin sonuç listesi
    açılır. Filtre başına atomik gün-kilidi (aynı gün çoğalma yok) + `last_notified_at` watermark
    (dün bildirilen ihale bugün tekrar bildirilmez). **Filtre alarmı Pro'ya özeldir.**
    """
    from ekap.models import Tender
    from ekap.views import apply_tender_filters

    from .models import Notification, SavedFilter
    from .services import notify, templates

    OPEN_STATUSES = [2, 3]
    now = timezone.now()
    today = timezone.localdate()
    # Yalnızca son `publish_days` gün içinde YAYINLANAN (ilan_tarihi) ihaleler bildirilir →
    # eski/backfill ihaleler bildirilmez. ilan_tarihi detay senkronundan dolar.
    publish_days = int(getattr(settings, "NOTIF_FILTER_PUBLISH_DAYS", 2))
    window_start = now - timedelta(days=publish_days)

    processed = 0
    notified = 0
    pushed = 0

    for sf in SavedFilter.objects.filter(alarm__isnull=False).select_related("user").iterator():
        if not _alarm_enabled(sf.alarm):
            continue
        # Filtre alarmı Pro'ya özeldir → Pro iken alarmlı filtre kurup Free'ye düşen
        # kullanıcıya bildirim gitmesin (is_premium property → Python'da eleriz).
        if not sf.user.is_premium:
            continue
        # Filtre başına atomik gün-kilidi: bu filtre bugün zaten işlendiyse (yinelenmiş/interval
        # beat) atla → satır/push çoğalmaz (race-safe).
        if not cache.add(f"filter:{sf.user_id}:{sf.id}:{today.isoformat()}", 1, _ROW_DEDUP_TTL):
            continue
        processed += 1
        try:
            prev_watermark = sf.last_notified_at
            filt = sf.filters or {}
            # Filtrenin kendi kriterleri (ihale_adi, ihale_tip, il_id...) apply_tender_filters
            # ile uygulanır (parametre adları = Tender model alan adları).
            base = apply_tender_filters(Tender.objects.all(), filt)
            # Filtre kendi durumunu belirtmediyse yalnızca katılıma açık ihaleleri öner.
            if not filt.get("ihale_durum"):
                base = base.filter(ihale_durum__in=OPEN_STATUSES)
            # Teklifi geçmemiş (biddable) + son `publish_days` günde yayınlanmış.
            base = base.filter(Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True))
            base = base.filter(ilan_tarihi__gte=window_start)
            # Cross-day dedup: yalnızca son kontrolden (watermark) sonra yayınlananlar.
            if prev_watermark:
                base = base.filter(ilan_tarihi__gt=prev_watermark)

            sf.last_notified_at = now
            sf.save(update_fields=["last_notified_at"])

            new_list = list(base.order_by("-ilan_tarihi")[:20])
            if not new_list:
                continue

            title, body = templates.saved_filter_match(filter_name=sf.name, count=len(new_list))
            notify.record_notification(
                sf.user,
                type=Notification.Type.TENDER,
                title=title,
                body=body,
                filter_id=sf.id,
            )
            notified += 1
            ok = notify.push_to_user(
                sf.user,
                title=title,
                body=body,
                data={"type": Notification.Type.TENDER, "filterId": sf.id},
                idem_key=None,  # gün-kilidi zaten tekilliği garanti eder
            )
            if ok:
                pushed += 1
        except Exception:
            logger.exception("check_saved_filter_matches: filtre %s işlenemedi", sf.pk)
            continue

    logger.info(
        "check_saved_filter_matches: %s filtre, %s bildirim, %s push", processed, notified, pushed,
    )
    return {"filters": processed, "notified": notified, "pushed": pushed}


@shared_task(name="tenders.tasks.check_favorite_authority_matches")
def check_favorite_authority_matches():
    """
    Favori idareler (alarm açık) için, o idarenin yeni yayınladığı açık ihaleleri bulur.
    **Her favori idare için AYRI** uygulama-içi satır + push atılır (kullanıcı başına birleşik
    özet DEĞİL). Başlık = idare adı; tıklanınca tek ihale DEĞİL, `authority_detsis` ile o
    idarenin ihale listesi (`GET /ekap/tenders/?idare_detsis=`) açılır. Seçilen `detsis_no`
    `descendant_idare_ids` ile alt birim `idare_id`'lerine genişletilir. İdare başına atomik
    gün-kilidi + `last_notified_at` watermark. **Favori idare alarmı Pro'ya özeldir.**
    """
    from ekap.detsis_tree import descendant_idare_ids
    from ekap.models import Tender
    from ekap.views import _tender_idare_id_set

    from .models import FavoriteAuthority, Notification
    from .services import notify, templates

    OPEN_STATUSES = [2, 3]
    now = timezone.now()
    today = timezone.localdate()
    # Yalnızca son `publish_days` günde YAYINLANAN (ilan_tarihi) ihaleler bildirilir →
    # eski/backfill ihaleler bildirilmez (kayıtlı filtre görevi ile aynı pencere).
    publish_days = int(getattr(settings, "NOTIF_FILTER_PUBLISH_DAYS", 2))
    window_start = now - timedelta(days=publish_days)

    processed = 0
    notified = 0
    pushed = 0

    for fav in FavoriteAuthority.objects.filter(alarm=True).select_related("user").iterator():
        # Favori idare alarmı Pro'ya özeldir → Free üyeye bildirim yok.
        if not fav.user.is_premium:
            continue
        # İdare başına atomik gün-kilidi (yinelenmiş/interval beat'e karşı; satır/push çoğalmaz).
        if not cache.add(f"authority:{fav.user_id}:{fav.detsis_no}:{today.isoformat()}", 1, _ROW_DEDUP_TTL):
            continue
        processed += 1
        try:
            # detsis_no → tüm alt birimlerin idare_id'leri (ihalede gerçekten geçenlerle kesiş).
            expanded = descendant_idare_ids([fav.detsis_no])
            if expanded:
                expanded &= _tender_idare_id_set()
            if not expanded:
                continue
            prev_watermark = fav.last_notified_at
            base = (
                Tender.objects.filter(idare_id__in=expanded, ihale_durum__in=OPEN_STATUSES)
                .filter(Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True))
                .filter(ilan_tarihi__gte=window_start)
            )
            # Cross-day dedup: yalnızca son kontrolden (watermark) sonra yayınlananlar.
            if prev_watermark:
                base = base.filter(ilan_tarihi__gt=prev_watermark)
            base = base.order_by("-ilan_tarihi")

            fav.last_notified_at = now
            fav.save(update_fields=["last_notified_at"])

            new_list = list(base[:20])
            if not new_list:
                continue

            title, body = templates.authority_match(
                authority_name=fav.ad or "Favori İdare",
                count=len(new_list),
                first_title=new_list[0].ihale_adi if len(new_list) == 1 else None,
            )
            notify.record_notification(
                fav.user,
                type=Notification.Type.TENDER,
                title=title,
                body=body,
                institution=fav.ad or None,
                authority_detsis=fav.detsis_no,  # tıklanınca idare listesi
            )
            notified += 1
            ok = notify.push_to_user(
                fav.user,
                title=title,
                body=body,
                data={"type": Notification.Type.TENDER, "authorityDetsis": fav.detsis_no},
                idem_key=None,  # gün-kilidi zaten tekilliği garanti eder
            )
            if ok:
                pushed += 1
        except Exception:
            logger.exception("check_favorite_authority_matches: favori %s işlenemedi", fav.pk)
            continue

    logger.info(
        "check_favorite_authority_matches: %s favori, %s bildirim, %s push", processed, notified, pushed,
    )
    return {"favorites": processed, "notified": notified, "pushed": pushed}


@shared_task(name="tenders.tasks.recommend_by_saved_okas")
def recommend_by_saved_okas():
    """
    Kayıtlı ihalelere göre günlük OKAS önerisi — **Free/Pro fark etmeksizin HERKESE**.

    Kullanıcının kaydettiği ihalelerin (`SavedTender`) OKAS kodlarını toplar; o kodlarla
    **son 24 saatte yayınlanan** (ilan_tarihi) açık + teklifi geçmemiş ihaleleri bulur.
    Kullanıcının zaten kaydettiği ihaleler hariç tutulur. Eşleşme varsa kullanıcı başına
    tek özet bildirim + push atılır; push/bildirim OKAS kodlarını `okas_kodlar` (CSV) ile
    taşır → mobil bildirime basınca tek ihale DEĞİL, `GET /ekap/tenders/?okas_kod=<CSV>`
    ile o kategorilerdeki arama sonuçlarını açar.

    Not: Bu bildirim **premium değildir** (herkese) — İhale Asistanı önerisinden (Pro,
    profil tabanlı) ayrıdır. Pencere `NOTIF_OKAS_PUBLISH_DAYS` (vars. 1 gün) ile ayarlanır.
    """
    from django.contrib.auth import get_user_model

    from ekap.models import OkasItem, Tender

    from .models import Notification, SavedTender
    from .services import notify, templates

    OPEN_STATUSES = [2, 3]
    MAX_OKAS = 20  # push/arama derin bağlantısını makul tut
    now = timezone.now()
    today = timezone.localdate()
    publish_days = int(getattr(settings, "NOTIF_OKAS_PUBLISH_DAYS", 1))
    window_start = now - timedelta(days=publish_days)

    User = get_user_model()
    user_ids = list(
        SavedTender.objects.values_list("user_id", flat=True).distinct()
    )
    if not user_ids:
        logger.info("recommend_by_saved_okas: kayıtlı ihalesi olan kullanıcı yok")
        return {"users": 0, "pushed": 0}

    users = {u.id: u for u in User.objects.filter(id__in=user_ids)}
    pushed = 0
    notified_users = 0

    for uid in user_ids:
        user = users.get(uid)
        if user is None or not getattr(user, "is_active", True):
            continue
        # Kullanıcı başına atomik gün-kilidi: OKAS önerisi tek özettir; görev gün içinde
        # birden çok tetiklenirse (yinelenmiş/interval beat) satır/push çoğalmasın.
        if not cache.add(f"okasrec:{uid}:{today.isoformat()}", 1, _ROW_DEDUP_TTL):
            continue
        try:
            saved_ikns = list(
                SavedTender.objects.filter(user_id=uid)
                .exclude(tender_ikn="")
                .values_list("tender_ikn", flat=True)
            )
            if not saved_ikns:
                continue
            # Kayıtlı ihalelerin OKAS kodları (benzersiz, boş olmayan; azami MAX_OKAS).
            okas_codes = list(
                OkasItem.objects.filter(tender__ikn__in=saved_ikns)
                .exclude(kodu="")
                .values_list("kodu", flat=True)
                .distinct()
            )[:MAX_OKAS]
            if not okas_codes:
                continue

            # Aynı OKAS kodlarıyla son 24 saatte yayınlanan açık + teklifi geçmemiş ihaleler
            # (kullanıcının zaten kaydettikleri hariç).
            matches = (
                Tender.objects.filter(
                    okas_kalemleri__kodu__in=okas_codes,
                    ihale_durum__in=OPEN_STATUSES,
                )
                .filter(Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True))
                .filter(ilan_tarihi__gte=window_start)
                .exclude(ikn__in=saved_ikns)
                .distinct()
            )
            count = matches.count()
            if count <= 0:
                continue

            okas_csv = ",".join(okas_codes)
            title, body = templates.okas_recommendation(count=count)
            notify.record_notification(
                user,
                type=Notification.Type.TENDER,
                title=title,
                body=body,
                okas_kodlar=okas_csv,
            )
            notified_users += 1

            data = {"type": Notification.Type.TENDER, "okasKodlar": okas_csv}
            ok = notify.push_to_user(
                user, title=title, body=body, data=data,
                idem_key=f"okas:{uid}:{today.isoformat()}",
            )
            if ok:
                pushed += 1
        except Exception:
            logger.exception("recommend_by_saved_okas: kullanıcı %s işlenemedi", uid)
            continue

    logger.info(
        "recommend_by_saved_okas: %s kullanıcıya bildirim, %s push", notified_users, pushed,
    )
    return {"users": notified_users, "pushed": pushed}


@shared_task(name="tenders.tasks.cleanup_old_notifications")
def cleanup_old_notifications(days: int = 30):
    """Belirtilen günden eski OKUNMUŞ bildirimleri siler."""
    from .models import Notification

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = Notification.objects.filter(
        read=True, created_at__lt=cutoff
    ).delete()
    logger.info("cleanup_old_notifications: %s bildirim silindi", deleted)
    return {"deleted": deleted}
