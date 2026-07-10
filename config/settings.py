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
    "core",
    "ekap",
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
    "DEFAULT_RENDERER_CLASSES": (
        "core.renderers.EnvelopeJSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
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
CLAUDE_MODEL = env("CLAUDE_MODEL", default="claude-sonnet-4-20250514")
CLAUDE_MAX_TOKENS = env.int("CLAUDE_MAX_TOKENS", default=3000)
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Google Sign-In (idToken audience'ları)
GOOGLE_CLIENT_IDS = env.list("GOOGLE_CLIENT_IDS", default=[])

# Apple Sign-In
APPLE_CLIENT_ID = env("APPLE_CLIENT_ID", default="com.envisoft.ihaletakip")

# Text-to-Speech
TTS_LANGUAGE_CODE = env("TTS_LANGUAGE_CODE", default="tr-TR")
TTS_VOICE_NAME = env("TTS_VOICE_NAME", default="tr-TR-Standard-A")
TTS_MAX_CHARS = 5000

# FCM Push (opsiyonel)
FCM_CREDENTIALS = env("FCM_CREDENTIALS", default="")
FCM_PROJECT_ID = env("FCM_PROJECT_ID", default="")

# ── EKAP veri toplama ──────────────────────────────────
EKAP_BASE_URL = env("EKAP_BASE_URL", default="https://ekapv2.kik.gov.tr")
# AES-192 imzalama anahtarı (mobil calls.js ile aynı olmalı)
EKAP_SIGNING_KEY = env("EKAP_SIGNING_KEY", default="Qm2LtXR0aByP69vZNKef4wMJ")
EKAP_MIN_INTERVAL_MS = env.int("EKAP_MIN_INTERVAL_MS", default=1000)  # ~1 istek/sn
# TLS parmak izi engelini aşmak için curl_cffi tarayıcı taklidi
EKAP_IMPERSONATE = env("EKAP_IMPERSONATE", default="chrome")
EKAP_TIMEOUT = env.int("EKAP_TIMEOUT", default=30)
EKAP_MAX_RETRIES = env.int("EKAP_MAX_RETRIES", default=4)
EKAP_RECENT_DAYS = env.int("EKAP_RECENT_DAYS", default=3)
EKAP_BACKFILL_YEARS = env.int("EKAP_BACKFILL_YEARS", default=5)

# EKAP görevleri ayrı, tek-concurrency'li kuyrukta serileştirilir
CELERY_TASK_ROUTES = {
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
