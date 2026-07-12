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

ekap/              # EKAP veri toplama + servis (kendi kaynağımız)
├── signing.py     # AES-192-CBC istek imzalama (mobil calls.js karşılığı)
├── client.py      # EkapV2Client (curl_cffi ile TLS parmak izi taklidi)
├── throttle.py    # Redis tabanlı hız sınırlama (~1 istek/sn)
├── constants.py   # DEFAULT_SEARCH_BODY, id→isim maplar, CITIES seed
├── models.py      # Tender + çocuklar, OkasCode, Authority, City, Sync*
├── sync.py        # EKAP→DB eşleme + toplama mantığı
├── tasks.py       # Celery: sync_recent/detail/refresh_stale/backfill/okas/authorities
├── views.py       # /ekap/tenders, detail, announcements, document-url, okas, authorities, cities
└── management/    # seed_cities, ekap_probe, run_ingest

core/              # Ortak altyapı
├── models.py      # TimeStampedModel, AppSetting, SupportTicket
├── renderers.py   # EnvelopeJSONRenderer (global {success,message,data})
├── exceptions.py  # custom_exception_handler (global hata zarfı)
├── response.py    # api_response() yardımcısı
├── storage.py     # JazzminManifestStaticFilesStorage (statik dosya storage)
└── views.py       # health, support
```

## EKAP Entegrasyonu (kritik)

Uygulama artık EKAP'a doğrudan gitmez; EKAP verisini biz toplayıp servis ederiz.

- **TLS parmak izi engeli**: EKAP v2 WAF'ı düz `requests`/OpenSSL'i reddeder
  (`SSLV3_ALERT_HANDSHAKE_FAILURE`). Çözüm: **`curl_cffi`** ile tarayıcı TLS taklidi
  (`impersonate="chrome"`, `EKAP_IMPERSONATE` ayarı). Düz `requests` KULLANMA.
- **İmzalama**: Her EKAP v2 isteği AES-192-CBC imza header'ı ister
  (`X-Custom-Request-Guid/R8id/Siv/Ts`). `signing.py`, mobil `calls.js`'in birebir
  karşılığı; anahtar `EKAP_SIGNING_KEY` (env).
- **Rate limit**: `throttle.py` (~1 istek/sn) + EKAP görevleri ayrı `ekap` Celery
  kuyruğunda **tek concurrency** ile serileştirilir (`ekap-worker` servisi).
- **Toplama (Celery Beat)**: `sync_recent` (gece 02:00), `refresh_stale` (3 saatte bir,
  akıllı kural: geçmiş+sonuçlanmamış → detay yenile; **yalnızca son `EKAP_REFRESH_YEARS`=1
  yıl**), `backfill` (yalnızca gece **01:00–06:00** arası 15 dk'da bir, son
  `EKAP_BACKFILL_YEARS`=5 yıl),
  `sync_okas`/`sync_authorities` (haftalık). Detay `detail_raw`'da tam saklanır;
  ayrı ilan çağrısı yapılmaz (detay zaten `ilanList` içerir → rate limit tasarrufu).
- **Servis**: `/api/v1/ekap/tenders/` (DB'den, **EKAP alan isimleriyle** → mobil
  mapper'lar minimal değişir), `tenders/{ekap_id}/` (detay, İKN'de `/` var — key olarak
  `ekap_id` kullan), `okas/search`, `authorities/search`, `cities`, `tenders/{id}/document-url`
  (dinamik → canlı proxy). Kullanıcı aramaları EKAP'a hiç dokunmaz.
- **Doğrulama**: `python manage.py ekap_probe` (canlı imza testi),
  `python manage.py run_ingest --task recent|backfill|okas|authorities|detail`.

## URL'de İKN (dikkat)

İKN `2025/1234567` biçimindedir, yani **`/` içerir**. Django'nun `<str:...>`
dönüştürücüsü `/` eşleştirmez; `%2F` ile kodlamak da kurtarmaz çünkü WSGI sunucusu
yolu Django'ya vermeden önce çözer (`PATH_INFO` decode edilmiş gelir) → yine `/`.

- İKN'yi yol parametresi yapan rotalar `<path:...>` kullanmalıdır.
  `tenders/urls.py` → `saved-tenders/<path:ikn>/` (aksi halde uç her zaman 404 verirdi).
- `ekap/tenders/<str:key>/` bilerek `str`'dir: altında `announcements/` ve
  `document-url/` alt rotaları var, `path:` açgözlü olup onları yutardı. Bu yüzden
  **EKAP detay uçlarında `ekap_id` kullanın**, İKN değil.

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

## Admin Paneli (Jazzmin)

`/admin/` arayüzü **django-jazzmin** ile temalandırılır (AdminLTE + Bootstrap).

- `jazzmin`, `INSTALLED_APPS` içinde **`django.contrib.admin`'den önce** gelmelidir
  (admin şablonlarını override eder).
- Ayarlar `config/settings.py` → `JAZZMIN_SETTINGS` (marka, üst menü, ikonlar,
  uygulama sıralaması) ve `JAZZMIN_UI_TWEAKS` (tema `flatly`,
  `default_theme_mode="auto"` → OS tema tercihini izler).
- Tema deneyip seçmek için geçici olarak `"show_ui_builder": True` yap.
- `dark_mode_theme` ayarı jazzmin 3.x'te **kaldırıldı**; `default_theme_mode`
  (`light|dark|auto`) kullanılır.

### Marka varlıkları

`static/ihaletakip/` — mobil uygulamanın (`~/Desktop/IhaleTakip/src/assests/logo/v1`)
logo setinden türetildi. Kaynak SVG'ler 1920×1080 canvas içinde ortalanmış olduğu
için `viewBox` gerçek içerik sınırlarına kırpıldı (aksi halde sidebar'da minicik görünür).

| Dosya | Kullanım | JAZZMIN ayarı |
|-------|----------|---------------|
| `icon-white.svg` | sidebar markası (koyu zemin) | `site_logo` |
| `logo.svg` | giriş sayfası, açık tema | `login_logo` |
| `logo-white.svg` | giriş sayfası, koyu tema | `login_logo_dark` |
| `favicon.png` | tarayıcı sekmesi | `site_icon` |
| `admin.css` | marka renkleri + düzeltmeler | `custom_css` |

- Marka renkleri: `#0074cb` (mavi), `#003ea1` (lacivert), `#002a6b` (sidebar).
- **Profil resmi yok**: `user_avatar` tanımlanırsa jazzmin varsayılan bir avatar
  basar. `None` bırakın.
- **AdminLTE 4 uyarısı**: jazzmin 3.x AdminLTE 4 kullanır; `navbar-primary`,
  `sidebar-dark-primary`, `accent-primary` gibi AdminLTE 3 sınıfları **artık yok**.
  Şablon navbar'a `bg-body` (beyaz) verdiği için `navbar-dark` ile birlikte beyaz
  üstüne beyaz metin çıkıyordu (butonlar sadece hover'da görünüyordu). Header ve
  sidebar renkleri bu yüzden `admin.css`'te açıkça tanımlı — UI tweaks'e güvenmeyin.

### Statik dosyalar (dikkat)

- `STATICFILES_STORAGE` ayarı **Django 5.1'de kaldırıldı** ve sessizce yok sayılır.
  Bunun yerine `STORAGES["staticfiles"]` kullanılır. DEBUG'da düz storage,
  üretimde `core.storage.JazzminManifestStaticFilesStorage`.
- Bu özel storage'ın tek işi `manifest_strict = False`: jazzmin'in `admin/base.html`
  şablonu `{% static 'vendor/bootswatch' %}` ile bir **dizin** ister; katı manifest
  bunu bilmediği için `ValueError: Missing staticfiles manifest entry` atar ve tüm
  admin iç sayfaları 500 döner.
- `DEBUG=False` ile `runserver` çalıştırırsan statikleri whitenoise servis eder →
  önce `python manage.py collectstatic` gerekir. Docker entrypoint bunu zaten yapar.
  Yerel geliştirmede `.env` içine `DJANGO_DEBUG=True` koymak en pratiği.

### Docker'da statik dosyalar

- `staticfiles/` ve `media/` **named volume**. Mount noktası image'da yoksa Docker
  onu `root:root` yaratır; konteyner `appuser` (uid 1000) ile koştuğu için
  `collectstatic` yazamaz → manifest üretilmez → **tüm admin 500**. Bu yüzden
  Dockerfile `chown`'dan önce `mkdir -p /app/staticfiles /app/media` yapar.
- Entrypoint'te `collectstatic` artık `|| true` ile susturulmaz; öncesinde
  `staticfiles` yazılabilirlik kontrolü vardır. Hata varsa konteyner ayağa
  kalkmaz ve sebep logda görünür.
- Eski bir kurulumda volume zaten `root` sahipli oluştuysa Dockerfile düzeltmesi
  tek başına yetmez, volume'u sıfırla:
  `docker compose down && docker volume rm ihaletakip-api_static_volume`

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

## İş Akışı Kuralları (ÖNEMLİ)

Bu proje için her oturumda uyulması ZORUNLU kurallar:

1. **Her değişiklikte GitHub'a gönder** — minör/majör fark etmeksizin, yapılan her
   değişiklikten sonra `git add` → `git commit` (açıklayıcı Türkçe mesaj) → `git push`.
   Kullanıcı geri alma isterse sorunsuz eski haline döndür.
2. **Hafızayı/dokümanı güncelle** — anlamlı her değişiklikte bu `CLAUDE.md`'yi ve
   (varsa) kalıcı hafızayı güncel tut; yeni endpoint/model/servis eklenince ilgili
   bölümü yaz.
3. **API dokümanları otomatik** — `docs/openapi.yaml` ve `docs/postman_collection.json`
   `python manage.py gen_api_docs` ile üretilir. **git pre-commit hook** bunu her
   commit'te otomatik çalıştırıp stage'ler (`core.hooksPath=.githooks`). Yeni klonda:
   `git config core.hooksPath .githooks`. Postman'e `docs/postman_collection.json`
   import edilir; `base_url`, `access_token` ve `refresh_token` koleksiyon
   değişkenidir (giriş ve token yenileme istekleri ikisini de otomatik kaydeder).
   Üretilen dosyalar **elle düzenlenmez** — kaynak view'lardaki `@extend_schema`'dır.

   ⚠️ **Sondaki `/` şart**: Postman isteği `url.raw` metninden değil `url.path`
   segment dizisinden kurar; sondaki `/` yalnızca **boş son segment** ile temsil
   edilir (`["api","v1","auth","login",""]`). Eksikse Postman `/auth/login` gönderir,
   Django `APPEND_SLASH` ile 301 döner, Postman yönlendirmeyi izlerken POST'u GET'e
   çevirir ve her uç `"GET" metoduna izin verilmiyor` (405) döner. `_postman_path()`
   bu boş segmenti ekler — kaldırmayın.

4. **Yeni endpoint = `@extend_schema`** — düz `APIView` kullanan bir uç eklerken
   drf-spectacular gövdeyi/parametreleri kendi çıkaramaz; dekoratör yoksa uç Postman'de
   **gövdesiz ve parametresiz** görünür. Her uçta `summary`, `description`, `request`,
   `responses`, `parameters` ve en az bir `OpenApiExample` verin (bkz. `accounts/views.py`).
   `generics.*` view'larında `@extend_schema_view(get=..., post=...)` kullanın, yoksa
   istek adı `api_v1_saved_filters_list` gibi görünür. Herkese açık uçlara `auth=[]`
   ekleyin — üreteç bunu Postman'de `noauth` olarak işaretler.

Commit mesajı sonuna şunu ekle:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

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

## Üretim Dağıtımı (Ubuntu + Cloudflare)

Ağ akışı: **Cloudflare (443/SSL) → sunucu:443 (nginx TLS) → gunicorn (web:8000)**.
Cloudflare bu hostname için **Full (strict)** (zone geneli Flexible olabilir — bkz.
aşağıdaki 522 tuzağı); origin, Cloudflare **Origin Certificate** ile 443'te TLS
sonlandırır (CF↔origin şifreli). Dışa açılan **tek port 443**'tür (nginx);
`web`, `db`, `redis` yalnızca iç ağda.

- **Ortam dosyası `.env.prod`**: Tüm servisler `env_file: .env.prod` kullanır
  (`docker-compose.yml`). `.gitignore`'dadır → commit edilmez, sunucuya elle kopyalanır.
  `.env.example`'dan türetilir. **Kritik**: `DJANGO_DEBUG=False`, güçlü
  `DJANGO_SECRET_KEY`, gerçek `DJANGO_ALLOWED_HOSTS` (domain — yanlışsa 400),
  `POSTGRES_PASSWORD` = `DATABASE_URL` içindeki şifre ile aynı.
- **nginx** (`docker/nginx/default.conf`): 443'te TLS; Cloudflare gerçek IP
  restorasyonu (`CF-Connecting-IP` + CF IP aralıkları), `X-Forwarded-Proto` iletimi
  (`settings.SECURE_PROXY_SSL_HEADER` bunu okuyup güvenli çerezleri açar),
  `client_max_body_size 20m` (AI 10 MB yükleme payı), `proxy_read_timeout 180s`
  (canlı EKAP çağrıları). CF IP aralıkları değişebilir: https://www.cloudflare.com/ips
- **nginx proxy başlıkları `server` seviyesinde**: `proxy_set_header` bir `location`
  içinde yeniden tanımlanırsa nginx üst seviyedeki **TÜM** `proxy_set_header`'ları o
  location için yok sayar. Bu yüzden `Host`/`X-Forwarded-*` başlıkları `server`
  seviyesinde; `location = /health/` bunları miras alır. (Aksi halde /health/ için Host
  = `$proxy_host` = `django_app` gider → Django `DisallowedHost` → 400.)
- **TLS sertifikası** (`docker/nginx/certs/`): Cloudflare **Origin Certificate**
  (`cf-origin.pem` + `cf-origin.key`, 15 yıl, yenileme yok). `.gitignore`'da →
  commit edilmez, sunucuda elle oluşturulur (bkz. `docker/nginx/certs/README.md`).
- **Cloudflare**: DNS kaydı **turuncu bulut** (proxied), A kaydı origin IP'sine.
- **⚠️ SSL modu tuzağı = 522**: SSL/TLS modu **zone geneli**dir. Bu zone
  (`envisoft.com.tr`) başka siteler için **Flexible** ayarlı ve öyle kalmalı. Flexible'da
  CF origin'e **80/HTTP**'den bağlanır; origin sadece 443/TLS dinlediği için CF
  origin:80'e ulaşamaz → **HTTP 522** (istek nginx'e **hiç** ulaşmaz, nginx logunda iz
  yok; `server: cloudflare` başlığı gelir). Çözüm: zone genelini değiştirmeden **Rules →
  Configuration Rules** ile *yalnızca* `ihale-takip.envisoft.com.tr` hostname'i için
  **SSL = Full (strict)** override et (Page Rule ile de olur). Diğer siteler Flexible kalır.
- **Origin'i CF'e kilitle** (yapılacak hardening): Origin IP'sine internetten sürekli bot
  taraması gelir. nginx'te Authenticated Origin Pulls (mTLS — `default.conf`'ta hazır,
  bkz. README) veya iptables ile 443'ü yalnızca Cloudflare IP aralıklarına aç.
  **Uyarı**: Docker yayınlanan portları **UFW'yi BYPASS eder** → UFW allow/deny 443'ü
  kısıtlamaz; `DOCKER-USER` iptables zinciri ya da nginx-seviyesi AOP kullan.
- **Güvenli çerez zinciri**: `DEBUG=False` → `SESSION_COOKIE_SECURE=True`
  (`settings.py`). TLS + `X-Forwarded-Proto: https` olmadan **admin'e giriş yapılamaz**
  (login olur, geri login'e atar). Cloudflare + nginx bu header'ı sağladığı için çalışır.
- **Başlatma**: `docker compose up -d --build`. `web` healthcheck'i (`/health/`)
  geçmeden nginx başlamaz. Entrypoint yalnızca `web`'de migrate + collectstatic yapar.
- **Dağıtım sonrası doğrulama**: `docker compose exec web python manage.py ekap_probe`
  (imza + canlı EKAP), `curl -I https://<domain>/health/` → `200`.

### İlk kurulum akışı (Ubuntu, özet)

1. Sistem güncelle + reboot: `apt update && apt upgrade -y` → `reboot`.
2. Docker + git: `apt install -y git curl` → `curl -fsSL https://get.docker.com | sh`.
3. Repo: `git clone ... && cd ihaletakip-api && git config core.hooksPath .githooks`.
4. `.env.prod` oluştur (`.env.example`'dan; `DJANGO_DEBUG=False`, güçlü SECRET_KEY,
   domain, DB şifresi, **güçlü admin parolası**). `chmod 600 .env.prod`.
5. Origin cert: `docker/nginx/certs/cf-origin.pem` + `cf-origin.key` (CF panel → Origin
   Server → Create Certificate). `chmod 600 ...key`. `mkdir -p credentials`.
6. Cloudflare: DNS A kaydı (proxied) + bu hostname'e **Configuration Rule SSL=Full(strict)**
   (zone Flexible olduğu için — yoksa 522).
7. `docker compose up -d --build` (ilk build 3-8 dk; uzun işlemlerde `tmux` kullan ki
   SSH kopsa build sürsün). `docker compose ps` → hepsi Up/healthy.
8. **Admini elle oluştur** (entrypoint bunu otomatik yapmaz — her restart'ta parolayı
   ezmemek için): `docker compose exec web python manage.py create_admin`. Parolayı
   sonradan `changepassword firat` ile değiştirirsen kalıcı olur.
9. Doğrula: `curl -sk https://localhost/health/` (iç), `ekap_probe`, `curl -I https://<domain>/health/`.

## Önemli Uyarılar

- **PostgreSQL üretimde zorunlu**: Yerel `manage.py check` `DATABASE_URL` yoksa
  SQLite'a düşer; üretim/Docker daima Postgres kullanır.
- **Google TTS kimliği**: `GOOGLE_APPLICATION_CREDENTIALS` bir servis hesabı JSON
  yolu göstermeli (`credentials/` dizini, git'e girmez).
- **FCM push opsiyonel**: `FCM_CREDENTIALS` boşsa push devre dışıdır (no-op).
- **Firebase Cloud Function bug fix'leri portlandı**: `.doc` net reddedilir,
  Claude bağlantı/timeout hataları güvenli işlenir (bkz. `ai/services/claude.py`).
```
