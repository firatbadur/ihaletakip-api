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
            "push_acik": kullanicilar.exclude(fcm_token="").count(),
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

    kayit = AppSetting.objects.filter(key=ekap_session.ANAHTAR_DURUM).first()
    metin = kayit.value if kayit else ""
    dustu = bool(cache.get("ekap:dogrulama:dustu"))
    var = bool(ekap_session.cerez())
    if dustu or metin.startswith("DÜŞTÜ"):
        seviye = "hata"
    elif var:
        seviye = "ok"
    else:
        seviye = "yok"
    return {
        "seviye": seviye,
        "metin": metin or "hiç yapılandırılmadı",
        "guncelleme": timezone.localtime(kayit.updated_at) if kayit else None,
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
        cache.delete_many([_K_OZET, _K_SERI, _K_EKAP])

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
