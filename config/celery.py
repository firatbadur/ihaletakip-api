"""Celery uygulaması — config paketi."""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("ihaletakip")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
# ⚠️ **`autodiscover_tasks()` yalnızca `<app>/tasks.py`'ye bakar** (INSTALLED_APPS
# üzerinden). `ekap/mobil/tasks.py` bir ALT PAKETTE olduğu için bulunmaz ve görevler
# worker'da kayıtlı olmaz. Belirti (üretimde ölçüldü 2026-09-10): beat görevi doğru
# tetikliyor (`PeriodicTask.last_run_at` ilerliyor) ama worker
# `Received unregistered task of type 'ekap.mobil.tasks.tik'` + `KeyError` basıyor ve
# **hiçbir iş yapılmıyor**. Kuyruk boş göründüğü için arıza "çalışmıyor ama sebebi yok"
# gibi görünür → paket açıkça verilir.
app.autodiscover_tasks(["ekap.mobil"])

# ── Celery Beat — periyodik görevler ───────────────────
app.conf.beat_schedule = {
    # Eski AI analiz cache kayıtlarını temizle (her gün 03:00)
    "cleanup-expired-analyses": {
        "task": "ai.tasks.cleanup_expired_analyses",
        "schedule": crontab(hour=3, minute=0),
        "kwargs": {"days": 30},
    },
    # Asistan onay kartları: süresi dolanları işaretle, 90 günden eskileri sil (03:15)
    "assistant-expire-actions": {
        "task": "assistant.tasks.expire_actions",
        "schedule": crontab(hour=3, minute=15),
    },
    # ── Bildirim servisi (kademeli düzen, kullanıcı başına tek özet push) ──
    # İhale Asistanı: günlük öneri digest'i + push (her gün 07:00 — ekap sync sonrası)
    "assistant-match-recommendations": {
        "task": "assistant.tasks.match_recommendations",
        "schedule": crontab(hour=7, minute=0),
    },
    # OKAS önerisi: kayıtlı ihalelerin OKAS kodlarıyla son 24s yayınlanan ihaleler
    # (her gün 08:00 — Free/Pro herkese; asistan önerisinden ayrı, premium değil)
    "recommend-by-saved-okas": {
        "task": "tenders.tasks.recommend_by_saved_okas",
        "schedule": crontab(hour=8, minute=0),
    },
    # İhale alarmları: ihale günü / doküman değişikliği / sonuçlandı (her gün 09:00)
    "check-tender-alarms": {
        "task": "tenders.tasks.check_tender_alarms",
        "schedule": crontab(hour=9, minute=0),
    },
    # Kayıtlı filtre eşleşmesi: filtreye uyan yeni ihaleler (her gün 10:00)
    # ⚠️ **Günde tek tur YETMEZ** — EKAP'ın yayım saati bilinmiyor ve `ilan_tarihi`
    # damgası gün başı olduğu için veriden de okunamıyor. Sabit tek saatte koşan bir
    # bildirim, o saatten sonra yayımlanan her ihaleyi kaçırır ve ertesi gün "bugün"
    # olmadıkları için hiç bildirmez. Gün içine yayıldı; mükerrerliği zaman değil
    # ihale bazlı dedup engelliyor (`tenders.tasks._yeni_ihaleler`).
    # ⚠️ Saatler `sync_recent`'in (tek saatler) **bir saat sonrasına** konumlandı:
    # 09:00 turu → 10:00 bildirimi. Aynı saatte başlasalardı bildirim, o turun
    # yazdığı ihaleleri göremeden koşardı.
    "check-saved-filter-matches": {
        "task": "tenders.tasks.check_saved_filter_matches",
        "schedule": crontab(minute=0, hour="10,14,18"),
    },
    # Favori idare eşleşmesi: favori idarelerin yeni yayınladığı ihaleler (her gün 11:00)
    # Filtre turundan bir saat sonra — kategori başına staggered saat, aynı anda
    # iki kategoriden push yağmasın (bkz. "Bildirim Servisi" pacing kuralları).
    "check-favorite-authority-matches": {
        "task": "tenders.tasks.check_favorite_authority_matches",
        "schedule": crontab(minute=0, hour="11,15,19"),
    },
    # 30 günden eski okunmuş bildirimleri temizle (her gün 04:00)
    "cleanup-old-notifications": {
        "task": "tenders.tasks.cleanup_old_notifications",
        "schedule": crontab(hour=4, minute=0),
        "kwargs": {"days": 30},
    },
    # ── EKAP veri toplama ──────────────────────────────
    # Güncel ihaleler — her gece 02:00
    # ⚠️ **Günde tek tur YETMEZ — ölçüldü (2026-08-27).** Eskiden `hour=2` idi.
    # EKAP gün içinde ilan eklemeye devam ediyor: 02:00 turunda o güne ait 0 kayıt
    # varken saat 11:22'de EKAP'ta 95 ihale vardı. Sonuç: "bugün yayınlananlar"a
    # bakan filtre/idare bildirimleri **yapısal olarak** boş küme üzerinde koşuyordu
    # — günün ihaleleri DB'ye ancak ertesi gece giriyor, o zaman da "bugün" değil
    # "dün" oluyorlar. Belirti aldatıcıydı: `SyncRun` her gece `ok`, `items=719`.
    # ⚠️ **TEK sayılı saatler** (1,3,…,23): bildirim görevleri 10:00 (filtre) ve
    # 11:00'de (idare) koşuyor; 09:00 turu onlara taze veri bırakır. Çift saatlerde
    # 10:00 turu bildirimle **aynı anda** başlar ve yarışırdı.
    # ⚠️ Maliyet önemsiz: pencere ~700 kayıt = 15 liste isteği (50/istek), 12 tur =
    # 180 istek/gün. Asıl pahalı olan detay istekleridir; onlar da `only_if_missing`
    # sayesinde yalnızca **yeni** ihaleler için atılıyor (bkz. sync_recent).
    # ── EKAP MOBİL (birincil kaynak) ───────────────────────────────────────
    # ⚠️ **Çekmeli tek tüketici**: `tik` her turda TEK iş seçer ve TEK istek harcar
    # (bkz. ekap/mobil/tasks.py). İtmeli fan-out bu bütçede (~1 istek/2-3 dk) kuyruğu
    # bayat görevle doldururdu — 2026-08-11'de `ekap` kuyruğunda ölçülen 218.443
    # görev / 159.801 gerçek iş arızası.
    # ⚠️ Aralık `EKAP_MOBIL_TIK_DK` ile uyumlu tutulmalı: beat'in tetiklemesi hız
    # penceresinden SIK olursa turlar bedavaya "hiz_penceresi" deyip çıkar (zararsız),
    # SEYREK olursa bütçe boşa gider.
    # ⚠️ `EKAP_MOBIL_ENABLED=False` iken görev ilk satırda döner → beat girdisi
    # açık kalabilir, kill switch ayardadır.
    "ekap-mobil-tik": {
        "task": "ekap.mobil.tasks.tik",
        "schedule": crontab(minute="*/2"),
    },
    # ⚠️ **Artık günde BİR kez (06:00).** Aşağıdaki "gün içinde koşmalı" gerekçesi
    # v2 birincil kaynakken geçerliydi; birincil kaynak artık mobil API ve gün içi
    # tazelik oradan geliyor. v2 turu yalnızca **yedek/karşılaştırma** amaçlı:
    # mobilde olmayan alanları (idare_id, ozellikler, kısım listesi) taze tutar.
    # Turnstile çerezi yoksa görev zaten bedavaya çıkar (`_dogrulama_kapisi`).
    "ekap-sync-recent": {
        "task": "ekap.tasks.sync_recent",
        "schedule": crontab(minute=0, hour=6),
    },
    # Akıllı detay yenileme — her 3 saatte bir (yalnızca son 1 yıl; EKAP_REFRESH_YEARS)
    "ekap-refresh-stale": {
        "task": "ekap.tasks.refresh_stale",
        "schedule": crontab(minute=30, hour="*/3"),
    },
    # Geçmiş doldurma (backfill) — TÜM GÜN, 15 dk'da bir. 5 yıl tabanına ulaşınca
    # görev DB kontrolüyle anında döner → boşta maliyeti yok. EKAP gün içinde
    # yavaş/yanıtsız olabildiğinden görev sayfa hatasını zarifçe yutar (kısmi
    # ilerlemeyi kaydeder, sonraki tetikte kaldığı yerden devam eder). Kilit (1 sa)
    # üst üste binmeyi, throttle (~1 istek/sn) + tek concurrency EKAP'ı korur.
    # Pazar panosu özetleri — her gece 01:30. EKAP'a gitmez, `detail_raw` OKUMAZ
    # (`.values().annotate()`) → süpürme penceresiyle çakışmaz. Ölçülen süre ~4 sn.
    "ekap-refresh-market-stats": {
        "task": "ekap.tasks.refresh_market_stats",
        "schedule": crontab(hour=1, minute=30),
    },
    # ⚠️ **`ekap-backfill` KALDIRILDI (2026-09-10).** Arşiv doldu: pencere
    # (`EKAP_BACKFILL_YEARS`) taranıp bitti, görev her turda "done" dönüyordu ve
    # yalnızca `SyncRun` satırı üretiyordu. Görevin kendisi (`ekap.tasks.backfill`)
    # duruyor — pencere genişletilirse `run_ingest --task backfill` ile elle ya da
    # buraya geri eklenerek çalıştırılabilir.
    # ⚠️ Kodda girdiyi silmek YETMEZ: `DatabaseScheduler` DB'deki `PeriodicTask`
    # satırını kendiliğinden kaldırmaz → satır ayrıca **devre dışı bırakılmalıdır**
    # (admin → Periodic Tasks, ya da `PeriodicTask.objects.filter(
    #  name="ekap-backfill").update(enabled=False)`).
    # OKAS kodları — haftalık (Pazartesi 05:00)
    # Sözleşmeleri firmalara bağlar. EKAP'a gitmez (detail_raw arşivinden çalışır) →
    # `celery` kuyruğuna yönlendirilir (bkz. settings.CELERY_TASK_ROUTES) ve EKAP
    # worker'ını bloklamaz. Redis kilidi üst üste binmeyi önler.
    # ⚠️ Gündüz koruması **saat penceresiyle** yapılır, aralıkla değil: süpürme yalnızca
    # 00:00-07:00'de çalışır (`CONTRACTOR_SWEEP_START/END`), gündüz görev anında
    # "atlandı" dönüp çıkar (bedava). Bu yüzden 5 dk aralık güvenlidir ve gece penceresini
    # doldurur — 10 dk × 90 sn kurgusu gecenin %85'ini boşa harcıyordu.
    # Artımlı mod (süpürme bitince) her saat çalışır ama kısa bütçelidir (90 sn).
    "ekap-sync-contractors": {
        "task": "ekap.tasks.sync_contractors",
        "schedule": crontab(minute="*/5"),
    },
    # Pro sinyal kolonlarını (OKAS ana kodu, bakanlık, istekli sayısı, şikâyet bayrakları,
    # seri anahtarı) `detail_raw` arşivinden doldurur. EKAP'a gitmez → `celery` kuyruğu.
    # ⚠️ Yüklenici süpürmesi bitene kadar her tetikte "atlandı" dönüp bedava çıkar
    # (ikisi de detail_raw okuyor; aynı gecede koşarlarsa ikisi de yarı hızda ilerler).
    # Süpürme bitince kendiliğinden devralır. 5 dk aralık gece penceresini doldurur.
    "ekap-backfill-tender-fields": {
        "task": "ekap.tasks.backfill_tender_fields",
        "schedule": crontab(minute="*/5"),
    },
    # Takip edilen firmalar yeni iş aldığında (12:00 — kademeli bildirim dizisinin devamı:
    # 07 öneri, 08 OKAS, 09 alarm, 10 filtre, 11 idare, 12 firma). Pro'ya özeldir.
    "tenders-check-favorite-contractors": {
        "task": "tenders.tasks.check_favorite_contractor_matches",
        "schedule": crontab(hour=12, minute=0),
    },
    # Tekrar eden ihale serilerini tespit eder (Pazar 02:30, haftalık).
    # EKAP'a gitmez ve detail_raw OKUMAZ (.values() ile indeksli seri_anahtar üzerinde
    # GROUP BY) → gece penceresi/süpürme çakışması sorunu yok. `celery` kuyruğu.
    "ekap-detect-recurring-series": {
        "task": "ekap.tasks.detect_recurring_series",
        "schedule": crontab(hour=2, minute=30, day_of_week=0),
    },
    # Ücretsiz üyeye HAFTADA BİR "bu hafta neyi kaçırdın" özeti (Pazartesi 10:00).
    # Günlük alarm görevleri Free kullanıcıyı sayılmadan eliyor → kullanıcı Pro'nun ne işe
    # yaradığını hiç hissetmiyordu. Yalnızca SAYI üretir (liste değil), sıfır eşleşmede
    # bildirim göndermez. Pro kullanıcılar zaten günlük bildirim aldığı için atlanır.
    "tenders-weekly-free-teaser": {
        "task": "tenders.tasks.weekly_free_teaser",
        "schedule": crontab(hour=10, minute=0, day_of_week=1),
    },
    # "İhalede geçen idare_id" kümesini sıcak tutar (idare_detsis filtresinin kesişimi).
    # ⚠️ İstek yolunda hesaplanınca ~40 sn sürüyordu; 10 dk'da bir tazelenince cache
    # (TTL 30 dk) hiç boşalmaz ve kullanıcı bu maliyeti hiç ödemez. EKAP'a gitmez →
    # `celery` kuyruğu (bkz. settings.CELERY_TASK_ROUTES).
    "ekap-refresh-idare-id-set": {
        "task": "ekap.tasks.refresh_idare_id_set",
        "schedule": crontab(minute="*/10"),
    },
    "ekap-sync-okas": {
        "task": "ekap.tasks.sync_okas",
        "schedule": crontab(hour=5, minute=0, day_of_week=1),
    },
    # DETSIS kurumlar — haftalık (Pazartesi 05:30)
    "ekap-sync-authorities": {
        "task": "ekap.tasks.sync_authorities",
        "schedule": crontab(hour=5, minute=30, day_of_week=1),
    },

    # ── Anahtar kelime boru hattı ───────────────────────
    # Hiçbiri EKAP'a gitmez → `celery` kuyruğu (bkz. settings.CELERY_TASK_ROUTES).
    # ⚠️ `KEYWORD_AI_ENABLED=False` iken dispatch hiçbir şey göndermez; beat
    # girdilerinin açık olması harcama BAŞLATMAZ.

    # Kalıp çıkarma — `detail_raw`'a dokunmaz (kaynak `ihale_adi`, satır içi), bu
    # yüzden gündüz de koşabilir ve süpürmeyle cache yarışına girmez.
    "ekap-backfill-tender-kalip": {
        "task": "ekap.tasks.backfill_tender_kalip",
        "schedule": crontab(minute="*/10"),
    },
    # ⚠️ 6 saatte bir: eşzamanlı batch tavanı 2 ve bir batch 24 saate kadar
    # sürebiliyor; daha sık tetiklemek yalnızca "inflight_limit" ile geri döner.
    "ekap-dispatch-keyword-batches": {
        "task": "ekap.tasks.dispatch_keyword_batches",
        "schedule": crontab(minute=5, hour="*/6"),
    },
    "ekap-poll-keyword-batches": {
        "task": "ekap.tasks.poll_keyword_batches",
        "schedule": crontab(minute="*/15"),
    },
    "ekap-process-keyword-results": {
        "task": "ekap.tasks.process_keyword_results",
        # ⚠️ Tur bütçesi 240 sn ve tur başına yalnızca BİR batch ilerler (bkz.
        # `process_keyword_results`). 50.000 kalıplık batch ~10 tur ister; 15 dakikalık
        # aralıkta bu 2,5 saat sürer ve altı batch birikince kuyruk günlere yayılır.
        # 5 dakikada bir tetikleme aynı işi ~3 kat hızlı bitirir; görev tamamen DB-içi
        # olduğu için EKAP'a yük binmez, `celery` kuyruğunda koşar.
        "schedule": crontab(minute="*/5"),
    },
    # ⚠️ Tek yazma-ağır aşama → gece penceresi görevin kendi içinde uygulanır
    # (`KEYWORD_PROPAGATE_START/END`); beat sık tetikler, görev kendini eler.
    # ⚠️ 10 dk aralık gece penceresinin **%60'ını boşa harcıyordu**: tur bütçesi 240 sn,
    # aralık 600 sn → duty cycle %40. Ölçüm (2026-09-09): 7 saatlik pencerede imleç
    # yalnızca 335k ihaleye ulaştı, kalan 761k için 2-3 gece daha gerekiyordu.
    # 5 dk aralıkta duty cycle %80 → yaklaşık iki kat hız. Pencere kontrolü görevin
    # İÇİNDE olduğu için (gündüz anında "peak_hours" dönüp bedava çıkar) sık tetikleme
    # güvenli — `sync_contractors`'ta da aynı desen kullanılıyor.
    "ekap-propagate-tender-keywords": {
        "task": "ekap.tasks.propagate_tender_keywords",
        "schedule": crontab(minute="*/5"),
    },
    # df + `pasif` bayrağı = benzerlik sorgusunun performans regülatörü.
    "ekap-refresh-keyword-df": {
        "task": "ekap.tasks.refresh_keyword_df",
        "schedule": crontab(hour=4, minute=30),
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
