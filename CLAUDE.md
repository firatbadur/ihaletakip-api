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
├── services/      # push.py (FCM gönderici), notify.py (kayıt+pacing'li dağıtıcı),
│                  #   templates.py (Türkçe bildirim metinleri)
├── management/    # send_test_push, run_notifications
└── tasks.py       # Celery: alarm kontrolü, kayıtlı filtre eşleşmesi, bildirim temizliği

ai/                # Yapay zeka servisleri
├── models.py      # AnalysisCache
├── prompts.py     # Claude prompt şablonları
├── services/      # claude.py (analiz), tts.py (seslendirme)
├── tasks.py       # Celery: run_analysis_task, cleanup_expired_analyses
└── views.py       # analyze (async), analyze-status, tts

assistant/         # İhale Asistanı (firma profili + AI sohbet + günlük öneri)
├── models.py      # CompanyProfile, TenderRecommendation, ChatMessage
├── prompts.py     # profil haritası + sohbet system prompt şablonları
├── services/      # profile_map.py (Claude→profil haritası), chat.py (çok turlu
│                  #   sohbet, prompt cache breakpoint'li), matching.py (kural
│                  #   tabanlı skorlama: şehir/tür/OKAS/anahtar kelime/bütçe)
├── tasks.py       # Celery: generate_profile_map, match_recommendations (beat 07:00)
├── views.py       # profile (GET/PUT), chat, messages, recommendations(+seen)
└── urls.py        # /api/v1/assistant/...

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
- **Dil (Accept-Language)**: EKAP enum açıklamalarını (`ihaleTipAciklama`,
  `ihaleUsulAciklama`, `ihaleDurumAciklama`) **istek dilini** temel alarak yerelleştirir.
  `curl_cffi` chrome taklidi varsayılan `Accept-Language: en-US` gönderdiği için bu
  alanlar İngilizce ("Production", "Procurement Procedure"...) dönüyordu. `client.py`
  `_post` başlıklarına **`Accept-Language: tr-TR,tr;q=0.9`** eklendi → Türkçe döner.
  Yalnızca **yeni/yeniden senkronlanan** kayıtlar düzelir; eski kayıtlar `refresh_stale`/
  `sync_recent` ile zamanla güncellenir. (`ihaleKapsamAciklama` zaten hep Türkçe geliyor.)
- **İmzalama**: Her EKAP v2 isteği AES-192-CBC imza header'ı ister
  (`X-Custom-Request-Guid/R8id/Siv/Ts`). `signing.py`, mobil `calls.js`'in birebir
  karşılığı; anahtar `EKAP_SIGNING_KEY` (env).
- **Rate limit**: `throttle.py` (~1 istek/sn) + EKAP görevleri ayrı `ekap` Celery
  kuyruğunda **tek concurrency** ile serileştirilir (`ekap-worker` servisi).
- **Pencere = EKAP tarafı tarih filtresi (kritik)**: Toplama son `EKAP_BACKFILL_YEARS`
  (vars. 5) yılla sınırlıdır ve bu sınır **EKAP aramasında** `ihaleTarihSaatBaslangic`
  ile uygulanır (`sync_recent` + `backfill`, ortak yardımcı `_window_floor()`). EKAP bu
  alanı **yalnızca ISO** (`YYYY-MM-DDTHH:MM:SS`) kabul eder — DD.MM.YYYY → HTTP 400.
  **Neden istemci tarafı yetmez**: `ilanTarihi` liste seviyesinde **%100 boş** gelir;
  eski kod `oldest`'ı ona göre hesapladığı için pencere kontrolü hiç tetiklenmiyordu →
  backfill EKAP'ın **~1.96M / 2002'ye kadar** havuzunu sonsuza dek kazıyordu (DB her
  tarihten kayıtla doluyordu). Artık sınır **ihale tarihine** göre EKAP'ta uygulanır;
  istemci `oldest` kontrolü yedek olarak `ihale_tarihi` (dolu alan) kullanır. Pencere
  daraltmak/genişletmek için tek düğme: `.env` → `EKAP_BACKFILL_YEARS`.
- **Toplama (Celery Beat)**: `sync_recent` (gece 02:00; en yeni ilanlar, `ilanTarihi` desc,
  20 sayfa), `refresh_stale` (3 saatte bir, akıllı kural: geçmiş+sonuçlanmamış → detay
  yenile; **yalnızca son `EKAP_REFRESH_YEARS`=1 yıl**), `backfill` (**tüm gün** 15 dk'da
  bir; pencere tabanından **ileriye** `ihaleTarihi` **asc** — DB'deki asıl boşluk eski
  yıllar olduğu için önce onları doldurur, en yeni kayıtlar listenin sonuna eklendiğinden
  imleç kaymaz; `skip >= total_count` [pencere içi toplam] ya da boş sayfada `done=True`.
  EKAP gün içinde yavaş/yanıtsız olabildiğinden görev sayfa hatasını **zarifçe yutar**:
  kısmi ilerlemeyi `SyncCheckpoint`'e kaydeder, çalışmayı *error* saymaz — `SyncRun.note`'a
  "EKAP kısmi" düşer — ve bir sonraki tetikte kaldığı yerden devam eder. Kilit=1sa üst üste
  binmeyi, throttle ~1istek/sn + tek concurrency EKAP'ı korur),
  `sync_okas`/`sync_authorities` (haftalık). Detay `detail_raw`'da tam saklanır;
  ayrı ilan çağrısı yapılmaz (detay zaten `ilanList` içerir → rate limit tasarrufu).
  ⚠️ **Pencere değişince** (`EKAP_BACKFILL_YEARS` veya filtre mantığı) backfill
  checkpoint'i sıfırla ki yeni pencereyle baştan taransın:
  `SyncCheckpoint.objects.filter(name="backfill").update(cursor_skip=0, done=False, oldest_date=None)`.
- **Dedup anahtarı = İKN**: `upsert_tender_from_list` satırı **`ikn`'ye göre** upsert eder
  (`ekap_id` değil), `ekap_id`'yi son değere günceller. Çünkü EKAP aynı İKN'yi farklı iç
  `id` ile döndürebilir (yeniden yayım); `ekap_id` ile upsert edilirse aynı İKN farklı
  id'yle gelince `ikn` unique kısıtı ihlal edilip ingest patlardı. Liste döngüleri
  (`sync_recent`/`backfill`) `_upsert_item_safe` ile sarılıdır → tek bozuk kayıt tüm
  çalışmayı düşürmez, `SyncRun.errors`'a sayılır.
- **Servis**: `/api/v1/ekap/tenders/` (DB'den, **EKAP alan isimleriyle** → mobil
  mapper'lar minimal değişir), `tenders/{ekap_id}/` (detay, İKN'de `/` var — key olarak
  `ekap_id` kullan), `okas/search`, `authorities/search`, `cities`, `tenders/{id}/document-url`
  (dinamik → canlı proxy). Kullanıcı aramaları EKAP'a hiç dokunmaz.
- **Doğrulama**: `python manage.py ekap_probe` (canlı imza testi),
  `python manage.py run_ingest --task recent|backfill|okas|authorities|detail`.

## İhale Asistanı (`assistant/`)

Firma profiline göre günlük ihale önerisi + AI sohbet. Uçlar `/api/v1/assistant/...`:

- **Profil**: `GET/PUT profile/` — `PUT` profili kaydeder ve Claude ile **profil
  haritası** üretimini Celery'ye atar (`generate_profile_map`); yanıt `{task_id}`
  döner, durum mevcut `GET /ai/tasks/{task_id}` ile izlenir. Harita
  (`keywords`, `okas_prefixes`, ...) `CompanyProfile.profile_map`'te, API'de read-only.
- **Sohbet**: oturum bazlıdır (`ChatConversation`). `POST chat/` gövdesinde
  `conversation` yoksa yeni oturum açar ve `{task_id, conversation_id}` döner;
  sonraki mesajlar `conversation` ile gönderilir. Bağlam yalnızca o oturumun
  mesajlarından kurulur (system prompt **prompt cache breakpoint**'li).
  `GET conversations/` geçmiş oturumları (**yalnızca `updated_at` son `days`=30 gün
  içinde** olanlar; `?days=N`, en çok 365 — eski sohbet DB'de kalır, listelenmez),
  `GET conversations/{id}/` oturum mesajlarını döner; `DELETE conversations/{id}/`
  oturumu siler. Digest mesajları her gün kendi `kind="digest"` oturumunda açılır
  (`payload.kind="digest"` + `tender_cards`). `GET messages/` eski (oturumsuz) uç
  olarak durur.
- **Öneriler**: `GET recommendations/`, `POST recommendations/{id}/seen/`.
  Günlük eşleştirme `match_recommendations(since_days=1)` beat görevi (07:00): **kural
  tabanlı** skorlama (şehir/tür/OKAS/anahtar kelime/bütçe — Claude çağrısı YOK, bedava) →
  `TenderRecommendation` + **digest sohbeti** (`kind="digest"`) + o sohbete **bağlı**
  push bildirimi. `CompanyProfile.is_active=False` ise kullanıcı atlanır.
  - **Eşleştirme kapsamı** (`match_tenders_for_profile`): `ilan_tarihi` liste
    senkronunda çoğu ihalede **boş** kaldığından ona göre filtrelenmez; **durum 2/3
    (katılıma açık) + teklifi geçmemiş** (`ihale_tarihi >= now` ya da boş) kullanılır.
    `since` verilirse (beat) yalnızca `ilan_tarihi` DOLU olanlara ek daraltma. Kullanıcının
    zaten **kaydettiği** ihaleler (`SavedTender`) önerilerden **exclude** edilir (İKN ile
    açıkça sorulursa yine gelir).
  - **Digest bildirimi = `Notification.type=CHAT`** + `conversation_id`: mobilde
    bildirime basınca ihale detayı DEĞİL, ilgili digest **sohbeti** açılır. (`Notification`
    modeline `CHAT` türü ve `conversation_id` alanı eklendi.)
- **Elle tetikleme**: `python manage.py run_assistant_match [--days N]` — beat beklemeden
  (veya beat çalışmıyorsa) eşleştirmeyi çalıştırır. `DatabaseScheduler` kullanıldığından
  kodda tanımlı beat girdisi ancak **beat yeniden başlatılınca** DB'ye senkronlanır.
- **Sohbet yönlendirme (`assistant_chat_task`) — niyet bazlı**:
  1. **Belirli ihale**: konuşma bir ihaleye bağlıysa (`tender_ikn`) VEYA mesajda tek İKN
     geçiyorsa → o ihaleyi `ekap.Tender`'dan çöz, detayını LLM'e ver, **analiz** + tıklanabilir
     kart. Çoklu İKN → hepsini kart getirir (LLM yok). Bulunamayan İKN → bilgilendirme.
  2. **Kayıtlı ihaleler**: yalnızca açıkça sorulunca ("takip ettiğim ihaleler") → `SavedTender`
     kartları. (Aksi halde öne çıkarılmaz — alakasız sorularda kafa karıştırıyordu.)
  3. **Öneri/listeleme** ("bana uygun ihale"): kural tabanlı eşleşme kartları (LLM yok);
     bugünkü `TenderRecommendation` yoksa **canlı eşleştirme** (`since=None` → tüm açık +
     teklifi geçmemiş uygun ihaleler; `profile_map` zayıfsa eşik 1.0). Eşleşme yoksa
     yönlendirme mesajı (kayıtlı ihale SIZMAZ).
  4. **Genel soru-cevap**: LLM, **minimal bağlam** (yalnızca tarih; profil zaten persona'da).
  Kart yalnızca bağlamdaki gerçek İKN'lere çözülür (uydurma yok). Kart çözümlemesi hep
  `ekap.Tender`'a bağlanır → doğru `ekap_id` (mobilde tıklayınca detaya gider).
- Dedup: `(user, tender)` unique — aynı ihale aynı kullanıcıya iki kez önerilmez.
- Claude çağrıları (profil haritası + sohbet) `ANTHROPIC_API_KEY` ister; anahtar
  yoksa sohbet/profil haritası hata döner ama öneri eşleştirme çalışmaya devam eder.
- **Model ayrımı (token tasarrufu)**: Sohbet (`chat_completion`) sık çalıştığı için
  ucuz modelle döner — `CLAUDE_CHAT_MODEL` (varsayılan `claude-haiku-4-5`) +
  `CLAUDE_CHAT_MAX_TOKENS` (varsayılan 1000). Profil haritası ve doküman analizi
  kalite öncelikli olduğundan `CLAUDE_MODEL` (varsayılan `claude-sonnet-5`) +
  `CLAUDE_MAX_TOKENS`'te kalır. Sohbet geçmişi bağlamı son **12** mesajla sınırlı
  (`build_chat_messages`), sistem promptu prompt-cache breakpoint'li.

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
- `cleanup_old_notifications` — eski bildirim temizliği (günlük 04:00)
- `match_recommendations` — İhale Asistanı günlük öneri eşleştirmesi + push (günlük 07:00,
  gece EKAP `sync_recent` bittikten sonra)
- `check_tender_alarms` — ihale alarm hatırlatıcıları + push (günlük 09:00)
- `check_saved_filter_matches` — kayıtlı filtre yeni-ihale bildirimi + push (günlük 10:00)

## Bildirim Servisi (Push)

Eski `~/Desktop/ihaletakip-scheduler` (Python + `firebase-admin`, Firebase projesi
`ihale-53fbf`) servisinin işlevi API'ye taşındı; artık kendi Postgres/EKAP verimizden
push üretiyoruz. Üç kaynak, **kademeli ve gruplanmış** bir düzende çalışır — kullanıcı
bombardımana tutulmaz.

- **Katmanlar** (`tenders/services/`):
  - `push.py` — FCM gönderici (`firebase_admin` **lazy** import; `FCM_CREDENTIALS` boşsa
    no-op). `send_fcm()` durum döner: `sent`/`invalid_token`/`error`/`disabled`. Ölü token
    (`UnregisteredError`, `SenderIdMismatchError`, token'a dair `InvalidArgumentError`) →
    `invalid_token`.
  - `notify.py` — **kayıt ve push ayrıdır**: `record_notification()` yalnızca uygulama-içi
    `Notification` satırı yazar; `push_to_user()` pacing kapılarından geçen **tek** push atar.
    `notify_and_push()` tek-olay kısayolu (öneriler).
  - `templates.py` — Türkçe metinler (İhale Günü / Doküman Güncellendi / İhale Sonuçlandı,
    alarm özeti, filtre eşleşmesi).
- **Zamanlama (kademeli, ≥1 sa arayla)**: 07:00 öneri digest'i (`match_recommendations`),
  09:00 alarm hatırlatıcıları (`check_tender_alarms`), 10:00 filtre eşleşmeleri
  (`check_saved_filter_matches`). Her kategori **kullanıcı başına tek özet push**.
- **Pacing kapıları** (`django.core.cache`=Redis, ayar-tabanlı): sessiz saat
  (`NOTIF_QUIET_START/END_HOUR`, vars. 22–07), günlük limit (`NOTIF_DAILY_CAP`=4),
  min aralık (`NOTIF_MIN_GAP_MINUTES`=30), idempotency (`cache` 7 gün TTL), kullanıcı
  tercihi (`User.preferences["notifications"]["push"]`, vars. açık) + `is_active` + dolu
  `fcm_token`. Kapı engellese de **uygulama-içi satır yazılır**, yalnızca push atlanır.
- **Kaynaklar**:
  1. **Öneri** — `match_recommendations` digest'i (mevcut) artık push de atar
     (`type=CHAT`, `conversation_id` → mobilde digest sohbeti açılır). idem `digest:{uid}:{date}`.
  2. **Alarm** (`TenderAlarm.reminder_day/document_change/completed`) — `ekap.Tender` ile
     karşılaştırır: ihale günü (`ihale_tarihi`=bugün), doküman değişikliği
     (`dokuman_sayisi != last_dokuman_sayisi`; ilk görüşte sessiz), sonuçlandı (durum
     `DURUM_SONUCLANMIS`'e geçiş; `completed_notified` ile tek sefer). Kullanıcı başına tek
     birleşik özet push (idem `alarm:{uid}:{date}`). Snapshot alanları `TenderAlarm`'da.
  3. **Kayıtlı filtre** (`SavedFilter.alarm` truthy) — filtreye uyan **yeni** (son kontrolden
     sonra DB'ye giren) açık ihaleler. Filtre semantiği `ekap.views.apply_tender_filters`
     ile view'la **ortak**. İlk kontrolde sessiz (taban `last_notified_at`). idem `filter:{uid}:{date}`.
- **FCM kimliği**: `credentials/fcm-service-account.json` (git-ignore, TTS anahtarıyla aynı
  yer; docker'da web+worker'a mount'lu). `.env` → `FCM_CREDENTIALS`, `FCM_PROJECT_ID=ihale-53fbf`.
- **Elle tetikleme / test**: `python manage.py send_test_push <user_id> [--raw]`,
  `python manage.py run_notifications --job alarms|filters|all`.

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
- **FCM push opsiyonel**: `FCM_CREDENTIALS` boşsa push devre dışıdır (no-op) — uygulama-içi
  `Notification` satırı yine yazılır. Bkz. "Bildirim Servisi (Push)".
- **Firebase Cloud Function bug fix'leri portlandı**: `.doc` net reddedilir,
  Claude bağlantı/timeout hataları güvenli işlenir (bkz. `ai/services/claude.py`).
```
