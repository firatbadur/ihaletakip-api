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
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

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
