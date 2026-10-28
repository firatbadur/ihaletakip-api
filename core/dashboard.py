"""
Admin anasayfa panosu — metrik hesaplayıcı.

HTTP'den bağımsız saf modül (`ekap/market.py`, `ekap/benchmark.py` ile aynı desen):
şablon katmanı yalnızca `core/templatetags/dashboard_tags.py` üzerinden sarmalar,
böylece metrikler `manage.py shell`'den de test edilebilir.

⚠️ PERFORMANS SÖZLEŞMESİ
`ekap_tender` (~1M satır / 7,5 GB) ve `ekap_contract` (~1,4M) üzerinde **`COUNT(*)`
çağrılmaz**. Tam sayım soğuk buffer'da saniyeler sürer ve `shared_buffers`'ı
boşaltarak ihale arama ucunu diske düşürür — CLAUDE.md'de ölçülmüş "%53 heap cache
isabeti" arızasının aynısı. Bunun yerine `pg_class.reltuples` yaklaşık sayımı
kullanılır ve panoda **"≈" işaretiyle** gösterilir. Pano bir büyüklük göstergesidir,
muhasebe değil.

⚠️ `ekap.Tender.created_at` İNDEKSSİZDİR, `ekap.Contract`'ta hiç yoktur → o
tablolarda zaman eşiği yalnızca indeksli `ilan_tarihi` / `ihale_tarihi` /
`sozlesme_tarihi` üzerinden kurulur.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.db.models import Count, Q
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone
from django.utils.formats import number_format

logger = logging.getLogger(__name__)

User = get_user_model()

# Metrik şeması değişince artır → deploy sonrası bayat şekilli dict şablonu patlatmasın.
CACHE_VERSION = "v1"

TTL_HIZLI = 60  # küçük tablolar — yalnızca F5 koruması
TTL_SERI = 300  # grafik serileri
TTL_AGIR = 3600  # ekap yaklaşık sayımları

_K_OZET = f"dash:{CACHE_VERSION}:ozet"
_K_SERI = f"dash:{CACHE_VERSION}:seri"
_K_EKAP = f"dash:{CACHE_VERSION}:ekap"

AYLAR_TR = [
    "Oca", "Şub", "Mar", "Nis", "May", "Haz",
    "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara",
]


# ── Zaman yardımcıları ──────────────────────────────────
# (TIME_ZONE=Europe/Istanbul, USE_TZ=True → "bugün" yerel gündür, UTC günü değil.)
def _gun_basi(gun: date) -> datetime:
    """Yerel gün başlangıcının aware datetime karşılığı."""
    return timezone.make_aware(datetime.combine(gun, time.min))


def bugun_basi() -> datetime:
    return _gun_basi(timezone.localdate())


def _once(gun: int) -> datetime:
    return timezone.now() - timedelta(days=gun)


# ── Yaklaşık satır sayısı ───────────────────────────────
def yaklasik_satir(model) -> tuple[int, bool]:
    """
    (sayı, yaklasik_mi) döndürür.

    Postgres'te `pg_class.reltuples` (ANALYZE/autovacuum'un tuttuğu tahmin),
    diğer motorlarda (yerel geliştirmedeki SQLite) canlı `COUNT(*)` — dev DB küçüktür.

    ⚠️ Hiç ANALYZE edilmemiş tabloda `reltuples` **-1** gelir. O durumda (0, False)
    dönülür ve şablon "—" gösterir: uydurma bir sayı basmak, yanlış sayı göstermenin
    en kötü türü olurdu.
    """
    tablo = model._meta.db_table
    if connection.vendor != "postgresql":
        try:
            return model.objects.count(), False
        except Exception:
            logger.warning("%s sayılamadı", tablo, exc_info=True)
            return 0, False
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)",
                [tablo],
            )
            satir = cur.fetchone()
    except Exception:
        logger.warning("reltuples okunamadı: %s", tablo, exc_info=True)
        return 0, False
    if not satir or satir[0] is None or satir[0] < 0:
        return 0, False
    return int(satir[0]), True


# ── Premium ─────────────────────────────────────────────
def pro_q(now=None) -> Q:
    """
    `accounts.User.is_premium` property'sinin SQL karşılığı.

    ⚠️ Property ile BİREBİR aynı mantığı taşımalıdır. Ayrışırsa panodaki "Pro"
    sayısı ile kullanıcının gerçekten gördüğü özellik kapısı çelişir — bu yüzden
    `core/tests/test_dashboard.py` iki tarafı karşılaştıran bir eşdeğerlik testi
    tutar.
    """
    now = now or timezone.now()
    return Q(is_superuser=True) | (
        Q(subscription_tier=User.Tier.PRO)
        & (Q(subscription_expires_at__isnull=True) | Q(subscription_expires_at__gt=now))
    )


# ── Biçimleme ───────────────────────────────────────────
def bicimle(n) -> str:
    """
    Türkçe binlik ayracıyla sayı.

    ⚠️ `humanize.intcomma` KULLANILMAZ — virgül basar, Türkçe'de virgül ondalık
    ayracıdır (1.234 doğru, 1,234 yanlış okunur).
    """
    if n is None:
        return "—"
    return number_format(n, force_grouping=True)


def _yuzde_degisim(simdi: int, onceki: int):
    """Düne/geçen döneme göre % değişim; payda 0 ise None (bölme değil, 'ölçülemez')."""
    if not onceki:
        return None
    return round((simdi - onceki) * 100.0 / onceki, 1)


# ── Seri üretimi ────────────────────────────────────────
def gunluk_seri(qs, alan: str, gun_sayisi: int = 30) -> dict:
    """
    Son `gun_sayisi` gün için günlük sayım serisi.

    ⚠️ Eksik günler **0 ile doldurulur**: DB yalnızca kayıt olan günleri döndürür;
    doldurulmazsa grafiğin x ekseni yalan söyler (aralarında bir hafta olan iki nokta
    yan yana çizilir).

    ⚠️ `TruncDate(..., tzinfo=...)` açıkça geçilir. Aktif timezone'a örtük güvenmek
    Celery/komut bağlamında UTC'ye düşebilir; o zaman "bugün" saat 03:00'te başlar.
    """
    tz = timezone.get_current_timezone()
    bugun = timezone.localdate()
    baslangic = _gun_basi(bugun - timedelta(days=gun_sayisi - 1))
    ham = (
        qs.filter(**{f"{alan}__gte": baslangic})
        .annotate(_g=TruncDate(alan, tzinfo=tz))
        .order_by()  # Meta.ordering GROUP BY'a sızmasın
        .values("_g")
        .annotate(n=Count("id"))
    )
    sayac = {r["_g"]: r["n"] for r in ham}
    gunler = [bugun - timedelta(days=i) for i in range(gun_sayisi - 1, -1, -1)]
    degerler = [sayac.get(g, 0) for g in gunler]
    return {
        "etiketler": [g.strftime("%d.%m") for g in gunler],
        "degerler": degerler,
        "toplam": sum(degerler),
    }


def aylik_seri(qs, alan: str, ay_sayisi: int = 12) -> dict:
    """Son `ay_sayisi` ay için aylık sayım + kümülatif seri (eksik aylar 0)."""
    tz = timezone.get_current_timezone()
    bugun = timezone.localdate()
    # ⚠️ Ay aritmetiği gün çıkarmayla YAPILMAZ: 31*11 gün ~11,2 ay eder ve ayın
    # uzunluğuna göre bazen 12 ay geriye düşer; o zaman 12 ay ileri sayınca seri
    # BU AYA ulaşmaz (son sütun geçen ay olur, bugünkü kayıtlar hiç görünmez).
    _toplam_ay = bugun.year * 12 + (bugun.month - 1) - (ay_sayisi - 1)
    ilk_ay = date(_toplam_ay // 12, _toplam_ay % 12 + 1, 1)
    ham = (
        qs.filter(**{f"{alan}__gte": _gun_basi(ilk_ay)})
        .annotate(_a=TruncMonth(alan, tzinfo=tz))
        .order_by()
        .values("_a")
        .annotate(n=Count("id"))
    )
    sayac = {}
    for r in ham:
        a = r["_a"]
        sayac[(a.year, a.month)] = r["n"]

    aylar, etiketler, degerler = [], [], []
    yil, ay = ilk_ay.year, ilk_ay.month
    for _ in range(ay_sayisi):
        aylar.append((yil, ay))
        etiketler.append(f"{AYLAR_TR[ay - 1]} {str(yil)[2:]}")
        degerler.append(sayac.get((yil, ay), 0))
        ay += 1
        if ay > 12:
            ay, yil = 1, yil + 1

    # Kümülatif: serinin başlangıcından ÖNCEKİ toplam taban alınır, yoksa
    # "toplam kullanıcı" eğrisi ilk aydan başlıyormuş gibi görünürdü.
    taban = qs.filter(**{f"{alan}__lt": _gun_basi(ilk_ay)}).count()
    kumulatif, toplam = [], taban
    for d in degerler:
        toplam += d
        kumulatif.append(toplam)

    return {
        "etiketler": etiketler,
        "degerler": degerler,
        "kumulatif": kumulatif,
        "toplam": sum(degerler),
    }


def sparkline_path(degerler, genislik: int = 100, yukseklik: int = 28) -> str:
    """
    Sunucuda üretilen mini trend çizgisi (SVG `d` özniteliği).

    KPI kartı başına bir Chart.js instance'ı açmak yerine tek bir `<path>` basarız:
    ucuz, JS gerektirmez ve `currentColor` sayesinde koyu temaya bedava uyar.
    Çıktı tamamen sayısaldır (kullanıcı girdisi yok) → şablonda `|safe` güvenlidir.
    """
    if not degerler or len(degerler) < 2:
        return ""
    en_az, en_cok = min(degerler), max(degerler)
    aralik = (en_cok - en_az) or 1
    adim = genislik / (len(degerler) - 1)
    noktalar = []
    for i, d in enumerate(degerler):
        x = round(i * adim, 2)
        # SVG'de y aşağı doğru büyür → değer yükseldikçe y küçülmeli.
        y = round(yukseklik - ((d - en_az) / aralik) * (yukseklik - 2) - 1, 2)
        noktalar.append(f"{x},{y}")
    return "M" + " L".join(noktalar)


# ── Metrik dilimleri ────────────────────────────────────
def _hesapla_ozet() -> dict:
    from ai.models import AnalysisCache
    from assistant.models import ChatConversation, ChatMessage, CompanyProfile, TenderRecommendation
    from core.models import SupportTicket
    from ekap.models import SyncRun, Tender
    from tenders.tasks import _alarm_enabled
    from tenders.models import (
        Favorite,
        FavoriteAuthority,
        FavoriteContractor,
        Notification,
        SavedFilter,
        SavedTender,
        TenderAlarm,
        TenderGroup,
    )

    simdi = timezone.now()
    bugun = bugun_basi()
    dun = bugun - timedelta(days=1)
    gun7, gun30 = _once(7), _once(30)
    saat24 = simdi - timedelta(hours=24)

    kullanicilar = User.objects.all()
    bugun_kayit = kullanicilar.filter(date_joined__gte=bugun).count()
    dun_kayit = kullanicilar.filter(date_joined__gte=dun, date_joined__lt=bugun).count()

    bildirim_turleri = list(
        Notification.objects.filter(created_at__gte=gun30)
        .order_by()
        .values("type")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    tur_adlari = dict(Notification.Type.choices)

    saglayicilar = list(
        kullanicilar.order_by().values("provider").annotate(n=Count("id")).order_by("-n")
    )
    saglayici_adlari = dict(User.Provider.choices)

    son_sync = SyncRun.objects.order_by("-started_at").first()
    bildirim_24s = Notification.objects.filter(created_at__gte=saat24).count()

    return {
        "kullanici": {
            "toplam": kullanicilar.count(),
            "aktif": kullanicilar.filter(is_active=True).count(),
            "bugun": bugun_kayit,
            "dun": dun_kayit,
            "bugun_degisim": _yuzde_degisim(bugun_kayit, dun_kayit),
            "son_7g": kullanicilar.filter(date_joined__gte=gun7).count(),
            "son_30g": kullanicilar.filter(date_joined__gte=gun30).count(),
            # MAU/DAU `last_seen_at`'e dayanır (bkz. accounts/authentication.py).
            # ⚠️ Ölçüm alan eklendiği günden itibaren birikir; ilk haftalarda düşük
            # görünmesi normaldir, şablon bunu not olarak yazar.
            "mau": kullanicilar.filter(last_seen_at__gte=gun30).count(),
            "dau": kullanicilar.filter(last_seen_at__gte=bugun).count(),
            "hic_gorulmedi": kullanicilar.filter(last_seen_at__isnull=True).count(),
            # Kaydı olan en az bir cihazı bulunan kullanıcı sayısı (accounts.PushDevice, 0008).
            # ⚠️ `.distinct()` ŞART: çok cihazlı kullanıcı JOIN'de birden çok satır üretir.
            # ⚠️ Geçişte sayının DÜŞMESİ beklenir (gerileme değil): eski `exclude(fcm_token="")`
            # aynı telefonda giriş yapmış her hesabı ayrı sayıyordu; üretimde bir cihaz dört
            # hesap üzerinden dört kez görünüyordu (2026-10-05 arızası).
            "push_acik": kullanicilar.filter(push_devices__isnull=False).distinct().count(),
            "saglayicilar": [
                {"ad": saglayici_adlari.get(s["provider"], s["provider"] or "—"), "n": s["n"]}
                for s in saglayicilar
            ],
        },
        "abonelik": {
            "pro": kullanicilar.filter(pro_q(simdi)).count(),
            "free": kullanicilar.exclude(pro_q(simdi)).count(),
            # İptal etti ama dönem sürüyor → win-back hedefi (RevenueCat izi).
            "iptal_suren": kullanicilar.filter(
                pro_q(simdi), subscription_cancelled_at__isnull=False
            ).count(),
            "iptal_bitti": kullanicilar.filter(
                subscription_cancelled_at__isnull=False
            ).exclude(pro_q(simdi)).count(),
            # tier hâlâ 'pro' ama süre dolmuş → RevenueCat senkron gecikmesi göstergesi.
            "senkron_gecikmesi": kullanicilar.filter(
                subscription_tier=User.Tier.PRO,
                subscription_expires_at__lt=simdi,
                is_superuser=False,
            ).count(),
        },
        "icerik": {
            "kayitli_ihale": SavedTender.objects.count(),
            "kayitli_ihale_bugun": SavedTender.objects.filter(saved_at__gte=bugun).count(),
            "favori": Favorite.objects.count(),
            "favori_bugun": Favorite.objects.filter(added_at__gte=bugun).count(),
            "favori_idare": FavoriteAuthority.objects.count(),
            "favori_firma": FavoriteContractor.objects.count(),
            "klasor": TenderGroup.objects.count(),
            "filtre": SavedFilter.objects.count(),
            # ⚠️ `SavedFilter.alarm` bir JSONField (bool / dict / null olabilir) →
            # SQL'de güvenilir biçimde süzülemez. Bildirim görevinin kullandığı TEK
            # doğruluk kaynağını (`tenders.tasks._alarm_enabled`) yeniden kullanıp
            # Python'da sayarız; tablo binler mertebesinde olduğu için ucuz.
            "filtre_alarmli": sum(
                1
                for a in SavedFilter.objects.values_list("alarm", flat=True)
                if _alarm_enabled(a)
            ),
            "alarm": TenderAlarm.objects.count(),
            "alarm_bugun": TenderAlarm.objects.filter(created_at__gte=bugun).count(),
        },
        "asistan": {
            "profil": CompanyProfile.objects.count(),
            "profil_aktif": CompanyProfile.objects.filter(is_active=True).count(),
            "profil_bugun": CompanyProfile.objects.filter(created_at__gte=bugun).count(),
            "sohbet": ChatConversation.objects.count(),
            "sohbet_24s": ChatConversation.objects.filter(created_at__gte=saat24).count(),
            "mesaj_24s": ChatMessage.objects.filter(created_at__gte=saat24).count(),
            "oneri_bugun": TenderRecommendation.objects.filter(
                date=timezone.localdate()
            ).count(),
            "oneri_kullanici": TenderRecommendation.objects.filter(date=timezone.localdate())
            .order_by()
            .values("user")
            .distinct()
            .count(),
            "analiz": AnalysisCache.objects.count(),
            "analiz_bugun": AnalysisCache.objects.filter(created_at__gte=bugun).count(),
        },
        "bildirim": {
            "son_24s": bildirim_24s,
            "okunmamis": Notification.objects.filter(read=False).count(),
            "turler": [
                {"ad": tur_adlari.get(t["type"], t["type"]), "n": t["n"]}
                for t in bildirim_turleri
            ],
        },
        "operasyon": {
            "destek_acik": SupportTicket.objects.filter(status="Bekliyor").count(),
            "destek_bugun": SupportTicket.objects.filter(created_at__gte=bugun).count(),
            "sync_hata_24s": SyncRun.objects.filter(
                started_at__gte=saat24, status="error"
            ).count(),
            "son_sync": (
                {
                    "task": son_sync.task,
                    "status": son_sync.status,
                    "zaman": timezone.localtime(son_sync.started_at),
                    "items": son_sync.items,
                    "errors": son_sync.errors,
                }
                if son_sync
                else None
            ),
            # İndeksli `ilan_tarihi` üzerinden dar aralık — canlı sayılabilir.
            "bugun_ilan": Tender.objects.filter(ilan_tarihi__gte=bugun).count(),
        },
    }


def _hesapla_seriler() -> dict:
    from assistant.models import ChatConversation
    from tenders.models import Favorite, SavedTender

    kayit_30g = gunluk_seri(User.objects.all(), "date_joined", 30)
    return {
        "kayit_30g": kayit_30g,
        "kayit_7g": kayit_30g["degerler"][-7:],
        "kayit_12ay": aylik_seri(User.objects.all(), "date_joined", 12),
        "kayitli_ihale_30g": gunluk_seri(SavedTender.objects.all(), "saved_at", 30),
        "favori_30g": gunluk_seri(Favorite.objects.all(), "added_at", 30),
        "sohbet_30g": gunluk_seri(ChatConversation.objects.all(), "created_at", 30),
    }


def _hesapla_ekap() -> dict:
    """⚠️ Yalnızca yaklaşık sayımlar — gerekçe modül docstring'inde."""
    from ekap.models import Contract, Contractor, Tender

    ihale, ihale_yak = yaklasik_satir(Tender)
    sozlesme, sozlesme_yak = yaklasik_satir(Contract)
    firma, firma_yak = yaklasik_satir(Contractor)
    return {
        "ihale": ihale,
        "ihale_yaklasik": ihale_yak,
        "sozlesme": sozlesme,
        "sozlesme_yaklasik": sozlesme_yak,
        "firma": firma,
        "firma_yaklasik": firma_yak,
    }


def ekap_dogrulama_durumu() -> dict:
    """EKAP insan doğrulamasının panodaki özeti.

    ⚠️ **Canlı EKAP sorgusu YAPILMAZ** — her admin sayfası açılışında dışarıya
    HTTP isteği atmak kabul edilemez (yavaşlık + gereksiz WAF trafiği).
    Kaynak, `ekap-browser` daemon'ının ve istemcinin yazdığı durum kaydıdır;
    çerezin gerçekten geçerli olup olmadığını daemon her dakika ölçüyor.
    """
    from core.models import AppSetting
    from ekap import session as ekap_session
    from ekap.tasks import katalog_durumu

    katalog = katalog_durumu()
    kayit = AppSetting.objects.filter(key=ekap_session.ANAHTAR_DURUM).first()
    metin = kayit.value if kayit else ""
    dustu = bool(cache.get("ekap:dogrulama:dustu"))
    var = bool(ekap_session.cerez())
    # ⚠️ Yalnızca önbellek okunur (canli=False) — pano EKAP'a istek atmaz.
    gerekli = ekap_session.dogrulama_gerekli(canli=False)
    son_kontrol = None
    if gerekli is None:
        # Kapı önbelleği kısa ömürlü; görevin son kesin cevabı 2 saate kadar geçerli sayılır.
        son, zaman = ekap_session.son_bilinen_gerekli()
        if zaman and timezone.now() - zaman <= timedelta(hours=2):
            gerekli, son_kontrol = son, zaman
    if dustu:
        seviye = "hata"
    elif gerekli is False:
        # 2026-10-27'den beri EKAP doğrulama istemiyor; eski "DÜŞTÜ" kaydı geçmiştir.
        seviye, metin = "ok", "EKAP şu an doğrulama istemiyor (çerezsiz çalışılıyor)"
        if son_kontrol:
            metin += f" · son kontrol {timezone.localtime(son_kontrol):%H:%M}"
    elif metin.startswith("DÜŞTÜ"):
        seviye = "hata"
    elif var:
        seviye = "ok"
    else:
        seviye = "yok"
    return {
        "seviye": seviye,
        "metin": metin or "hiç yapılandırılmadı",
        "guncelleme": timezone.localtime(kayit.updated_at) if kayit else None,
        # OKAS / DETSIS yalnızca v2'den gelir; son başarılı tur 8 günden eskiyse uyarı.
        "katalog": katalog,
        "katalog_eski": any(k["eski"] for k in katalog),
    }


def ekap_mobil_durumu() -> dict:
    """
    EKAP **mobil** hattının panodaki özeti (birincil kaynak).

    ⚠️ **Cache'lenmez ve canlı EKAP sorgusu YAPMAZ**: hepsi Redis sayaçları + tek
    küçük DB sorgusu. "Toplama duruyor mu" sorusunda 60 sn eski bilgi kabul edilemez
    (`ekap_dogrulama_durumu` ile aynı gerekçe).

    ⚠️ Buradaki asıl sinyal `SyncRun` DEĞİL: çekmeli tik görevi tur başına satır
    yazmıyor (günde ~720 boş kayıt olurdu). Gerçek kanıt bugünkü iş sayaçları ve
    `Tender.detay_kaynak='mobil'` satırlarıdır.
    """
    from django.conf import settings

    from ekap.mobil import captcha as mobil_captcha
    from ekap.mobil import tasks as mobil_tasks
    from ekap.mobil import throttle as mobil_throttle
    from ekap.models import Tender

    acik = bool(getattr(settings, "EKAP_MOBIL_ENABLED", False))
    bekliyor = mobil_captcha.bekliyor_mu()
    bekleyen = bool(mobil_captcha.bekleyen_oku())
    sayac = mobil_tasks.sayaclar()
    butce = mobil_throttle.butce_ozet()
    bas = bugun_basi()
    bugunku = Tender.objects.filter(
        detay_kaynak="mobil", detail_synced_at__gte=bas
    ).count()

    if bekliyor:
        seviye = "hata"
    elif not acik:
        seviye = "kapali"
    elif bekleyen:
        seviye = "uyari"
    else:
        seviye = "ok"
    return {
        "seviye": seviye,
        "acik": acik,
        "captcha_bekliyor": bekliyor,
        "captcha_var": bekleyen,
        "captcha_durum": mobil_captcha.bekleyen_durum(),
        "sayac": sayac,
        "butce": butce,
        "bugun_detay": bugunku,
    }


# ── İş hatları (günlük durum) ──────────────────────────
_K_HAT = f"dash:{CACHE_VERSION}:hatlar"
TTL_HAT = 60


KAYNAK_V2, KAYNAK_MOBIL, KAYNAK_IC = "v2 (eski EKAP)", "mobil API", "iç iş (EKAP'a gitmez)"


def _hat(ad, kaynak, seviye, bugun, son=None, notu=""):
    return {"ad": ad, "kaynak": kaynak, "seviye": seviye, "bugun": bugun,
            "son": timezone.localtime(son) if son else None, "not": notu}


def _syncrun_ozet(gorev, bas):
    """Bugünkü turlar + son başarılı tur (SyncRun yazan görevler için)."""
    from django.db.models import Max, Sum
    from ekap.models import SyncRun
    bugun = SyncRun.objects.filter(task=gorev, started_at__gte=bas).aggregate(
        tur=Count("pk"), kayit=Sum("items"), hata=Sum("errors"))
    son_ok = (SyncRun.objects.filter(task=gorev, status="ok")
              .aggregate(m=Max("finished_at"))["m"])
    return bugun, son_ok


def _yasli(son, esik, simdi):
    return not son or simdi - son > esik


def _hesapla_hatlar() -> dict:
    """
    Veri toplama + önemli arka plan iş hatlarının BUGÜNKÜ durumu (kullanıcı isteği
    2026-10-28: "hangisi v2'den hangisi mobilden, hangisinde sıkıntı var").

    ⚠️ EKAP'a istek ATILMAZ: Redis sayaçları + küçük indeksli sorgular (60 sn önbellek).
    Ağır kuyruk sayımları ayrı ve 10 dk önbellekli (`_hesapla_kuyruklar`). Eşikler
    görevlerin kendi aralıklarından türetildi (ör. yüklenici artımlı turu 5 dk'da bir).
    """
    from django.conf import settings
    from django.db.models import Max

    from core.models import AppSetting
    from ekap import session as ekap_session
    from ekap import v2_tazeleme
    from ekap.mobil import captcha as mobil_captcha
    from ekap.mobil import tasks as mobil_tasks
    from ekap.mobil import throttle as mobil_throttle
    from ekap.models import DogrudanTemin, KurulKarari, SyncRun, Tender
    from ekap.tasks import katalog_durumu
    from tenders.models import Notification

    simdi, bas = timezone.now(), bugun_basi()
    m = mobil_tasks.sayaclar()
    butce = mobil_throttle.butce_ozet()
    kuyruk = cache.get_or_set(_K_KUYRUK, _hesapla_kuyruklar, TTL_KUYRUK)
    hatlar = []

    # ── v2 (eski EKAP) ──
    v2 = v2_tazeleme.sayaclar()
    acik = getattr(settings, "EKAP_V2_TAZELEME_ENABLED", False)
    son_v2 = v2_tazeleme.son_is()
    gerekli = ekap_session.dogrulama_gerekli(canli=False)
    if gerekli is None:
        gerekli, _ = ekap_session.son_bilinen_gerekli()
    kapi = ("doğrulama istemiyor" if gerekli is False else
            "doğrulama İSTİYOR — köprü çerezi gerekir" if gerekli else "doğrulama durumu bilinmiyor")
    if not acik:
        sv = "kapali"
    elif v2["hata"] >= 5 and v2["hata"] > v2["tazeleme"] + v2["sonuc"]:
        sv = "hata"
    elif _yasli(son_v2, timedelta(minutes=30), simdi):
        sv = "uyari"
    else:
        sv = "ok"
    hatlar.append(_hat(
        "İhale tazeleme (durum + sonuç)", KAYNAK_V2, sv,
        f"durum {v2['tazeleme']} · sonuç {v2['sonuc']} · v2'de yok {v2['bulunamadi']} · "
        f"hata {v2['hata']} — bekleyen: tazeleme {kuyruk['v2_tazeleme']}, sonuç {kuyruk['v2_sonuc']}",
        son_v2, "EKAP_V2_TAZELEME_ENABLED kapalı" if not acik else f"{kapi}. {v2_tazeleme.son_hata()}"))
    for k in katalog_durumu(simdi):
        if k["donduruldu"]:
            hatlar.append(_hat(k["ad"], KAYNAK_V2, "kapali", "donduruldu (EKAP anonimleştirdi)",
                               k["son_basarili"]))
        else:
            hatlar.append(_hat(k["ad"], KAYNAK_V2, "uyari" if k["eski"] else "ok",
                               f"{k['kayit'] or 0} kayıt (son başarılı tur, haftalık)", k["son_basarili"]))

    # ── mobil API ──
    mobil_acik = getattr(settings, "EKAP_MOBIL_ENABLED", False)
    captcha = mobil_captcha.bekliyor_mu()
    ab = butce.get("arka_plan") or {}
    kes, son_kes = _syncrun_ozet("mobil_kesif", bas)
    son_kes_hata = (SyncRun.objects.filter(task="mobil_kesif", status="error", started_at__gte=bas)
                    .order_by("-started_at").values_list("note", flat=True).first() or "")
    sv = ("kapali" if not mobil_acik else "hata" if captcha else
          "uyari" if _yasli(son_kes, timedelta(hours=5), simdi) else "ok")
    hatlar.append(_hat(
        "Yeni ihale keşfi", KAYNAK_MOBIL, sv,
        f"{kes['tur']} tur · {m.get('kesif', 0)} istek · bütçe {ab.get('kullanilan', 0)}/{ab.get('tavan', 0)}",
        son_kes, mobil_captcha.bekleyen_durum() if captcha else son_kes_hata[:160]))
    son_detay = Tender.objects.filter(detay_kaynak="mobil").aggregate(m=Max("detail_synced_at"))["m"]
    borc = kuyruk["detay_borcu"]
    sv = ("kapali" if not mobil_acik else "hata" if captcha else
          "uyari" if _yasli(son_detay, timedelta(hours=2), simdi) or borc > 300 else "ok")
    hatlar.append(_hat(
        "İhale detayı (ilan tarihi → bildirimler)", KAYNAK_MOBIL, sv,
        f"{m.get('detay', 0)} detay · bekleyen {borc} · captcha {m.get('captcha', 0)} · hata {m.get('hata', 0)}",
        son_detay, "detay borcu yüksek — bildirimler gecikir" if borc > 300 else ""))
    dt_acik = getattr(settings, "EKAP_MOBIL_DT_ENABLED", False)
    dt_yeni = DogrudanTemin.objects.filter(created_at__gte=bas).count()
    son_dt = DogrudanTemin.objects.aggregate(m=Max("son_gorulme"))["m"]
    dtb = butce.get("dt") or {}
    sv = ("kapali" if not dt_acik else
          "uyari" if _yasli(son_dt, timedelta(hours=3), simdi) else "ok")
    hatlar.append(_hat("Doğrudan temin", KAYNAK_MOBIL, sv,
                       f"{dt_yeni} yeni alım · {m.get('dt', 0)} istek · bütçe {dtb.get('kullanilan', 0)}/{dtb.get('tavan', 0)}",
                       son_dt))
    kurul_acik = getattr(settings, "EKAP_MOBIL_KURUL_ENABLED", False)
    k_yeni = KurulKarari.objects.filter(ilk_gorulme__gte=bas).count()
    son_k = KurulKarari.objects.aggregate(m=Max("updated_at"))["m"]
    sv = ("kapali" if not kurul_acik else
          "uyari" if _yasli(son_k, timedelta(hours=30), simdi) else "ok")
    hatlar.append(_hat("KİK kurul kararları", KAYNAK_MOBIL, sv,
                       f"{k_yeni} yeni karar · {m.get('kurul', 0)} istek (günde bir tur, 06:40)", son_k))

    # ── iç işler (EKAP'a gitmez) ──
    for gorev, ad, esik in (("sync_contractors", "Yüklenici bağlantısı", timedelta(hours=1)),
                            ("refresh_market_stats", "Pazar panosu istatistikleri", timedelta(hours=26)),
                            ("detect_recurring_series", "Tekrar eden ihaleler", timedelta(hours=26))):
        o, son_ok = _syncrun_ozet(gorev, bas)
        sv = ("hata" if _yasli(son_ok, esik, simdi) else "uyari" if (o["hata"] or 0) else "ok")
        hatlar.append(_hat(ad, KAYNAK_IC, sv,
                           f"{o['tur']} tur · {o['kayit'] or 0} kayıt · hata {o['hata'] or 0}", son_ok))
    bild = Notification.objects.filter(created_at__gte=bas)
    son_b = Notification.objects.aggregate(m=Max("created_at"))["m"]
    hatlar.append(_hat("Bildirimler", KAYNAK_IC, "ok",
                       f"{bild.count()} bildirim · {bild.values('user').distinct().count()} kullanıcı", son_b))
    y = AppSetting.objects.filter(key="yedek_durumu").first()
    if y is None:
        sv, metin = "uyari", "kayıt yok"
    else:
        metin = y.value[:120]
        sv = ("hata" if not y.value.startswith("TAMAM") else
              "uyari" if simdi - y.updated_at > timedelta(hours=30) else "ok")
    hatlar.append(_hat("Gecelik yedek (FTP)", "sunucu", sv, metin, y.updated_at if y else None))

    # Son 24 saatin hataları (SyncRun yazan görevler) — sebebi `note`'ta
    hatalar = [
        {"gorev": r["task"], "zaman": timezone.localtime(r["started_at"]), "not": (r["note"] or "")[:200]}
        for r in SyncRun.objects.filter(status="error", started_at__gte=simdi - timedelta(hours=24))
        .order_by("-started_at").values("task", "started_at", "note")[:10]
    ]
    return {"hatlar": hatlar, "hatalar": hatalar,
            "sorun": sum(h["seviye"] in ("hata", "uyari") for h in hatlar)}


_K_KUYRUK = f"dash:{CACHE_VERSION}:kuyruk"
TTL_KUYRUK = 600


def _hesapla_kuyruklar() -> dict:
    """Ağır sayımlar (10 dk önbellek): v2 bekleyenleri + detayı gelmemiş yeni ihaleler."""
    from ekap import v2_tazeleme
    from ekap.models import Tender
    simdi = timezone.now()
    return {
        "v2_tazeleme": v2_tazeleme.tazeleme_adaylari(simdi).order_by().count(),
        "v2_sonuc": v2_tazeleme.sonuc_adaylari(simdi).order_by().count(),
        # Kısmi indeks (0009) `detail_synced_at IS NULL`; pencere `created_at` indeksli.
        "detay_borcu": Tender.objects.filter(detail_synced_at__isnull=True,
                                             created_at__gte=simdi - timedelta(days=7)).count(),
    }


# ── Giriş noktası ───────────────────────────────────────
def panel_metrikleri(*, force_refresh: bool = False) -> dict:
    """
    Panonun tüm verisi.

    Üç ayrı cache dilimi: ucuz olan sık (60 sn), grafik serileri orta (5 dk),
    pahalı EKAP yaklaşık sayımları seyrek (1 sa) yenilenir — biri bayatlarken
    diğerinin yeniden hesaplanmasına gerek kalmasın.
    """
    if force_refresh:
        cache.delete_many([_K_OZET, _K_SERI, _K_EKAP, _K_HAT])

    ozet = cache.get_or_set(_K_OZET, _hesapla_ozet, TTL_HIZLI)
    seriler = cache.get_or_set(_K_SERI, _hesapla_seriler, TTL_SERI)
    ekap = cache.get_or_set(_K_EKAP, _hesapla_ekap, TTL_AGIR)

    return {
        "ozet": ozet,
        "seriler": seriler,
        "ekap": ekap,
        # ⚠️ Cache'lenmez: arıza anında panonun 60 sn eski bilgi göstermesi,
        # "toplama duruyor mu" sorusunda kabul edilemez.
        "dogrulama": ekap_dogrulama_durumu(),
        "mobil": ekap_mobil_durumu(),
        "hatlar": cache.get_or_set(_K_HAT, _hesapla_hatlar, TTL_HAT),
        "uretildi": timezone.localtime(),
        # Grafiklere gidecek dilim (json_script ile şablondan JS'e aktarılır).
        "grafik": {
            "kayit_30g": seriler["kayit_30g"],
            "kayit_12ay": seriler["kayit_12ay"],
            "etkilesim_30g": {
                "etiketler": seriler["kayitli_ihale_30g"]["etiketler"],
                "seriler": [
                    {"ad": "Kaydedilen ihale", "degerler": seriler["kayitli_ihale_30g"]["degerler"]},
                    {"ad": "Favori", "degerler": seriler["favori_30g"]["degerler"]},
                    {"ad": "Yeni sohbet", "degerler": seriler["sohbet_30g"]["degerler"]},
                ],
            },
            "abonelik": [
                {"ad": "Pro", "n": ozet["abonelik"]["pro"]},
                {"ad": "Ücretsiz", "n": ozet["abonelik"]["free"]},
            ],
            "saglayici": [
                {"ad": s["ad"], "n": s["n"]} for s in ozet["kullanici"]["saglayicilar"]
            ],
            "bildirim_turu": [
                {"ad": t["ad"], "n": t["n"]} for t in ozet["bildirim"]["turler"]
            ],
        },
    }
