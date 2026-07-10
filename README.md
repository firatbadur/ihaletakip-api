# IhaleTakip API

IhaleTakip mobil uygulamasının kendi kendine barındırılan backend servisi.
Firebase (Auth + Firestore + Cloud Functions) yerine geçer.

**Stack:** Django 5 · DRF · PostgreSQL · Redis · Celery · Docker

---

## Hızlı Başlangıç (Docker — Ubuntu)

```bash
# 1) Repoyu al ve dizine gir
cd ihaletakip-api

# 2) Ortam dosyasını hazırla
cp .env.example .env
#   → .env içindeki değerleri doldur (SECRET_KEY, ANTHROPIC_API_KEY,
#     Google/Apple client id'leri, DB şifresi, admin şifresi...)

# 3) (Opsiyonel) Google TTS servis hesabı JSON'unu koy
mkdir -p credentials
#   → credentials/tts-service-account.json

# 4) Ayağa kaldır
docker compose up -d --build

# 5) Kontrol
curl http://localhost:8000/health/
```

Servisler:
| Servis | Açıklama | Port |
|--------|----------|------|
| `web` | Gunicorn (Django API) | 8000 |
| `worker` | Celery worker (analiz vb.) | — |
| `beat` | Celery Beat (periyodik) | — |
| `db` | PostgreSQL 16 | 5432 (iç) |
| `redis` | Redis 7 | 6379 (iç) |

İlk açılışta `web` otomatik olarak: migrate → collectstatic → **admin oluştur**
(`.env`'deki `DJANGO_SUPERUSER_*`, varsayılan `firat` / `Firat1212b.`).

- **Admin paneli**: http://localhost:8000/admin/
- **API dokümantasyonu**: http://localhost:8000/api/docs/ (Swagger)

---

## Ubuntu Sunucuya Kurulum

```bash
# Docker + Compose kur
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo tee /etc/apt/keyrings/docker.asc > /dev/null
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Projeyi çalıştır
cd ihaletakip-api
cp .env.example .env    # değerleri doldur (DEBUG=False, gerçek ALLOWED_HOSTS)
docker compose up -d --build
```

Önünde bir reverse proxy (Nginx/Caddy) ile 443/TLS sonlandırması önerilir.

---

## API Uçları (özet)

Tüm uçlar `/api/v1/` altında. Yanıt zarfı: `{success, message, data}`.

### Kimlik (`/auth`)
| Method | Yol | Açıklama |
|--------|-----|----------|
| POST | `/auth/register` | E-posta+şifre kayıt |
| POST | `/auth/login` | Giriş (username/email + şifre) |
| POST | `/auth/logout` | Çıkış (refresh blacklist) |
| POST | `/auth/token/refresh` | Access token yenile |
| POST | `/auth/social/google` | Google `{id_token}` |
| POST | `/auth/social/apple` | Apple `{identity_token}` |
| GET/PATCH | `/auth/profile` | Profil |
| PATCH | `/auth/preferences` | Tercihler |
| POST | `/auth/fcm-token` | Push token kaydet |
| POST | `/auth/deactivate` | Hesabı devre dışı bırak |

### İhale içerikleri
| Method | Yol |
|--------|-----|
| GET/POST | `/favorites` · `/saved-filters` · `/saved-tenders` · `/alarms` |
| DELETE/GET | `/favorites/{id}` · `/saved-tenders/{ikn}` · `/alarms/{id}` |
| GET | `/notifications` · `/notifications/unread-count` |
| POST | `/notifications/{id}/read` · `/notifications/read-all` |

### AI (`/ai`)
| Method | Yol | Açıklama |
|--------|-----|----------|
| POST | `/ai/analyze` | Analizi kuyruğa al → `{task_id}` |
| GET | `/ai/tasks/{task_id}` | Görev durumu/sonucu |
| POST | `/ai/tts` | Metni sese çevir |

### Diğer
| Method | Yol |
|--------|-----|
| GET | `/health/` · `/api/v1/detsis/search?q=` |
| GET/POST | `/api/v1/support` |

---

## Yerel Geliştirme (Docker'sız)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# DATABASE_URL vermezsen SQLite kullanılır
python manage.py migrate
python manage.py create_admin
python manage.py runserver
# Redis çalışıyorsa:
celery -A config worker -l info
celery -A config beat -l info
```

---

## API Dokümantasyonu & Postman

- **Swagger UI**: http://localhost:8000/api/docs/ · **OpenAPI**: `docs/openapi.yaml`
- **Postman**: `docs/postman_collection.json` dosyasını Postman'e import et.
  - Koleksiyon değişkenleri: `base_url` (varsayılan `http://localhost:8000`) ve `access_token`.
  - **auth › login** isteğini çalıştır → JWT otomatik `access_token`'a kaydedilir → diğer
    tüm istekler Bearer ile yetkilenir.
- **Otomatik güncelleme**: `python manage.py gen_api_docs` OpenAPI + Postman'i şemadan
  üretir. **git pre-commit hook** her commit'te otomatik çalışır. Yeni klonda bir kez:
  ```bash
  git config core.hooksPath .githooks
  ```

## Mobil Uygulamaya Entegrasyon (sonraki adım)

React Native tarafında Firebase çağrıları bu API'ye taşınacak:
- `firestoreApi.js` → REST çağrıları (`/favorites`, `/saved-filters`, ...)
- `claudeAI.js` → `/ai/analyze` + `/ai/tasks/{id}` (async poll)
- `ttsService.js` → `/ai/tts`
- Login → `/auth/social/google|apple` (Firebase Auth yerine JWT sakla)

Detaylar `CLAUDE.md` içinde.
