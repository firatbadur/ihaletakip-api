"""
tenders Celery görevleri — alarm kontrolü, kayıtlı filtre eşleşmesi, bildirim temizliği.

Bildirim gönderimi `tenders.services.notify` (kayıt + pacing'li push) ve
`tenders.services.templates` (Türkçe metinler) üzerinden yapılır. Kullanıcı başına
**tek özet push** ilkesiyle (kategori bazlı) kullanıcı bombardımana tutulmaz.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("ihaletakip")


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
    değişikliği / ihale sonuçlandı olaylarını tespit eder. Her olay için uygulama-içi
    bildirim satırı yazılır; kullanıcı başına **tek birleşik özet push** atılır.
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

    # Kullanıcı başına olay kovaları → tek özet push.
    per_user = {}

    for alarm in alarms:
        tender = tenders_by_ekap.get(alarm.tender_id) or tenders_by_ikn.get(alarm.tender_ikn)
        if tender is None:
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

            bucket = per_user.setdefault(
                alarm.user_id,
                {"user": alarm.user, "reminder": [], "document": [], "completed": []},
            )
            # Her olay için uygulama-içi satır (push değil).
            builders = {
                "reminder": templates.reminder_day,
                "document": templates.document_change,
                "completed": templates.completed,
            }
            for ev in events:
                title, body = builders[ev](tender)
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
                bucket[ev].append(tender)
        except Exception:
            logger.exception("check_tender_alarms: alarm %s işlenemedi", alarm.pk)
            continue

    # Kullanıcı başına tek özet push.
    pushed = 0
    for uid, b in per_user.items():
        title, body = templates.alarm_summary(
            reminder_count=len(b["reminder"]),
            document_count=len(b["document"]),
            completed_count=len(b["completed"]),
        )
        all_tenders = b["reminder"] + b["document"] + b["completed"]
        data = {"type": Notification.Type.ALARM}
        # Tek olay/ihale varsa doğrudan derin bağlantı.
        if len(all_tenders) == 1:
            t = all_tenders[0]
            data["tenderId"] = t.ekap_id
            data["tenderIkn"] = t.ikn
        ok = notify.push_to_user(
            b["user"],
            title=title,
            body=body,
            data=data,
            idem_key=f"alarm:{uid}:{today.isoformat()}",
        )
        if ok:
            pushed += 1

    logger.info(
        "check_tender_alarms: %s alarm, %s kullanıcıya bildirim, %s push",
        len(alarms), len(per_user), pushed,
    )
    return {"alarms": len(alarms), "users_notified": len(per_user), "pushed": pushed}


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
    Alarmı açık kayıtlı filtreler için, filtreye uyan **yeni** (son kontrolden sonra
    DB'ye giren) açık ihaleleri bulur. Her filtre için uygulama-içi özet satır yazılır;
    kullanıcı başına tek özet push atılır. İlk kontrolde bildirim yok (taban alınır).
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

    per_user = {}  # uid -> {"user":u, "count":int, "rep":Tender|None}
    processed = 0
    seen_by_user = {}  # uid -> set(ikn): bu kullanıcıya daha önce filtre bildirimi gitmiş

    def _seen_ikns(user_id):
        if user_id not in seen_by_user:
            seen_by_user[user_id] = set(
                Notification.objects.filter(
                    user_id=user_id,
                    type=Notification.Type.TENDER,
                    tender_ikn__isnull=False,
                    created_at__gte=now - timedelta(days=30),
                ).values_list("tender_ikn", flat=True)
            )
        return seen_by_user[user_id]

    for sf in SavedFilter.objects.filter(alarm__isnull=False).select_related("user").iterator():
        if not _alarm_enabled(sf.alarm):
            continue
        # Filtre alarmı Pro'ya özeldir → Pro iken alarmlı filtre kurup Free'ye düşen
        # kullanıcıya bildirim gitmesin (is_premium property → Python'da eleriz).
        if not sf.user.is_premium:
            continue
        processed += 1
        try:
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

            sf.last_notified_at = now
            sf.save(update_fields=["last_notified_at"])

            # Bu kullanıcıya daha önce bildirilmemiş (yeni yayınlanan) ihaleler.
            seen = _seen_ikns(sf.user_id)
            new_list = [
                t for t in base.order_by("-ilan_tarihi")[:50]
                if t.ikn and t.ikn not in seen
            ][:20]
            if not new_list:
                continue
            for t in new_list:
                seen.add(t.ikn)  # aynı çalıştırmada başka filtreden tekrar bildirme

            first = new_list[0]
            title, body = templates.saved_filter_match(
                filter_name=sf.name,
                count=len(new_list),
                first_title=first.ihale_adi,
            )
            # Bu bir **filtre eşleşmesi** bildirimidir → bildirime basınca tek bir ihale
            # DEĞİL, bu filtrenin sonuç listesi açılmalı. Bu yüzden tender_id/tender_ikn
            # DOLDURULMAZ (aksi halde mobil yanlışlıkla tek ihaleye giderdi); yerine
            # `filter_id=sf.id` verilir → mobil filtreyi yükleyip uygular.
            notify.record_notification(
                sf.user,
                type=Notification.Type.TENDER,
                title=title,
                body=body,
                filter_id=sf.id,
            )

            bucket = per_user.setdefault(
                sf.user_id,
                {"user": sf.user, "count": 0, "rep": None, "filter_id": None, "filter_name": None},
            )
            bucket["count"] += len(new_list)
            if bucket["rep"] is None:
                bucket["rep"] = first  # push özet metni için temsili ihale başlığı
            if bucket["filter_id"] is None:
                # Push derin bağlantısı için temsili filtre (birden çok filtre eşleşirse ilki).
                bucket["filter_id"] = sf.id
                bucket["filter_name"] = sf.name
        except Exception:
            logger.exception("check_saved_filter_matches: filtre %s işlenemedi", sf.pk)
            continue

    # Kullanıcı başına tek özet push.
    pushed = 0
    for uid, b in per_user.items():
        count = b["count"]
        rep = b["rep"]
        if count == 1 and rep is not None:
            title, body = templates.saved_filter_match(
                filter_name="Kayıtlı Filtreleriniz", count=1, first_title=rep.ihale_adi
            )
        else:
            title = "Kayıtlı Filtreleriniz"
            body = f"Filtrelerinize uygun {count} yeni ihale"
        data = {"type": Notification.Type.TENDER}
        # Bildirime basınca tek ihaleye DEĞİL, temsili filtrenin sonuçlarına gitsin.
        if b["filter_id"] is not None:
            data["filterId"] = b["filter_id"]
        ok = notify.push_to_user(
            b["user"],
            title=title,
            body=body,
            data=data,
            idem_key=f"filter:{uid}:{today.isoformat()}",
        )
        if ok:
            pushed += 1

    logger.info(
        "check_saved_filter_matches: %s filtre, %s kullanıcıya bildirim, %s push",
        processed, len(per_user), pushed,
    )
    return {"filters": processed, "users_notified": len(per_user), "pushed": pushed}


@shared_task(name="tenders.tasks.check_favorite_authority_matches")
def check_favorite_authority_matches():
    """
    Favori idareler (alarm açık) için, o idarenin **yeni yayınladığı** açık ihaleleri bulur
    ve kullanıcıya bildirir. Kayıtlı filtre eşleşmesiyle aynı desendedir; fark: filtre yerine
    idare, seçilen `detsis_no` tüm alt birimlerin `idare_id`'lerine genişletilir
    (`descendant_idare_ids`, ihale/tarama uçlarıyla ortak).

    Her yeni ihale için uygulama-içi satır yazılır (type=TENDER; **`authority_detsis` dolu** →
    tıklanınca tek ihale DEĞİL, o idarenin ihale listesi `GET /ekap/tenders/?idare_detsis=`
    açılır). `tender_ikn` de yazılır ama yalnızca **dedup** için (mobil authority_detsis'i
    önceler). Kullanıcı başına tek özet push atılır. **Favori idare alarmı Pro'ya özeldir.**
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

    per_user = {}  # uid -> {"user","count","rep","authority_detsis","authority_ad","seen"}
    processed = 0

    for fav in FavoriteAuthority.objects.filter(alarm=True).select_related("user").iterator():
        # Favori idare alarmı Pro'ya özeldir → Free üyeye bildirim yok.
        if not fav.user.is_premium:
            continue
        processed += 1
        try:
            # detsis_no → tüm alt birimlerin idare_id'leri (ihalede gerçekten geçenlerle kesiş).
            expanded = descendant_idare_ids([fav.detsis_no])
            if expanded:
                expanded &= _tender_idare_id_set()
            if not expanded:
                continue
            base = (
                Tender.objects.filter(idare_id__in=expanded, ihale_durum__in=OPEN_STATUSES)
                .filter(Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True))
                .filter(ilan_tarihi__gte=window_start)
                .order_by("-ilan_tarihi")
            )

            bucket = per_user.setdefault(
                fav.user_id,
                {"user": fav.user, "count": 0, "rep": None,
                 "authority_detsis": None, "authority_ad": None, "seen": None},
            )
            # Bu kullanıcıya daha önce (son 30 gün) bildirilmiş İKN'ler → tekrar bildirme.
            # type=TENDER + tender_ikn dolu bildirimler; aynı ihale filtre/idare kanalından
            # ikinci kez gitmez. (Kullanıcı başına bir kez sorgula, kova içinde önbelleğe al.)
            if bucket["seen"] is None:
                bucket["seen"] = set(
                    Notification.objects.filter(
                        user_id=fav.user_id,
                        type=Notification.Type.TENDER,
                        tender_ikn__isnull=False,
                        created_at__gte=now - timedelta(days=30),
                    ).values_list("tender_ikn", flat=True)
                )
            seen = bucket["seen"]

            new_list = [t for t in base[:50] if t.ikn and t.ikn not in seen][:20]
            if not new_list:
                continue

            fav.last_notified_at = now
            fav.save(update_fields=["last_notified_at"])

            for t in new_list:
                seen.add(t.ikn)  # aynı çalıştırmada başka favori idareden tekrar bildirme
                title, body = templates.authority_match(
                    authority_name=fav.ad or "Favori İdare", count=1, first_title=t.ihale_adi,
                )
                notify.record_notification(
                    fav.user,
                    type=Notification.Type.TENDER,
                    title=title,
                    body=body,
                    tender_ikn=t.ikn,           # yalnızca dedup için
                    tender_title=t.ihale_adi,
                    institution=fav.ad or None,
                    authority_detsis=fav.detsis_no,  # tıklanınca idare listesi
                )

            bucket["count"] += len(new_list)
            if bucket["rep"] is None:
                bucket["rep"] = new_list[0]
            if bucket["authority_detsis"] is None:
                bucket["authority_detsis"] = fav.detsis_no
                bucket["authority_ad"] = fav.ad
        except Exception:
            logger.exception("check_favorite_authority_matches: favori %s işlenemedi", fav.pk)
            continue

    # Kullanıcı başına tek özet push (idare listesine derin bağlantı).
    pushed = 0
    notified_users = 0
    for uid, b in per_user.items():
        count = b["count"]
        if count <= 0:
            continue
        notified_users += 1
        rep = b["rep"]
        if count == 1 and rep is not None:
            title, body = templates.authority_match(
                authority_name=b["authority_ad"] or "Favori İdare", count=1, first_title=rep.ihale_adi,
            )
        else:
            title = "Favori İdareleriniz"
            body = f"Favori idarelerinizden {count} yeni ihale"
        data = {"type": Notification.Type.TENDER}
        # Bildirime basınca tek ihaleye DEĞİL, temsili idarenin ihale listesine gitsin.
        if b["authority_detsis"]:
            data["authorityDetsis"] = b["authority_detsis"]
        ok = notify.push_to_user(
            b["user"], title=title, body=body, data=data,
            idem_key=f"authority:{uid}:{today.isoformat()}",
        )
        if ok:
            pushed += 1

    logger.info(
        "check_favorite_authority_matches: %s favori, %s kullanıcıya bildirim, %s push",
        processed, notified_users, pushed,
    )
    return {"favorites": processed, "users_notified": notified_users, "pushed": pushed}


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
