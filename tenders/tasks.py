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

from ekap.utils import local_day_range

logger = logging.getLogger("ihaletakip")

# Abonelik-başına gün-kilidi TTL'i (Redis). Anahtarda tarih olduğundan günün bitmesini
# aşması yeter; ~1.5 gün.
_ROW_DEDUP_TTL = 36 * 3600

# ⚠️ **Bildirim penceresi neden "bugün" DEĞİL, dedup neden zamana bağlı DEĞİL.**
# Görevler eskiden `ilan_tarihi` BUGÜN olanları arıyor ve filtre/idare başına bir
# **gün-kilidi** ile günde tek tura zorlanıyordu. İkisi birlikte iki arıza üretti:
#   1. EKAP'ın yayım saati **bilinmiyor ve veriden okunamıyor** (`ilan_tarihi`
#      damgası gün başıdır). Sabit saatte tek tur, o saatten sonra yayımlanan her
#      ihaleyi kaçırıyordu — ertesi gün de "bugün" olmadıkları için hiç bildirilmiyorlardı.
#   2. "Son bildirimden beri" (`last_notified_at`) tabanlı bir pencere de ÇÖZMEZ:
#      öğlen DB'ye giren ihale de `00:00` damgası taşır, yani sabahki watermark'ın
#      **altında** kalır ve yine kaçardı.
# Çözüm: pencereyi genişlet (son `NOTIF_LOOKBACK_DAYS` gün) ve mükerrerliği zamana
# değil **ihaleye** bağla — abonelik başına "bu ihaleyi bildirdim" işareti.
# ⚠️ İşaret Redis'te (cache) durur; Redis sıfırlanırsa nadiren mükerrer bildirim
# gidebilir. Bilinçli tercih: **kaçan bildirim, mükerrer bildirimden kötüdür.**
_TENDER_DEDUP_TTL = 7 * 24 * 3600
# Tur kilidi: yalnızca **eşzamanlı** tetiklemeye karşı (yinelenmiş beat girdisi,
# elle tetikleme). Beat aralığından KISA olmalı, yoksa sonraki turu da yutar.
_TUR_KILIDI_TTL = 20 * 60


def _yeni_ihaleler(prefix, sahip_id, tenders):
    """Bu abonelik için daha önce bildirilmemiş ihaleleri süzer ve işaretler.

    `cache.add` atomiktir → aynı görev iki kez tetiklense de bir ihale bir kez geçer.
    """
    yeni = []
    for tender in tenders:
        if cache.add(f"{prefix}:{sahip_id}:{tender.pk}", 1, _TENDER_DEDUP_TTL):
            yeni.append(tender)
    return yeni


def _bildirim_taban():
    """Bildirim penceresinin alt sınırı: son `NOTIF_LOOKBACK_DAYS` günün başı.

    ⚠️ Pencere yalnızca **arşiv gürültüsüne** karşıdır (backfill 2019 ihalesini bugün
    ekleyebilir); mükerrerliği `_yeni_ihaleler` engeller. Bu yüzden geniş tutulabilir.
    """
    gun = timezone.localdate() - timedelta(days=getattr(settings, "NOTIF_LOOKBACK_DAYS", 1))
    return local_day_range(gun)[0]

# Rakip alarmı: `ilk_gorulme` bugün OLSA BİLE sözleşme bundan eskiyse bildirim gitmez.
# Gerekçe: arşiv süpürmesi daha önce bağlanmamış eski sözleşmeleri bugün "ilk kez"
# görüyor; 2021 tarihli bir sözleşme rakip takibi haberi değildir.
_RAKIP_TAZELIK_GUN = 90


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
    Alarmı açık kayıtlı filtreler için, filtreye uyan ve **son `NOTIF_LOOKBACK_DAYS`
    günde yayımlanmış, bu abonelik için daha önce bildirilmemiş** açık ihaleleri bulur.
    ⚠️ Görev gün içinde birkaç kez koşar (EKAP'ın yayım saati bilinmiyor); mükerrerliği
    zaman penceresi değil **ihale bazlı dedup** (`_yeni_ihaleler`) engeller. **Her filtre için AYRI** uygulama-içi
    satır + push atılır (kullanıcı başına birleşik özet DEĞİL): 10 filtreden 8'i eşleşirse 8 ayrı
    bildirim gider. Başlık = filtre adı, gövde = "{filtre} filtrenize uygun N adet ihale bulundu.";
    tıklanınca tek ihale DEĞİL, `filter_id` ile o filtrenin sonuç listesi açılır. Filtre başına
    kısa tur kilidi (eşzamanlı tetiklemeye karşı) + ihale bazlı dedup.
    **Filtre alarmı Pro'ya özeldir.**
    """
    from ekap.models import Tender
    from ekap.views import apply_tender_filters

    from .models import Notification, SavedFilter
    from .services import notify, templates

    OPEN_STATUSES = [2, 3]
    now = timezone.now()

    processed = 0
    notified = 0
    pushed = 0

    taban = _bildirim_taban()

    for sf in SavedFilter.objects.filter(alarm__isnull=False).select_related("user").iterator():
        if not _alarm_enabled(sf.alarm):
            continue
        # Filtre alarmı Pro'ya özeldir → Pro iken alarmlı filtre kurup Free'ye düşen
        # kullanıcıya bildirim gitmesin (is_premium property → Python'da eleriz).
        if not sf.user.is_premium:
            continue
        # ⚠️ Gün-kilidi KALDIRILDI: görev artık gün içinde birkaç kez koşuyor ve
        # gün-kilidi ikinci/üçüncü turu tümüyle atlardı. Yerine kısa **tur kilidi**
        # (yalnızca eşzamanlı tetiklemeye karşı) + ihale bazlı dedup geçti.
        if not cache.add(f"filter:tur:{sf.user_id}:{sf.id}", 1, _TUR_KILIDI_TTL):
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
            # Teklifi geçmemiş (biddable).
            base = base.filter(Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True))
            # Pencere arşiv gürültüsüne karşıdır (backfill eski ihaleyi bugün ekleyebilir);
            # mükerrerliği `_yeni_ihaleler` engeller. ⚠️ `ilan_tarihi` detay senkronundan
            # dolar → detayı henüz gelmemiş ihale bu turda değil, sonraki turda yakalanır
            # (dedup ihaleye bağlı olduğu için kaçmaz — zamana bağlı olsaydı kaçardı).
            base = base.filter(ilan_tarihi__gte=taban)

            sf.last_notified_at = now
            sf.save(update_fields=["last_notified_at"])

            new_list = _yeni_ihaleler("nf", sf.id, base.order_by("-ilan_tarihi")[:50])[:20]
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
    Favori idareler (alarm açık) için, o idarenin **yalnızca ilan_tarihi BUGÜN olan** açık
    ihalelerini bulur (eski/dün yayınlananlar DEĞİL). **Her favori idare için AYRI** uygulama-içi
    satır + push atılır (kullanıcı başına birleşik özet DEĞİL). Başlık = idare adı; tıklanınca
    tek ihale DEĞİL, `authority_detsis` ile o idarenin ihale listesi (`GET /ekap/tenders/?idare_detsis=`)
    açılır. Seçilen `detsis_no` `descendant_idare_ids` ile alt birim `idare_id`'lerine genişletilir.
    İdare başına atomik gün-kilidi ("bugün" filtresi cross-day dedup'ı sağlar). **Favori idare
    alarmı Pro'ya özeldir.**
    """
    from ekap.detsis_tree import descendant_idare_ids, tender_idare_id_set
    from ekap.models import Tender

    from .models import FavoriteAuthority, Notification
    from .services import notify, templates

    OPEN_STATUSES = [2, 3]
    now = timezone.now()

    processed = 0
    notified = 0
    pushed = 0

    taban = _bildirim_taban()

    for fav in FavoriteAuthority.objects.filter(alarm=True).select_related("user").iterator():
        # Favori idare alarmı Pro'ya özeldir → Free üyeye bildirim yok.
        if not fav.user.is_premium:
            continue
        # Gün-kilidi yerine kısa tur kilidi — bkz. check_saved_filter_matches.
        if not cache.add(f"authority:tur:{fav.user_id}:{fav.detsis_no}", 1, _TUR_KILIDI_TTL):
            continue
        processed += 1
        try:
            # detsis_no → tüm alt birimlerin idare_id'leri (ihalede gerçekten geçenlerle kesiş).
            expanded = descendant_idare_ids([fav.detsis_no])
            if expanded:
                expanded &= tender_idare_id_set()
            if not expanded:
                continue
            base = (
                Tender.objects.filter(idare_id__in=expanded, ihale_durum__in=OPEN_STATUSES)
                .filter(Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True))
                .filter(ilan_tarihi__gte=taban)
                .order_by("-ilan_tarihi")
            )

            fav.last_notified_at = now
            fav.save(update_fields=["last_notified_at"])

            new_list = _yeni_ihaleler("na", fav.id, base[:50])[:20]
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


@shared_task(name="tenders.tasks.check_favorite_contractor_matches")
def check_favorite_contractor_matches():
    """
    Takip edilen firmalar (alarm açık) yeni bir iş aldığında bildirim gönderir.

    Favori idare görevinin ikizi: **her firma için AYRI** uygulama-içi satır + push,
    başlık = firma adı, tıklanınca `contractor_id` ile firma detayı açılır.
    Firma başına atomik gün-kilidi. **Pro'ya özeldir** (takip etmek serbest, bildirim Pro).

    ## "Yeni iş" nasıl tanımlanır

    `Contract.ilk_gorulme` = satırı **ilk kez gördüğümüz** an (bkz. `ekap/models.py`).
    `sozlesme_tarihi` bu iş için KULLANILAMAZ: Sonuç İlanı imzadan aylar sonra
    yayımlanabildiği için eski tarihli bir sözleşme bugün keşfedilebiliyor.

    ⚠️ **Arşiv gürültüsü koruması**: `sync_contractors` süpürmesi arşivi tararken daha önce
    hiç bağlanmamış ESKİ sözleşmeleri de ilk kez görür ve onlara bugünün `ilk_gorulme`'sini
    yazar. O satırlar teknik olarak "yeni keşfedildi" ama kullanıcı için haber değil —
    2021'de imzalanmış bir sözleşme rakip takibi bildirimi olmamalı. Bu yüzden ikinci koşul:
    `sozlesme_tarihi` son `_RAKIP_TAZELIK_GUN` gün içinde olmalı.
    """
    from ekap.models import Contract

    from .models import FavoriteContractor, Notification
    from .services import notify, templates

    now = timezone.now()
    today = timezone.localdate()
    gun_bas, gun_bit = local_day_range(today)
    tazelik_taban = now - timedelta(days=_RAKIP_TAZELIK_GUN)

    processed = notified = pushed = 0

    for fav in (
        FavoriteContractor.objects.filter(alarm=True)
        .select_related("user", "contractor")
        .iterator()
    ):
        if not fav.user.is_premium:
            continue  # takip serbest, bildirim Pro'ya özel
        if not cache.add(
            f"contractor:{fav.user_id}:{fav.contractor_id}:{today.isoformat()}",
            1, _ROW_DEDUP_TTL,
        ):
            continue
        processed += 1
        try:
            yeni = list(
                Contract.objects.filter(
                    yuklenici_id=fav.contractor_id,
                    ilk_gorulme__gte=gun_bas,
                    ilk_gorulme__lt=gun_bit,
                    sozlesme_tarihi__gte=tazelik_taban,  # arşiv gürültüsünü ele
                )
                .select_related("tender")
                .defer("tender__detail_raw", "tender__list_raw")
                .order_by("-sozlesme_tarihi")[:20]
            )
            fav.last_notified_at = now
            fav.save(update_fields=["last_notified_at"])
            if not yeni:
                continue

            title, body = templates.contractor_match(
                firma_adi=fav.contractor.kanonik_ad,
                count=len(yeni),
                ihale_adi=yeni[0].tender.ihale_adi if len(yeni) == 1 else None,
            )
            notify.record_notification(
                fav.user,
                type=Notification.Type.TENDER,
                title=title,
                body=body,
                contractor_id=fav.contractor_id,  # tıklanınca firma detayı
            )
            notified += 1
            if notify.push_to_user(
                fav.user, title=title, body=body,
                data={"type": Notification.Type.TENDER, "contractorId": fav.contractor_id},
                idem_key=None,  # gün-kilidi tekilliği garanti ediyor
            ):
                pushed += 1
        except Exception:
            logger.exception("check_favorite_contractor_matches: %s işlenemedi", fav.pk)
            continue

    logger.info(
        "check_favorite_contractor_matches: %s firma, %s bildirim, %s push",
        processed, notified, pushed,
    )
    return {"contractors": processed, "notified": notified, "pushed": pushed}


@shared_task(name="tenders.tasks.weekly_free_teaser")
def weekly_free_teaser(days: int = 7):
    """
    Ücretsiz üyeye **haftada bir** "bu hafta neyi kaçırdın" özeti.

    ## Neden gerekli

    Günlük alarm görevleri Free kullanıcıyı **sayılmadan** eliyor (`if not user.is_premium:
    continue`). Sonuç: kullanıcı alarm anahtarını açıyor, hiçbir şey olmuyor, Pro'nun ne
    işe yaradığını **hiç hissetmiyor**. Kaçırılan bildirim = kaçırılan satış.

    ## Neden ayrı görev, günlük görevlere Free eklemek yerine

    Free tabanı günlük dört ağır sorguya sokmak maliyeti tabana orantılı büyütürdü. Bu
    görev **haftada bir** çalışır ve yalnızca **sayı** üretir (`.count()`); ihale gövdesi,
    serializer, liste hiç yok.

    ## Sınırlar

    - **Sıfır eşleşme → bildirim YOK.** Boş teaser ("0 ihale kaçırdınız") güven kaybettirir.
    - Kullanıcı başına **tek** özet (abonelik-başına ayrı push deseninin istisnası —
      burada amaç bilgilendirme değil, dönüşüm).
    - Atomik **hafta kilidi** (`teaser:{uid}:{yil}-{hafta}`) → yinelenmiş beat çoğaltmaz.
    - Mevcut pacing kapılarından geçer (sessiz saat, günlük cap, push tercihi).
    - `Notification.type=INFO` + derin bağlantı alanı YOK → mobil bunu Paywall'a yönlendirir.
    """
    from django.contrib.auth import get_user_model

    from ekap.models import Tender
    from ekap.views import apply_tender_filters

    from .models import FavoriteAuthority, Notification, SavedFilter
    from .services import notify, templates

    OPEN_STATUSES = [2, 3]
    now = timezone.now()
    bugun = timezone.localdate()
    yil, hafta, _ = bugun.isocalendar()
    baslangic = now - timedelta(days=days)

    User = get_user_model()
    # Yalnızca alarmlı aboneliği OLAN Free kullanıcılar: hiç filtre/idare kaydetmemiş
    # birine "kaçırdıklarınız" demek anlamsız olurdu.
    aday_ids = set(
        SavedFilter.objects.filter(alarm__isnull=False).values_list("user_id", flat=True)
    ) | set(
        FavoriteAuthority.objects.filter(alarm=True).values_list("user_id", flat=True)
    )
    if not aday_ids:
        return {"users": 0, "pushed": 0}

    bildirilen = pushed = 0
    for user in User.objects.filter(pk__in=aday_ids, is_active=True).iterator():
        if user.is_premium:
            continue  # Pro zaten günlük bildirim alıyor
        if not cache.add(f"teaser:{user.pk}:{yil}-{hafta}", 1, 8 * 24 * 3600):
            continue

        try:
            toplam_ihale = 0
            filtre_sayisi = idare_sayisi = 0

            # ── Kayıtlı filtreler ──
            for sf in SavedFilter.objects.filter(user=user, alarm__isnull=False):
                if not _alarm_enabled(sf.alarm):
                    continue
                base = apply_tender_filters(Tender.objects.all(), sf.filters or {})
                if not (sf.filters or {}).get("ihale_durum"):
                    base = base.filter(ihale_durum__in=OPEN_STATUSES)
                adet = (
                    base.filter(Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True))
                    .filter(ilan_tarihi__gte=baslangic)
                    .count()
                )
                if adet:
                    toplam_ihale += adet
                    filtre_sayisi += 1

            # ── Favori idareler ──
            favoriler = list(FavoriteAuthority.objects.filter(user=user, alarm=True))
            if favoriler:
                from ekap.detsis_tree import descendant_idare_ids, tender_idare_id_set

                gecerli = tender_idare_id_set()
                for fav in favoriler:
                    expanded = descendant_idare_ids([fav.detsis_no]) & gecerli
                    if not expanded:
                        continue
                    adet = (
                        Tender.objects.filter(
                            idare_id__in=expanded, ihale_durum__in=OPEN_STATUSES
                        )
                        .filter(Q(ihale_tarihi__gte=now) | Q(ihale_tarihi__isnull=True))
                        .filter(ilan_tarihi__gte=baslangic)
                        .count()
                    )
                    if adet:
                        toplam_ihale += adet
                        idare_sayisi += 1

            if not toplam_ihale:
                continue  # boş teaser gönderilmez

            title, body = templates.free_teaser(
                ihale=toplam_ihale, filtre=filtre_sayisi, idare=idare_sayisi
            )
            notify.record_notification(
                user, type=Notification.Type.INFO, title=title, body=body
            )
            bildirilen += 1
            if notify.push_to_user(
                user, title=title, body=body,
                data={"type": Notification.Type.INFO, "teaser": "1"},
                idem_key=None,  # hafta kilidi tekilliği zaten garanti ediyor
            ):
                pushed += 1
        except Exception:
            logger.exception("weekly_free_teaser: kullanıcı %s işlenemedi", user.pk)
            continue

    logger.info("weekly_free_teaser: %s bildirim, %s push", bildirilen, pushed)
    return {"users": bildirilen, "pushed": pushed}


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
