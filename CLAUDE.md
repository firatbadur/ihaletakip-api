# IhaleTakip API - Claude Code Rehberi

## Proje Genel Bakış

IhaleTakip mobil uygulamasının (React Native) backend servisi. Firebase
(Cloud Functions + Firestore + Auth) yerine geçen, kendi kendine barındırılan
**Django REST Framework** API'sidir.

- **Dil/Çatı**: Python 3.13 + Django 5.1 + Django REST Framework
- **Veritabanı**: PostgreSQL 16
- **Kuyruk/Cache**: Redis 7 + Celery + Celery Beat
- **Kimlik**: JWT (SimpleJWT) + Google/Apple Sign-In doğrulama
- **AI**: Anthropic Claude (doküman analizi) + Google Cloud TTS
- **Dağıtım**: Docker Compose (Ubuntu)
- **UI Dili / Mesajlar**: Türkçe

Bu servis, mevcut `~/Desktop/IhaleTakip` React Native uygulamasındaki Firebase
bağımlılığını (auth, firestore, cloud functions) devralmak üzere tasarlandı.

## Komutlar

```bash
# ── Docker (önerilen) ──────────────────────────────────
docker compose up -d --build      # tüm servisleri ayağa kaldır
docker compose logs -f web        # web loglarını izle
docker compose exec web python manage.py <komut>
docker compose down               # durdur

# ── Yerel geliştirme (venv) ────────────────────────────
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
celery -A config worker -l info   # ayrı terminal
celery -A config beat -l info     # ayrı terminal

# ── Sık kullanılan ─────────────────────────────────────
python manage.py makemigrations
python manage.py create_admin     # env'den admin oluştur/güncelle
python manage.py check
```

## Mimari

```
config/            # Django proje ayarları
├── settings.py    # tüm ayarlar (env tabanlı, django-environ)
├── urls.py        # kök router → /api/v1/...
├── celery.py      # Celery app + Beat schedule
└── wsgi/asgi.py

accounts/          # Kullanıcı + kimlik doğrulama
├── models.py      # User (AbstractUser genişletmesi)
├── managers.py    # sosyal giriş kullanıcı yönetimi
├── services/      # google.py, apple.py (token doğrulama)
├── views.py       # register/login/logout/social/profile/preferences/fcm
└── management/commands/create_admin.py

tenders/           # İhale ile ilgili kullanıcı içerikleri
├── models.py      # Favorite, SavedFilter, SavedTender, TenderAlarm, Notification
├── views.py       # CRUD endpoint'leri
└── tasks.py       # Celery: alarm kontrolü, bildirim temizliği

ai/                # Yapay zeka servisleri
├── models.py      # AnalysisCache
├── prompts.py     # Claude prompt şablonları
├── services/      # claude.py (analiz), tts.py (seslendirme)
├── tasks.py       # Celery: run_analysis_task, cleanup_expired_analyses
└── views.py       # analyze (async), analyze-status, tts

core/              # Ortak altyapı
├── models.py      # TimeStampedModel, AppSetting, SupportTicket, Detsis
├── renderers.py   # EnvelopeJSONRenderer (global {success,message,data})
├── exceptions.py  # custom_exception_handler (global hata zarfı)
├── response.py    # api_response() yardımcısı
└── views.py       # health, support, detsis-search
```

## Global API Sözleşmesi

**Tüm** yanıtlar şu zarfta döner:

```json
{ "success": true, "message": "", "data": {} }
```

Hata durumunda:

```json
{ "success": false, "message": "Açıklama", "data": null, "errors": {...} }
```

- View'lar düz `Response(data)` dönebilir → `EnvelopeJSONRenderer` otomatik sarar.
- Özel mesaj/durum için `core.response.api_response(data, message, success, status)`.
- Beklenmeyen hatalar dahil **her hata** JSON zarfı döner (asla ham HTML 500 yok).

## Kimlik Doğrulama Akışı

- **JWT**: `Authorization: Bearer <access>` header'ı. Access 1 gün, refresh 30 gün.
- **E-posta/şifre**: `POST /api/v1/auth/register`, `POST /api/v1/auth/login`
- **Google**: `POST /api/v1/auth/social/google` body `{id_token}` — istemci
  `@react-native-google-signin`'den alır. Sunucu Google imzasını doğrular.
- **Apple**: `POST /api/v1/auth/social/apple` body `{identity_token}` — istemci
  `@invertase/react-native-apple-authentication`'dan alır. Sunucu Apple public
  key'leriyle doğrular (audience = `com.envisoft.ihaletakip`).
- **Çıkış**: `POST /api/v1/auth/logout` body `{refresh}` → token kara listeye alınır.

Admin girişi `username` iledir (varsayılan admin: `firat`).

## Uzun İşlemler → Celery

Doküman analizi gibi uzun süren tüm işler **her zaman** Celery worker'a atılır:

1. `POST /api/v1/ai/analyze` → `{task_id, status:'pending'}` (İKN cache varsa
   anında sonuç döner).
2. `GET /api/v1/ai/tasks/{task_id}` → durum: `pending|processing|completed|failed`.

**Celery Beat** periyodik işleri yürütür (config/celery.py):
- `cleanup_expired_analyses` — eski AI cache temizliği (günlük 03:00)
- `check_tender_alarms` — ihale alarm kontrolü (saatlik)
- `cleanup_old_notifications` — eski bildirim temizliği (günlük 04:00)

## Veri Modeli (Firestore karşılıkları)

| Firestore | Django modeli |
|-----------|---------------|
| `users/{uid}` | `accounts.User` |
| `users/{uid}/favorites` | `tenders.Favorite` |
| `users/{uid}/savedFilters` | `tenders.SavedFilter` |
| `users/{uid}/savedTenders` | `tenders.SavedTender` |
| `users/{uid}/alarms` | `tenders.TenderAlarm` |
| `users/{uid}/notifications` | `tenders.Notification` |
| `analyses/{ikn}/results/{type}` | `ai.AnalysisCache` |
| `supportTickets` | `core.SupportTicket` |
| `detsis` | `core.Detsis` |
| `config/ai_service` | `core.AppSetting` |

## Kodlama Kuralları

- **Ağır importlar lazy**: `anthropic`, `google.cloud.texttospeech`, `docx`,
  `pypdf`, `google.auth`, `jwt` her zaman fonksiyon içinde import edilir —
  böylece `manage.py check` ve hafif komutlar bu bağımlılıklar olmadan çalışır.
- **Sırlar env'de**: Hiçbir anahtar koda gömülmez; `settings.py` `django-environ`
  ile `.env` okur. `.env` asla commit edilmez (`.gitignore`).
- **Renkler/mesajlar Türkçe**: Kullanıcıya dönen tüm mesajlar Türkçe.
- **Yeni endpoint ekleme**: model → serializer → view → `<app>/urls.py` →
  gerekiyorsa `config/urls.py`'de include.
- **Global zarfı bozma**: View'lar ya düz `Response(data)` ya da
  `api_response(...)` kullanmalı; elle `{success:...}` kurmayın.

## Önemli Uyarılar

- **PostgreSQL üretimde zorunlu**: Yerel `manage.py check` `DATABASE_URL` yoksa
  SQLite'a düşer; üretim/Docker daima Postgres kullanır.
- **Google TTS kimliği**: `GOOGLE_APPLICATION_CREDENTIALS` bir servis hesabı JSON
  yolu göstermeli (`credentials/` dizini, git'e girmez).
- **FCM push opsiyonel**: `FCM_CREDENTIALS` boşsa push devre dışıdır (no-op).
- **Firebase Cloud Function bug fix'leri portlandı**: `.doc` net reddedilir,
  Claude bağlantı/timeout hataları güvenli işlenir (bkz. `ai/services/claude.py`).
```
