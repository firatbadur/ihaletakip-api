"""
IhaleTakip API — Django ayarları.

Ortam değişkenleri .env dosyasından okunur (django-environ).
Üretimde tüm sırlar env üzerinden gelmelidir.
"""
from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Ortam ──────────────────────────────────────────────
env = environ.Env(
    DJANGO_DEBUG=(bool, False),
)
# .env varsa oku (yoksa sistem env'i kullanılır)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="dev-insecure-key-change-me-in-production-0123456789abcdef",
)
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# ── Uygulamalar ────────────────────────────────────────
DJANGO_APPS = [
    # jazzmin, admin şablonlarını override ettiği için admin'den ÖNCE gelmeli
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "drf_spectacular",
    "django_celery_beat",
    "django_celery_results",
]

LOCAL_APPS = [
    "accounts",
    "tenders",
    "ai",
    "assistant",
    "core",
    "ekap",
    "subscription",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ── Middleware ─────────────────────────────────────────
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ── Veritabanı ─────────────────────────────────────────
# DATABASE_URL varsa onu kullan; yoksa yerel SQLite'a düş.
DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}
# Kalıcı bağlantı: Django varsayılanı 0'dır, yani **her HTTP isteği** yeni TCP + auth
# el sıkışması yapar. Postgres'te bu, hızlı bir arama sorgusunun yanına sabit ~5-15 ms
# ekler. ⚠️ Toplam bağlantı = CONN_MAX_AGE × (gunicorn worker + celery worker + beat);
# Postgres `max_connections`'ı aşmamalı (docker-compose'da 200'e çıkarıldı).
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DJANGO_CONN_MAX_AGE", default=60)
# Kalıcı bağlantı kullanırken şart: worker'ın elindeki bağlantı (DB restart, ağ kesintisi)
# ölmüşse istek başında sessizce yenilenir, 500 dönmez.
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# ── Auth ───────────────────────────────────────────────
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── Uluslararasılaştırma ───────────────────────────────
LANGUAGE_CODE = "tr"
TIME_ZONE = "Europe/Istanbul"
USE_I18N = True
USE_TZ = True

# ── Statik / Medya ─────────────────────────────────────
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]  # marka logoları + admin özel CSS
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Geliştirmede (DEBUG=True) düz storage: collectstatic gerektirmez, manifest aramaz.
# Üretimde whitenoise + manifest (hash'li dosya adları, uzun cache).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "core.storage.JazzminManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── DRF ────────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # BrowsableAPIRenderer yalnızca geliştirmede: üretimde her yanıt için HTML sayfası +
    # form üretebilecek durumda durmasının bir faydası yok, maliyeti var.
    "DEFAULT_RENDERER_CLASSES": (
        ("core.renderers.EnvelopeJSONRenderer", "rest_framework.renderers.BrowsableAPIRenderer")
        if DEBUG
        else ("core.renderers.EnvelopeJSONRenderer",)
    ),
    "EXCEPTION_HANDLER": "core.exceptions.custom_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "IhaleTakip API",
    "DESCRIPTION": "Kamu ihalesi takip uygulaması backend servisi",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ── JWT ────────────────────────────────────────────────
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

# ── CORS ───────────────────────────────────────────────
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])

# ── Celery ─────────────────────────────────────────────
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="django-db")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# ── Cache (Redis) ──────────────────────────────────────
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://localhost:6379/0"),
    }
}

# ── Uygulama servis ayarları ───────────────────────────
# Claude / Anthropic
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
# Genel/analiz + profil haritası modeli (kalite öncelikli, seyrek çalışır)
CLAUDE_MODEL = env("CLAUDE_MODEL", default="claude-sonnet-5")
CLAUDE_MAX_TOKENS = env.int("CLAUDE_MAX_TOKENS", default=3000)
# İhale Asistanı sohbet modeli (sık çalışır → ucuz model + kısa çıktı ile token tasarrufu)
CLAUDE_CHAT_MODEL = env("CLAUDE_CHAT_MODEL", default="claude-haiku-4-5")
CLAUDE_CHAT_MAX_TOKENS = env.int("CLAUDE_CHAT_MAX_TOKENS", default=1000)
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Google Sign-In (idToken audience'ları)
GOOGLE_CLIENT_IDS = env.list("GOOGLE_CLIENT_IDS", default=[])

# Apple Sign-In
APPLE_CLIENT_ID = env("APPLE_CLIENT_ID", default="com.envisoft.ihaletakip")

# Text-to-Speech
TTS_LANGUAGE_CODE = env("TTS_LANGUAGE_CODE", default="tr-TR")
TTS_VOICE_NAME = env("TTS_VOICE_NAME", default="tr-TR-Standard-A")
TTS_MAX_CHARS = 5000

# FCM Push (opsiyonel — kimlik yoksa push no-op, uygulama-içi bildirim satırı yazılır)
FCM_CREDENTIALS = env("FCM_CREDENTIALS", default="")
FCM_PROJECT_ID = env("FCM_PROJECT_ID", default="")

# ── RevenueCat abonelik (Pro) ──────────────────────────
# app_user_id = str(user.id). `subscription/verify` (mobil→backend, senkron) ve
# `subscription/revenuecat-webhook` (RC→backend, async) RC v2 REST ile aktif
# entitlement'ı çeker ve accounts.User.subscription_tier/expires'i senkronlar.
# SECRET_KEY boşsa uçlar 502 döner (kimlik yok). WEBHOOK_AUTH RC panelindeki
# webhook Authorization başlığıyla birebir eşleşmeli.
REVENUECAT_SECRET_KEY = env("REVENUECAT_SECRET_KEY", default="")
REVENUECAT_PROJECT_ID = env("REVENUECAT_PROJECT_ID", default="")
REVENUECAT_ENTITLEMENT = env("REVENUECAT_ENTITLEMENT", default="pro")
REVENUECAT_WEBHOOK_AUTH = env("REVENUECAT_WEBHOOK_AUTH", default="")

# ── Bildirim pacing (kullanıcıyı bombalamamak için) ────
# Sessiz saatler: bu aralıkta (yerel saat) push atılmaz; uygulama-içi satır yine yazılır.
# Varsayılan 22:00–07:00. Zamanlanmış görevler 07/09/10'da çalıştığı için normal akışı
# etkilemez → bu bir güvenlik ağıdır (elle/kaza tetiklemelere karşı).
NOTIF_QUIET_START_HOUR = env.int("NOTIF_QUIET_START_HOUR", default=22)
NOTIF_QUIET_END_HOUR = env.int("NOTIF_QUIET_END_HOUR", default=7)
# Kullanıcı başına gün içinde en fazla bu kadar push (uygulama-içi satır limitten muaf).
# 0/negatif → SINIRSIZ. Abonelik-başına ayrı bildirim (her filtre/idare/alarm için ayrı push)
# olduğundan yüksek tutulur → meşru push'lar düşmez; çoğalma abonelik-başına gün-kilidiyle önlenir.
NOTIF_DAILY_CAP = env.int("NOTIF_DAILY_CAP", default=50)
# Son push'tan bu kadar dakika geçmeden aynı kullanıcıya yeni push atılmaz. 0 → KAPALI.
# Abonelik push'ları aynı görev turunda arka arkaya gittiğinden 0'dır (30 dk olsa 2.+ push düşerdi).
NOTIF_MIN_GAP_MINUTES = env.int("NOTIF_MIN_GAP_MINUTES", default=0)
# NOT: `NOTIF_FILTER_PUBLISH_DAYS` KALDIRILDI — hiçbir yerde okunmuyordu. Kayıtlı filtre ve
# favori idare bildirimleri artık yalnızca `ilan_tarihi` BUGÜN olan ihaleleri bildiriyor
# (bkz. tenders/tasks.py). Ayarın durması, var olmayan bir düğmeyi ayarlanabilir gösteriyordu.
# OKAS önerisi (recommend_by_saved_okas): kayıtlı ihalelerin OKAS kodlarıyla yalnızca
# son bu kadar günde YAYINLANAN ihaleler önerilir (vars. 1 gün = "bugün yayınlanan").
NOTIF_OKAS_PUBLISH_DAYS = env.int("NOTIF_OKAS_PUBLISH_DAYS", default=1)

# ── EKAP veri toplama ──────────────────────────────────
EKAP_BASE_URL = env("EKAP_BASE_URL", default="https://ekapv2.kik.gov.tr")
# AES-192 imzalama anahtarı (mobil calls.js ile aynı olmalı)
EKAP_SIGNING_KEY = env("EKAP_SIGNING_KEY", default="Kj9PxV3sM5wE7tC2zY1bR8qL")
EKAP_MIN_INTERVAL_MS = env.int("EKAP_MIN_INTERVAL_MS", default=1000)  # ~1 istek/sn
# TLS parmak izi engelini aşmak için curl_cffi tarayıcı taklidi
EKAP_IMPERSONATE = env("EKAP_IMPERSONATE", default="chrome")
EKAP_TIMEOUT = env.int("EKAP_TIMEOUT", default=30)
EKAP_MAX_RETRIES = env.int("EKAP_MAX_RETRIES", default=4)
EKAP_RECENT_DAYS = env.int("EKAP_RECENT_DAYS", default=3)
EKAP_BACKFILL_YEARS = env.int("EKAP_BACKFILL_YEARS", default=5)
# `backfill` turu başına çekilecek liste sayfası. ⚠️ **Geçmiş toplama hızının asıl
# düğmesi budur, throttle değil** — ölçüm (2026-08): tur başına tam 500 kayıt
# (10 sayfa × 50) işleniyor ve `ekap` kuyruğu BOŞ, yani 1 istek/sn bütçesi
# kullanılmıyordu. Liste taraması ucuz (50 kayıt/istek); pahalı olan detay istekleri
# ama onlar zaten yalnızca detayı eksik ihaleler için atılıyor.
# ⚠️ Üst sınır: tur maliyeti ≈ `max_pages` saniye + upsert süresi; `CELERY_TASK_TIME_LIMIT`
# 300 sn olduğu için 40 civarı güvenli tavandır.
EKAP_BACKFILL_MAX_PAGES = env.int("EKAP_BACKFILL_MAX_PAGES", default=10)
# `backfill` turu için süre bütçesi (sn) — `max_pages`'ten BAĞIMSIZ güvenlik ağı.
# ⚠️ `CELERY_TASK_TIME_LIMIT=300`'ün altında kalmalı. Ölçüm 2026-08-11: `max_pages=40`
# ile turlar 3-5 dk sürüyor, yani sınırın dibinde; aşan turlar öldürülüyor, `finally`
# çalışmadığı için Redis kilidi TTL'i dolana kadar kalıyor ve görev 15 dk yerine
# saatte bir koşuyordu. Bütçe dolunca döngüden temiz çıkılır ve imleç kaydedilir.
EKAP_BACKFILL_MAX_SECONDS = env.int("EKAP_BACKFILL_MAX_SECONDS", default=240)
# refresh_stale yalnızca son bu kadar yılın ihalelerinin detayını yeniler
EKAP_REFRESH_YEARS = env.int("EKAP_REFRESH_YEARS", default=1)
# `sync_contractors.enqueue_missing_detail` turu başına kuyruğa atılacak eksik detay.
# ⚠️ **Detay borcunu eritme hızının asıl düğmesi budur** — `max_pages`'in ikizi.
# Ölçüm (2026-08-11): 167.965 ihalenin detayı eksikti, `LLEN ekap = 0` (kuyruk BOŞ,
# 5 worker boşta bekliyor) ve gerçekleşen hız 2.273 detay/saat — throttle tavanının
# (3.600/saat) yalnızca %63'ü. Sebep: bu dal tek besleyiciydi ve 50×12 tur = 600/saat
# besliyordu. `backfill` imleci detayı zaten dolu yıllara geldiği için 0 besliyor,
# `refresh_stale` ise yalnızca son `EKAP_REFRESH_YEARS` yıla bakıyor → eski arşiv
# boşluğunu (2019/2020) yalnızca bu dal kapatabiliyor.
# 600 × 12 tur = 7.200/saat, tavanın ~2 katı → kuyrukta daima küçük bir tampon kalır,
# worker'lar hiç boşta beklemez. Fazlası throttle'da erir; **EKAP yükü değişmez**.
EKAP_MISSING_DETAIL_LIMIT = env.int("EKAP_MISSING_DETAIL_LIMIT", default=600)

# Arama uçlarının `totalCount` cache süresi (sn). COUNT soğuk buffer cache'te pahalıdır
# (500k satır + GIN indeksleri); bu TTL soğuk yola düşme sıklığını doğrudan belirler.
# `totalCount` yalnızca bir ilerleme göstergesidir, sayfa içeriği hep canlı sorgudur.
SEARCH_COUNT_CACHE_TTL = env.int("SEARCH_COUNT_CACHE_TTL", default=600)

# `sync_contractors` SÜPÜRME modunun çalışabileceği saat aralığı (yerel saat, [start, end)).
# Süpürme tüm `detail_raw` arşivini okur ve küçük bir `shared_buffers`'ı boşaltarak arama
# sorgularını diske düşürür → gündüz çalıştırılmaz. Artımlı mod bu pencereden bağımsızdır.
# ⚠️ **Süpürmeyi hızlandırmanın tek gerçek kaldıracı BU PENCEREDİR**, tur bütçesi değil:
# üretimde tur başına ortalama 180 sn ölçüldü (bütçe 270 sn), yani bütçe hiç sınırlayıcı
# değil. 7 saatlik pencere günde ~2,5 saat iş demek; `END=24` yapmak bunu ~20 saate
# çıkarıp süpürmeyi ~9× hızlandırır.
# Bedeli: `ekap_tender` 7,5 GB ve `shared_buffers` 2 GB → süpürme cache'i boşaltıyor
# (ölçüm: heap isabeti %79). Pencere açılırsa bu gündüz de olur, arama ucu yavaşlar.
# Geçici hızlandırma için `.env.prod`'da açıp iş bitince geri almak doğru kullanımdır.
CONTRACTOR_SWEEP_START = env.int("CONTRACTOR_SWEEP_START", default=0)
CONTRACTOR_SWEEP_END = env.int("CONTRACTOR_SWEEP_END", default=7)
# Tur başına süre bütçesi (sn). Süpürme yalnız gece koştuğu için pencereyi doldurabilir;
# artımlı mod gündüz de koştuğundan kısa kalır. ⚠️ İkisi de CELERY_TASK_TIME_LIMIT=300'ün
# ALTINDA olmalı, yoksa görev yarıda kesilir (imleç kaydedilmez, tur boşa gider).
CONTRACTOR_SWEEP_MAX_SECONDS = env.int("CONTRACTOR_SWEEP_MAX_SECONDS", default=270)
CONTRACTOR_INCREMENTAL_MAX_SECONDS = env.int("CONTRACTOR_INCREMENTAL_MAX_SECONDS", default=90)

# `backfill_tender_fields` (Pro sinyal kolonları) — aynı gerekçe, aynı pencere deseni.
# ⚠️ Bu görev `sync_contractors` SÜPÜRMESİ bitene kadar kendini geri çeker: ikisi de
# arşivin `detail_raw`'ını okuyor, aynı gecede koşarlarsa ikisi de yarı hızda ilerler.
PRO_BACKFILL_START = env.int("PRO_BACKFILL_START", default=0)
PRO_BACKFILL_END = env.int("PRO_BACKFILL_END", default=7)
PRO_BACKFILL_MAX_SECONDS = env.int("PRO_BACKFILL_MAX_SECONDS", default=270)

# EKAP görevleri ayrı, tek-concurrency'li kuyrukta serileştirilir (EKAP ~1 istek/sn).
# ⚠️ Sıra önemli: Celery ilk eşleşen deseni kullanır, bu yüzden istisnalar `ekap.tasks.*`
# joker'inden ÖNCE gelmelidir.
CELERY_TASK_ROUTES = {
    # sync_contractors EKAP'a HİÇ gitmez (Tender.detail_raw arşivinden çalışır) →
    # tek-concurrency'li ekap kuyruğunda yer tutup sync_recent/backfill'i bloklamasın.
    "ekap.tasks.sync_contractors": {"queue": "celery"},
    "ekap.tasks.refresh_idare_id_set": {"queue": "celery"},
    # backfill_tender_fields de EKAP'a gitmez (detail_raw arşivinden türetir).
    "ekap.tasks.backfill_tender_fields": {"queue": "celery"},
    "ekap.tasks.detect_recurring_series": {"queue": "celery"},
    "ekap.tasks.refresh_market_stats": {"queue": "celery"},
    # ⚠️ **Zamana duyarlı EKAP görevleri AYRI kuyrukta** (`ekap_oncelik`).
    # Ölçüm 2026-08-11: `sync_recent` beat'te 02:00'de doğru tetikleniyordu
    # (`PeriodicTask.last_run_at` = 23:00 UTC ✓) ama `SyncRun.started_at` 9-20 saat
    # sonraydı — görev tek FIFO `ekap` kuyruğunda on binlerce `sync_detail` arşiv
    # görevinin arkasına düşüyor ve 1 istek/sn tavanında kuyruk saatlerce erimiyordu.
    # Belirti: "gece 02:00'de çekmesi gereken veri gün ortasında geliyor / hiç gelmiyor".
    # Ayrı kuyruk + ayrı worker bunu keser. **EKAP'a giden yük DEĞİŞMEZ**: throttle
    # Redis'te global slot rezervasyonu yapar, kaç worker olursa olsun tavan aynıdır.
    # ⚠️ Bu satırlar `ekap.tasks.*` joker'inden ÖNCE gelmeli.
    "ekap.tasks.sync_recent": {"queue": "ekap_oncelik"},
    "ekap.tasks.refresh_stale": {"queue": "ekap_oncelik"},
    "ekap.tasks.sync_okas": {"queue": "ekap_oncelik"},
    "ekap.tasks.sync_authorities": {"queue": "ekap_oncelik"},
    "ekap.tasks.*": {"queue": "ekap"},
}

# ── Admin arayüzü (Jazzmin) ────────────────────────────
JAZZMIN_SETTINGS = {
    "site_title": "IhaleTakip API",
    "site_header": "IhaleTakip",
    "site_brand": "İhale Takip",
    # Marka varlıkları — static/ihaletakip/ (mobil uygulamanın logo setinden)
    "site_logo": "ihaletakip/icon-white.svg",   # sidebar (koyu zemin)
    "site_logo_classes": "",                    # yuvarlatma yok, yatay logo
    "login_logo": "ihaletakip/logo.svg",        # giriş — açık tema
    "login_logo_dark": "ihaletakip/logo-white.svg",  # giriş — koyu tema
    "site_icon": "ihaletakip/favicon.png",
    "welcome_sign": "IhaleTakip Yönetim Paneli",
    "copyright": "Envisoft",
    "search_model": ["ekap.Tender"],  # üst bardaki hızlı arama
    # user_avatar tanımlanırsa jazzmin varsayılan bir profil resmi basar — istemiyoruz
    "user_avatar": None,
    # ── Üst menü ──
    "topmenu_links": [
        {"name": "Panel", "url": "admin:index", "permissions": ["auth.view_user"]},
        {"name": "API Dokümanı", "url": "/api/docs/", "new_window": True},
        {"name": "Sağlık", "url": "/health/", "new_window": True},
        {"model": "accounts.User"},
        {"app": "ekap"},
    ],
    # ── Kullanıcı menüsü (sağ üst) ──
    "usermenu_links": [
        {"name": "API Dokümanı", "url": "/api/docs/", "new_window": True},
        {"model": "accounts.user"},
    ],
    # ── Kenar çubuğu ──
    "show_sidebar": True,
    "navigation_expanded": False,
    "hide_apps": [],
    "hide_models": [],
    "order_with_respect_to": [
        "ekap",
        "tenders",
        "accounts",
        "ai",
        "core",
        "django_celery_beat",
        "django_celery_results",
        "auth",
        "token_blacklist",
    ],
    # ── İkonlar (FontAwesome 5 free) ──
    "icons": {
        "auth": "fas fa-shield-alt",
        "auth.Group": "fas fa-users",
        "accounts": "fas fa-id-badge",
        "accounts.User": "fas fa-user",
        "ekap": "fas fa-gavel",
        "ekap.Tender": "fas fa-gavel",
        "ekap.Contract": "fas fa-file-signature",
        "ekap.ContractSection": "fas fa-layer-group",
        "ekap.Contractor": "fas fa-industry",
        "ekap.ContractorAlias": "fas fa-tags",
        "ekap.ContractorMembership": "fas fa-handshake",
        "ekap.Announcement": "fas fa-bullhorn",
        "ekap.TenderDate": "fas fa-calendar-day",
        "ekap.OkasCode": "fas fa-barcode",
        "ekap.OkasItem": "fas fa-cubes",
        "ekap.Authority": "fas fa-building",
        "ekap.City": "fas fa-map-marker-alt",
        "ekap.SyncRun": "fas fa-sync-alt",
        "ekap.SyncCheckpoint": "fas fa-flag-checkered",
        "tenders": "fas fa-folder-open",
        "tenders.Favorite": "fas fa-heart",
        "tenders.SavedFilter": "fas fa-filter",
        "tenders.SavedTender": "fas fa-bookmark",
        "tenders.TenderAlarm": "fas fa-bell",
        "tenders.Notification": "fas fa-envelope",
        "ai": "fas fa-brain",
        "ai.AnalysisCache": "fas fa-microchip",
        "core": "fas fa-cogs",
        "core.AppConfig": "fas fa-triangle-exclamation",
        "core.AppSetting": "fas fa-sliders-h",
        "core.SupportTicket": "fas fa-life-ring",
        "django_celery_beat": "fas fa-clock",
        "django_celery_beat.PeriodicTask": "fas fa-stopwatch",
        "django_celery_beat.IntervalSchedule": "fas fa-hourglass-half",
        "django_celery_beat.CrontabSchedule": "fas fa-calendar-alt",
        "django_celery_beat.SolarSchedule": "fas fa-sun",
        "django_celery_beat.ClockedSchedule": "fas fa-bell",
        "django_celery_results": "fas fa-tasks",
        "django_celery_results.TaskResult": "fas fa-list-check",
        "django_celery_results.GroupResult": "fas fa-object-group",
        "token_blacklist": "fas fa-ban",
        "token_blacklist.OutstandingToken": "fas fa-key",
        "token_blacklist.BlacklistedToken": "fas fa-user-lock",
    },
    "default_icon_parents": "fas fa-chevron-circle-right",
    "default_icon_children": "fas fa-circle",
    # ── Kullanıcı arayüzü ──
    "related_modal_active": True,
    "custom_css": "ihaletakip/admin.css",
    "custom_js": None,
    "use_google_fonts_cdn": True,
    "show_ui_builder": False,
    # ── Değişiklik formları ──
    "changeform_format": "horizontal_tabs",
    "changeform_format_overrides": {
        "accounts.User": "collapsible",
        "auth.Group": "vertical_tabs",
    },
    "language_chooser": False,
}

JAZZMIN_UI_TWEAKS = {
    "navbar_small_text": False,
    "footer_small_text": False,
    "body_small_text": False,
    "brand_small_text": False,
    # NOT: AdminLTE 4'te navbar-primary / sidebar-dark-primary / accent-primary
    # sınıfları YOK. Header ve sidebar renkleri static/ihaletakip/admin.css'te.
    "brand_colour": "",
    "accent": "",
    "navbar": "navbar-dark",
    "no_navbar_border": True,
    "navbar_fixed": True,
    "layout_boxed": False,
    "footer_fixed": False,
    "sidebar_fixed": True,
    "sidebar": "",
    "sidebar_nav_small_text": False,
    "sidebar_disable_expand": False,
    "sidebar_nav_child_indent": True,
    "sidebar_nav_compact_style": False,
    "sidebar_nav_legacy_style": False,
    "sidebar_nav_flat_style": False,
    "theme": "flatly",
    # light | dark | auto — auto: işletim sisteminin tema tercihini izler
    "default_theme_mode": "auto",
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success",
    },
    "actions_sticky_top": True,
}

# ── Logging ────────────────────────────────────────────
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "ihaletakip": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}

# ── Güvenlik (üretim) ──────────────────────────────────
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
