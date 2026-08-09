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
├── models.py      # Favorite, FavoriteAuthority, SavedFilter, SavedTender, TenderAlarm, Notification
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
├── models.py      # Tender + çocuklar, Contractor/Alias/Membership, OkasCode, Authority (DETSIS), City, Sync*
├── sync.py        # EKAP→DB eşleme + toplama mantığı (çocuk tablolar kararlı anahtarla upsert)
├── detsis_tree.py # İdare ağaç yardımcıları (üst→alt idare_id genişletme, ata-yolu)
├── contractors.py # Yüklenici kimliği: kanonikleştirme, ortak girişim ayrıştırma, agregalar
├── sonuc_ilani.py # Sonuç İlanı (ilanTip=4) HTML ayrıştırıcı — doğru yaklaşık maliyet kaynağı
├── tasks.py       # Celery: sync_recent/detail/refresh_stale/backfill/okas/authorities/contractors
├── views.py       # /ekap/tenders, detail, announcements, contracts, contractors, document-url, okas, authorities, cities
└── management/    # seed_cities, ekap_probe, run_ingest, rebuild_contractors

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
  bir, tur başına `EKAP_BACKFILL_MAX_PAGES` sayfa — **hızın asıl düğmesi budur, throttle
  değil**; ölçüm 2026-08: tur başına tam 500 kayıt işlenirken `ekap` kuyruğu BOŞTU
  (`LLEN ekap=0`), yani 1 istek/sn bütçesinin çoğu kullanılmıyordu → 43.500 kayıt/gün.
  Liste taraması ucuz (50 kayıt/istek); pahalı olan detay istekleri, onlar da yalnızca
  detayı eksik ihaleler için atılıyor. ⚠️ Tavan: tur maliyeti ≈ `max_pages` sn + upsert,
  `CELERY_TASK_TIME_LIMIT=300` yüzünden ~40 güvenli sınır;
  pencere tabanından **ileriye** `ihaleTarihi` **asc** — DB'deki asıl boşluk eski
  yıllar olduğu için önce onları doldurur, en yeni kayıtlar listenin sonuna eklendiğinden
  imleç kaymaz; `skip >= total_count` [pencere içi toplam] ya da boş sayfada `done=True`.
  EKAP gün içinde yavaş/yanıtsız olabildiğinden görev sayfa hatasını **zarifçe yutar**:
  kısmi ilerlemeyi `SyncCheckpoint`'e kaydeder, çalışmayı *error* saymaz — `SyncRun.note`'a
  "EKAP kısmi" düşer — ve bir sonraki tetikte kaldığı yerden devam eder. Kilit=1sa üst üste
  binmeyi, throttle ~1istek/sn + tek concurrency EKAP'ı korur),
  `sync_okas`/`sync_authorities` (haftalık), `sync_contractors` (5/35 dk, EKAP'a gitmez —
  bkz. "Yüklenici (Firma) Kaydı"). Detay `detail_raw`'da tam saklanır;
  ayrı ilan çağrısı yapılmaz (detay zaten `ilanList` içerir → rate limit tasarrufu).
  ⚠️ **Pencere değişince** (`EKAP_BACKFILL_YEARS` veya filtre mantığı) backfill
  checkpoint'i sıfırla ki yeni pencereyle baştan taransın:
  `SyncCheckpoint.objects.filter(name="backfill").update(cursor_skip=0, done=False, oldest_date=None)`.
  Backfill `ihaleTarihi` **asc** gittiği için yeni (daha eski) taban önce taranır → asıl
  boşluk önce dolar. **Detayı zaten çekilmiş ihaleler için detay isteği kuyruğa GİRMEZ**
  (`if tender.detail_synced_at is None`): backfill boşluk doldurur, tazeliği
  `refresh_stale` yönetir. Bu guard olmadan 5→10 yıl geçişi, arşivdeki yüz binlerce
  ihalenin detayını EKAP'tan gereksizce yeniden çeker ve ~1 istek/sn sınırında günler
  harcardı. Detayı hiç gelmemiş eskiler `sync_contractors.enqueue_missing_detail` ile
  yakalanır.
- **Dedup anahtarı = İKN**: `upsert_tender_from_list` satırı **`ikn`'ye göre** upsert eder
  (`ekap_id` değil), `ekap_id`'yi son değere günceller. Çünkü EKAP aynı İKN'yi farklı iç
  `id` ile döndürebilir (yeniden yayım); `ekap_id` ile upsert edilirse aynı İKN farklı
  id'yle gelince `ikn` unique kısıtı ihlal edilip ingest patlardı. Liste döngüleri
  (`sync_recent`/`backfill`) `_upsert_item_safe` ile sarılıdır → tek bozuk kayıt tüm
  çalışmayı düşürmez, `SyncRun.errors`'a sayılır.
- **Servis**: `/api/v1/ekap/tenders/` (DB'den, **EKAP alan isimleriyle** → mobil
  mapper'lar minimal değişir), `tenders/{ekap_id}/` (detay, İKN'de `/` var — key olarak
  `ekap_id` kullan), `okas/search`, `authorities/tree`, `authorities/search`, `cities`,
  `tenders/{id}/document-url` (dinamik → canlı proxy). Kullanıcı aramaları EKAP'a hiç dokunmaz.
- **Doğrulama**: `python manage.py ekap_probe` (canlı imza testi),
  `python manage.py run_ingest --task recent|backfill|okas|authorities|detail`.

### DETSIS İdare Ağacı (kurum seçimi)

EKAP'ın "İdare Seç" ekranındaki gibi **hiyerarşik idare ağacı** (bakanlıklar →
genel müdürlükler → daireler → …). EKAP `DetsisAgaci` bir **lazy-tree**'dir; alanlar:
`detsisNo` (ağaç anahtarı, benzersiz), `parentIdareKimlikKodu` (üst düğümün detsisNo'su;
kök=`0`), `idareId` (**ihale filtre anahtarı** = `Tender.idare_id`; dal düğümünde `null`),
`hasItems` (çocuğu var mı), `seviye`.

- **Model** (`Authority`): `detsis_no`, `parent_detsis`, `idare_id`, `ad`, `has_items`,
  `seviye`. Eski düz `detsis_id/ust_idare/idare_kod` alanları **kaldırıldı** (migration
  `0004_authority_tree` satırları temizleyip şemayı değiştirir → deploy sonrası
  `run_ingest --task authorities` ile yeniden doldur).
- **Sync** (`sync_authorities`, haftalık): **tüm ağaç 3 EKAP isteğiyle** çekilir —
  kökler (`detsis_roots`, parent=0) + tüm bağlı alt düğümler (`detsis_all_descendants`,
  parent>0) + **Bağlantısız Kurumlar** (`detsis_children(-999999)`). `detsis_no` üzerinden
  `bulk_create(update_conflicts)` ile yerinde upsert (~87.5k düğüm).
  - **Bağlantısız Kurumlar** (parent=`-999999`): belediyelerin Bilgi İşlem Müdürlükleri
    gibi ağaca bağlı OLMAYAN ama **ihale açan** (idareId dolu) idareler. `parent>0`
    sorgusu bunları kaçırıyordu → EKAP aramasında görünüp bizde görünmüyorlardı.
    Sentetik bir "Bağlantısız Kurumlar" kökü (`detsis_no="-999999"`) altına yerleştirilir.
- **Uçlar**:
  - `GET /ekap/authorities/tree/?parent=<detsis_no>` — gözat (lazy). `parent` boşsa
    **kökler** (bakanlıklar + Bağlantısız Kurumlar), doluysa o düğümün **doğrudan
    çocukları** (azami `TREE_CHILD_LIMIT=2000` — yalnızca Bağlantısız ~17k'yı aşar,
    orada aramayla bulunur).
  - `GET /ekap/authorities/search/?q=` — ad ile arama; her düğümde `path` (kök→ebeveyn
    ata adları, breadcrumb) döner. Seçilebilir (idare_id dolu) düğümler önce sıralanır.
- **İhale filtreleme = `idare_detsis`** (bkz. `apply_tender_filters`): seçilen `detsis_no`
  düğümleri `detsis_tree.descendant_idare_ids` ile **tüm alt birimlerin `idare_id`'lerine**
  genişletilir (üst düğüm seçilince alt birimlerin ihaleleri de gelir — EKAP tri-state
  karşılığı). Büyük bakanlıklar on binlerce alt birime açıldığından (MEB ~37k okul)
  genişletilen küme **ihalede gerçekten geçen idare_id'lerle** kesiştirilir
  (`_tender_idare_id_set`, cache 5 dk) → küçük/hızlı IN listesi. Eski `idare_id` param'ı
  yaprak (doğrudan) seçim için durur.
- **Mobil**: filtre ekranında "İdare Seç" **butonu** → ayrı sayfa (`AuthoritySelectScreen`,
  lazy ağaç + arama + checkbox) → seçim `detsis_no` olarak geri döner → `idare_detsis` gönderilir.

#### Takip Edilen Firmalar (`tenders.FavoriteContractor`) — rakip takibi

`FavoriteAuthority`'nin birebir ikizi; doğal anahtar `(user, contractor)` (firma kaydı
bizim ürettiğimiz normalize kimlik, dış anahtarı yok). Uçlar:
`GET/POST /favorite-contractors/`, `GET/DELETE /favorite-contractors/<contractor_id>/`.
Mobil yalnızca `contractor` (id) gönderir; ad/istatistik sunucuda zenginleştirilir.

- **Takip etmek her üyeye açık ve sınırsız; bildirim Pro'ya özel** (favori idaredeki
  asimetrinin aynısı). `check_favorite_contractor_matches` (beat **12:00**) premium
  olmayanı atlar.
- **Derin bağlantı**: `Notification.contractor_id` → mobil firma detayını açar
  (`GET /ekap/contractors/<id>/`). Öncelik sırası güncellendi: `conversation_id` >
  `filter_id` > `authority_detsis` > **`contractor_id`** > `okas_kodlar` > `tender_ikn`.
- **"Yeni iş" = `Contract.ilk_gorulme`**, `sozlesme_tarihi` DEĞİL: Sonuç İlanı imzadan
  aylar sonra yayımlanabildiği için eski tarihli bir sözleşme bugün keşfedilebiliyor.
  ⚠️ `ilk_gorulme` **yalnızca yaratma yolunda** yazılır (`_bulk_upsert_children`'ın
  `build` lambda'sı) ve `_CONTRACT_FIELDS`'ta **YOKTUR** — orada olsaydı her
  `refresh_stale` turunda güncellenir, her sözleşme her gün "yeni" görünürdü.
  Eski satırlarda NULL kalır = "eski" (geriye dönük doldurma anlamsız: onları ne zaman
  gördüğümüz kayıtlı değil).
- ⚠️ **Arşiv gürültüsü koruması**: `sync_contractors` süpürmesi daha önce hiç bağlanmamış
  ESKİ sözleşmeleri de bugün "ilk kez" görür. 2021 tarihli bir sözleşme rakip takibi
  haberi değildir → ikinci koşul `sozlesme_tarihi >= now - _RAKIP_TAZELIK_GUN` (90 gün).

#### Free teaser (`weekly_free_teaser`, Pazartesi 10:00)

Günlük alarm görevleri Free kullanıcıyı **sayılmadan** eliyordu → kullanıcı alarm
anahtarını açıyor, hiçbir şey olmuyor, Pro'nun ne işe yaradığını hiç hissetmiyordu.

- **Ayrı haftalık görev**, günlük görevlere Free eklemek yerine: Free tabanını günlük dört
  ağır sorguya sokmak maliyeti tabana orantılı büyütürdü. Bu görev yalnızca **sayı**
  üretir (`.count()`); ihale gövdesi/serializer/liste yok.
- **Sıfır eşleşme → bildirim YOK.** Boş teaser ("0 ihale kaçırdınız") güven kaybettirir.
- Kullanıcı başına **tek** özet (abonelik-başına ayrı push deseninin bilinçli istisnası —
  amaç bilgilendirme değil dönüşüm), atomik **hafta kilidi** `teaser:{uid}:{yıl}-{hafta}`.
- Yalnızca alarmlı filtre/idare kaydı OLAN Free kullanıcılara gider; hiç abonelik
  kurmamış birine "kaçırdıklarınız" demek anlamsız olurdu.
- `type=INFO`, derin bağlantı alanı YOK → mobil Paywall'a yönlendirir.
- ⚠️ Sayılar **gerçek** olmalı; abartılmış teaser kullanıcı Pro olup karşılığını
  göremeyince güveni kalıcı bozar.

#### Favori İdareler (`tenders.FavoriteAuthority`)

Kullanıcı bir idareyi (DETSIS düğümü) favorileyebilir; favoriye basınca mobil o idarenin
ihalelerini `GET /ekap/tenders/?idare_detsis=<detsis_no>` ile listeler. Favorileme
**sınırsızdır** (Free + Pro). Mevcut favorinin upsert'i sorun değil.

- **Model**: doğal anahtar `(user, detsis_no)` unique. `detsis_no` ASCII → `/` içermez,
  yol parametresi `<str:detsis_no>` yeterli (İKN'deki `path:` gerekmez). `ad`/`idare_id`/
  `has_items` **sunucuda `ekap.Authority`'den zenginleştirilir** (`_enrich_authority`);
  mobil yalnızca `detsis_no` gönderir. `alarm` (bool, vars. `True`) yazılabilir;
  `last_notified_at` alarm görevinin izidir.
- **Alarm** (`alarm=True`, vars. açık): favori idare **yeni bir ihale yayınladığında**
  kullanıcıya bildirim gider — `check_favorite_authority_matches` beat görevi (her gün
  **11:00**), kayıtlı-filtre eşleşmesiyle **aynı desen**. Fark: filtre yerine idare;
  seçilen `detsis_no` `descendant_idare_ids` ile tüm alt birimlerin `idare_id`'lerine
  genişletilir (ihale/tarama uçlarıyla ortak), açık + **`ilan_tarihi` BUGÜN olan** ihaleler
  bulunur. Uygulama-içi satır `type=TENDER` + **`authority_detsis`
  dolu** yazılır → mobil bildirime basınca **tek ihale DEĞİL**, o idarenin ihale listesini
  (`idare_detsis=<detsis_no>`) açar (`tender_ikn` yalnızca dedup için yazılır, mobil
  `authority_detsis`'i önceler). Kullanıcı başına tek özet push. **Alarm Pro'ya özeldir**:
  görev premium olmayan kullanıcıyı atlar (favorileme yine serbest — bkz. Premium bölümü).
  Elle tetik: `python manage.py run_notifications --job authorities`.
- **Uçlar** (`/api/v1/...`, hepsi JWT):
  - `GET favorite-authorities/` — favori idareleri listele.
  - `POST favorite-authorities/` — gövde `{"detsis_no": "24308110", "alarm": true}`; aynı
    `detsis_no` tekrar → upsert (hata yok). `alarm:false` = yalnızca hızlı erişim, bildirim yok.
  - `DELETE favorite-authorities/<detsis_no>/` — favoriden çıkar (idempotent, 204).
  - `GET favorite-authorities/<detsis_no>/` — `{is_favorite: bool}`.

### Türkçe arama normalizasyonu (kritik)

DB `icontains`/ILIKE **Türkçe büyük/küçük harf katlaması yapmaz** (İ↔i, ş↔s...).
Kullanıcı küçük harf Türkçe yazınca ("bilgi işlem") arama **boş** dönüyordu; EKAP ise
Türkçe-duyarlı eşleşiyordu. Çözüm: `ekap.utils.normalize_tr` — metni **Türkçe+aksan
duyarsız** biçime indirger (`İ/I/ı/i→i`, `ş→s`, `ğ→g`, `ü→u`, `ö→o`, `ç→c`, lowercase).
Böylece "bilgi işlem" == "BİLGİ İŞLEM" == "bilgi islem" (EKAP'tan bile esnek — ascii de bulur).

- **Nasıl**: modele normalize sütunu eklenir (senkronda doldurulur), sorgu da normalize
  edilip `__contains` ile aranır (iki taraf da lowercase ASCII → **DB-bağımsız**, indeksli
  arama mümkün). Sütunlar: `Authority.ad_norm`, `OkasCode.adi_norm`,
  `Tender.ihale_adi_norm` + `idare_adi_norm` (migration `0005_search_norm`, batched backfill).
- **Kullanan yerler**: `apply_tender_filters` (`q`, `ihale_adi`, `idare_adi`),
  `AuthoritySearchView`, `OkasSearchView`, ve asistan `matching.py` keyword eşleştirmesi.
  OKAS kodu ASCII olduğundan `startswith` bırakıldı.
- **Yeni metin araması eklerken**: Türkçe alanda `icontains` KULLANMA → normalize sütunu
  ekle + `normalize_tr(sorgu)` ile `__contains`. Bilinen istisna (henüz düzeltilmedi):
  `okas_adi` filtresi `OkasItem.adi__icontains` kullanır (mobil `okas_kod` gönderdiği için
  pratikte tetiklenmez).
- **⚠️ `icontains` ASCII alanda bile indeksi ÖLDÜRÜR.** Django Postgres'te `icontains`'i
  `UPPER(kolon::text) LIKE UPPER(%s)` diye derler; kolonun üstündeki `UPPER()` yüzünden o
  kolondaki trigram indeksi kullanılamaz. Bu yüzden `ikn` filtreleri `__contains` kullanır
  (İKN saf ASCII → davranış birebir aynı). **Bunu geri `icontains`'e çevirmeyin.**
- **⚠️ OR'lu arama: TEK indekssiz dal HEPSİNİ düşürür.** `q` filtresi
  `ihale_adi_norm | idare_adi_norm | ikn` üçlüsünü OR'lar; Postgres BitmapOr'u ancak **her
  dal** indekslenebiliyorsa kurar. Bu yüzden `ikn`'ye de trigram indeksi verildi — biri
  eksik olsaydı diğer ikisi de boşa giderdi. `q`'ya yeni bir OR dalı eklerken o dalın da
  indekslenebilir olduğundan emin olun.
- **Trigram indeksleri** (`0007_search_indexes`): `Tender.ihale_adi_norm`,
  `idare_adi_norm`, `ikn`, `OkasItem.adi` → `GinIndex(opclasses=["gin_trgm_ops"])`
  + `pg_trgm` extension. `LIKE '%…%'` baştan joker olduğu için **düz B-tree işe yaramaz**,
  trigram şarttır. ⚠️ Trigram **en az 3 karakter** ister; 1-2 harflik sorgu yine seq scan'e
  düşer (sonuç doğru, sadece yavaş) — mobilde min 3 karakter dayatmak iyi olur.

### İhale arama performansı (kritik — dokunmadan önce okuyun)

`ekap_tender` ~500k satır (backfill tüm gün çalıştığı için 60k'dan buraya büyüdü). Bu
boyutta "küçük veride sorun çıkarmayan" desenler ucu saniyelere çıkarır. Yaşanmış hatalar
ve konan korumalar:

- **⚠️ OR'un HER dalı indekslenebilir VE aynı tabloda olmalı.** Postgres BitmapOr'u
  ancak böyle kurar; tek indekssiz ya da başka tablodaki dal, diğer dalların
  indekslerini de devre dışı bırakır. İki kez ısırdı:
  - `q` filtresi (`ihale_adi_norm | idare_adi_norm | ikn`) — `ikn__icontains`
    `UPPER(ikn) LIKE …` ürettiği için indekslenemiyordu, üçü birden seq scan'e
    düşüyordu → `__contains` + `ikn` trigramı.
  - `yuklenici` metin filtresi — `arama_norm` `ekap_contractor`'da,
    `yuklenici_adi_norm` `ekap_contract`'ta. OR iki tabloya yayıldığı için Postgres
    840k sözleşmeyi 94k firmayla join edip **sonra** filtreliyordu (~1.2 sn, 79 satır
    için 840k tarama). Çözüm: iki dalı **ayrı ayrı** indeksten çözüp `UNION`'la
    birleştirmek (`pk__in=A.union(B)`) → 222 ms.
    ⚠️ Ölçülmüş çıkmaz sokak: aynı OR'u tek `Exists` içine `yuklenici_id IN (alt sorgu)`
    olarak indirmek **109 sn** sürdü (planlayıcı ihale başına korelasyonlu tarama
    seçiyor). Bu alanda **EXPLAIN'siz kurgu değiştirmeyin**.
- **`.distinct()` KULLANMAYIN → `Exists(OuterRef("pk"))`.** Çoklu-satır ilişkiye
  (`okas_kalemleri`, `sozlesmeler`) `.filter(ilişki__…)` ile bağlanmak ihaleyi çoğaltır ve
  eskiden `.distinct()` ile toplanırdı. `DISTINCT` **`LIMIT`'ten önce** çalışır → sayfa
  boyutu maliyeti kurtarmaz; Postgres eşleşen TÜM satırları (üstelik `detail_raw`/`list_raw`
  JSONB'leriyle) sort/hash'ten geçirir. `Exists` semi-join'i satır çoğalmasını hiç doğurmaz.
  Yardımcılar: `ekap.views._okas_exists`, `_contract_exists`.
- **Liste ucu `.only(_LIST_FIELDS)` kullanır.** `detail_raw` + `list_raw` (~40 KB/satır)
  serializer'da hiç kullanılmıyor ama `SELECT *` her satırda TOAST'tan açıp `json.loads`
  ediyordu. ⚠️ `EkapTenderListSerializer`'a alan eklerseniz `views._LIST_FIELDS`'a da
  ekleyin, yoksa deferred alan başına ek sorgu atılır (sessiz N+1).
  ⚠️ `.only()`'i `apply_tender_filters` İÇİNE koymayın — o fonksiyon bildirim
  görevlerinde de kullanılıyor ve orada başka alanlar okunuyor.
- **`totalCount` cache'li** (`views._cached_count`; TTL `SEARCH_COUNT_CACHE_TTL`, vars.
  **600 sn**). Anahtar yalnızca **filtre** parametrelerinden üretilir;
  `page`/`page_size`/`order`/`siralamaTipi` dışlanır → sayfa gezinmesi tek `COUNT` yapar.
  Deny-list kullanılır (allow-list olsaydı yeni bir filtre unutulunca iki farklı arama
  aynı anahtara düşüp **yanlış** sayı dönerdi).
- **⚠️ `ORDER BY <tarih> DESC LIMIT N` + seyrek filtre = plan tuzağı.** Planlayıcı
  sıralama+LIMIT'i görünce tarih indeksini **geriye tarayıp filtrelemeyi** seçiyor;
  eşleşen satır seyrekse N tane bulana kadar neredeyse tüm tabloyu geziyor. Üç kez
  ısırdı, üçü de farklı çözüm istedi:
  - İhale **detay** ucu (`.first()` + `Meta.ordering`) → sıralamayı düşür
    (`_tender_by_key`); tek satır döndüğü için sıralamanın anlamı yok.
  - `_tender_idare_id_set` (`.distinct()` + `Meta.ordering`) → `.order_by()`.
  - `enqueue_missing_detail` / `refresh_stale`
    (`detail_synced_at IS NULL ORDER BY tarih DESC LIMIT 50`) → sıralama **gerekli**
    olduğu için **kısmi indeks** (`0009`, `condition=Q(detail_synced_at__isnull=True)`).
    Bu tek sorgu `pg_stat_statements`'ta **çağrı başına 78 sn / toplam 6.3 saat** DB
    zamanı yiyordu.
  Yeni bir `ORDER BY ... LIMIT` + seçici filtre yazarken **EXPLAIN'e bakın**.
- **`_tender_idare_id_set()` içindeki `.order_by()` ŞART.** `Tender.Meta.ordering`
  (`-ihale_tarihi`) + `.distinct()` birleşince Django ORDER BY kolonunu SELECT'e ekler →
  `DISTINCT (idare_id, ihale_tarihi)` olur, birkaç bin satır yerine tablonun TAMAMI dönüp
  Python'da tekilleştirilirdi. Aynı tuzak `descendant_idare_ids` BFS'inde de var
  (`Authority.Meta.ordering = ["ad"]`) — oraya da `.order_by()` konuldu.
- **`ilan_tarihi__date=gun` KULLANMAYIN → `ekap.utils.local_day_range`.** `USE_TZ` ile
  Django bunu `(ilan_tarihi AT TIME ZONE …)::date = …` diye derler; kolonun üstündeki
  fonksiyon indeksi öldürür. Bildirim görevleri kullanıcı saatlerinde (10:00/11:00)
  koştuğu için bu taramalar doğrudan aramayla I/O yarışıyordu.
- **`sync_contractors` SÜPÜRMESİ yalnızca gece çalışır** (`CONTRACTOR_SWEEP_START/END`,
  vars. 00:00–07:00). Süpürme tüm arşivin `detail_raw`'ını (~40 KB/satır) okur; 3 GB'lık
  makinede 512 MB `shared_buffers` bunu kaldırmıyor ve arama sorgularının çalışma kümesi
  sürekli eviction'a uğruyordu (ölçüm: **heap cache isabeti %53**, olması gereken >%99).
  Duty cycle da 5 dk × 240 sn (~%80) → 10 dk × 90 sn (~%15) yapıldı, `.only()` eklendi.
  **Artımlı mod pencereden bağımsızdır** — `refresh_stale`'in tazelediği birkaç yüz
  satıra dokunur, ucuzdur. Süpürme uzar (gece-only ~2 hafta) ama arka plan
  zenginleştirmesidir, kullanıcıyı bekletmez.
  ⚠️ Sunucu 8 GB'a çıkarsa `docker-compose.yml`'de `shared_buffers=2GB` yapıp bu
  pencereyi genişletmek mantıklı olur.
- **Postgres tuning `docker-compose.yml` → `db` → `command:` içindedir**, `.env.prod`'da
  DEĞİL: compose'un `${VAR}` ikamesi `env_file:`'dan okumaz (o yalnızca konteyner içi
  ortam değişkeni tanımlar), kabuk ortamından ya da `.env`'den okur. Oraya yazılan bir
  `PG_*` sessizce yok sayılırdı. RAM'e göre değer tablosu dosyadaki yorumda.
  **Üretim donanımı: 8 GB RAM · 4 çekirdek · 100 GB RAID10 SSD (VPS).**
  Ayarlar buna göredir (`shared_buffers=2GB`, `effective_cache_size=5GB`,
  paralel sorgu 4 çekirdeğe göre). Donanım değişirse tabloya bakın.
  ⚠️ Bu ayarlar **db konteynerinin yeniden başlamasını** gerektirir (~30 sn);
  `shared_buffers` ve `shared_preload_libraries` runtime'da değişmez.

### Pro sinyal kolonları — `detail_raw`'dan türetilenler

`Tender.detail_raw` içinde kolona alınmamış çok veri vardı; fiyat analizi / idare profili
/ pazar panosu bunlara dayanıyor. **EKAP'a hiç istek atılmaz** — hepsi arşivden türetilir.

- **Tek çıkarım kaynağı = `sync.apply_pro_fields(tender, data)`**. Hem canlı detay senkronu
  (`upsert_tender_detail`, `save()`'den hemen önce) hem geriye dönük doldurma görevi bunu
  çağırır. İki ayrı çıkarım yazılsaydı arşiv ile yeni kayıtlar sessizce farklı semantiğe
  kayardı. Yazdığı alanların listesi `sync.PRO_TENDER_FIELDS` — **alan eklerken ikisini de
  güncelleyin**, yoksa backfill o alanı hiç yazmaz.
- **Kolonlar**: `okas_ana_kod`/`okas_ana_adi`/`okas_bucket`/`okas_kalem_sayisi`,
  `en_ust_idare_kod`/`en_ust_idare_adi`, `istekli_sayisi`, `itirazen_sikayet_var`,
  `idareye_sikayet_var`, `sikayet_dilekce_var`, `fiyat_disi_unsur_var`,
  `e_eksiltme_yapilacak`, `duzeltme_ilani_var`, `kismi_ihale`, `ilansiz_mi`, `seri_anahtar`.
- ⚠️ **`ihaleBilgi.okas` yeni bilgi DEĞİL** — `ihtiyacKalemiOkasList`'in
  `", ".join(koduAdi)` hâli. `okas_ana_kod` **`ihtiyacKalemiOkasList[0].kodu`**'dan
  türetilir; string yalnızca liste boşsa yedek olarak ayrıştırılır. Değeri *denormalizasyon*:
  çok satırlı `OkasItem` yerine tek değerli, gruplanabilir kolon.
  **OKAS kod uzunluğu sabit değil** (canlı veride 8 ve 9 hane bir arada) → `max_length=16`.
- ⚠️ **`istekli_sayisi` boş liste = "bilinmiyor", sıfır DEĞİL.** `tebligatAlanIstekliList`
  **değerlendirme sonrası** dolar; açık ihalede daima `[]` gelir (doğrulandı: 2026
  ihalelerinde 0, sonuçlananlarda 3 ve 13). Bu yüzden `len(...) or None` yazılır — `0`
  yazılsaydı "hiç istekli çıkmadı" gibi okunurdu.
- ⚠️ **Bayraklar ÜÇ DEĞERLİ** (`BooleanField(null=True)`): `NULL` = "detay ayrıştırılmadı".
  Çıkarımda `k.get(key, False)` **KULLANMAYIN** (`sync._ucdeger` bunu doğru yapar) —
  bilinmeyeni "hayır"a çevirir ve "itirazsız ihaleler" filtresi detayı gelmemişleri de toplar.
- ⚠️ **`duzeltme_ilani_var` şüpheli**: örneklenen 4 ihalenin dördünde de `True`. Anlamı
  "düzeltme ilanı yayımlandı" olmayabilir. **Ürün özelliği yapmadan önce prod'da dağılımını
  ölçün**; şimdilik yalnızca saklanıyor.
- **`idare.enUstIdareKod/Adi` = bakanlık rollup.** Bu alanlar eskiden **her senkronda
  atılıyordu**: `ust_idare` alanı `ustIdare or enUstIdareAdi` yazıyor ve `ustIdare` genelde
  dolu ama değersiz (canlı örnek: `ustIdare="BAKAN YARDIMCILIKLARI"` kazanıyor,
  `enUstIdareAdi="TARIM VE ORMAN BAKANLIĞI"` düşüyordu). `en_ust_idare_kod`, DETSIS alt
  ağacını `descendant_idare_ids()` ile on binlerce `idare_id`'ye açmak yerine **tek indeksli
  eşitliğe** indirger. `ust_idare` bilerek değiştirilmedi (mobil okuyor olabilir).
- **Sözleşme özeti** (`Tender.sozlesme_sayisi`, `toplam_sozlesme_bedeli`):
  `sync_contracts_from_raw` zaten aynı satırı `update()` ediyor ve değerler elinde →
  `sync_contractors` süpürmesi bunları **bedavaya** doldurur. Amaç: "sonuçlanmış mı /
  bedel şu aralıkta mı" filtreleri korelasyonlu `Exists(Contract…)` yerine düz,
  indekslenebilir `Tender` kolonu kullansın (OR-indekslenebilirlik kuralı için şart).
  Sıfır-sözleşme dalı da sıfırlar; `toplam_sozlesme_bedeli` hiç bedel yoksa `NULL` kalır
  (0 "bedel sıfır" demek olurdu).
- **Seri anahtarı** (`seri_anahtar`, `ekap/series.py`): aynı idarenin yıldan yıla
  tekrarladığı işi tanır ("2024 YILI TEKSTİL … ALIMI" ≡ "2025 YILI TEKSTİL … ALIM İŞİ").
  Ad iskeleti = yıl/rakam/stopword atılıp token'lar sıralanır, sonra
  `sha1(idare_id|okas_ana_kod|iskelet)`.
  ⚠️ **Trigram self-join bilinçli olarak REDDEDİLDİ**: idare bazlı `similarity()` self-join
  O(k²) ve büyük alıcılarda k on binler; bir milyon probe saatlerce CPU **ve** arama çalışma
  kümesinin boşalması demek (yüklenici süpürmesindeki %53 cache isabeti arızasının aynısı).
  ⚠️ **`apply_pro_fields` İÇİNDE üretilir** — arşivin `detail_raw`'ı bir kez okunsun diye.
  Sonradan eklemek tek bir `varchar(40)` için 40 GB TOAST üzerinde **ikinci bir haftalar
  süren gece taraması** demekti.
  ⚠️ Yanlış birleştirme kaçırılandan kötüdür (`contractors.py` ile aynı değer): aynı
  `idare_id` **ve** `okas_ana_kod` şartı + iskelette **en az 2 anlamlı token**; yoksa
  anahtar üretilmez ve ihale hiçbir seriye girmez.

#### Tekrar eden ihaleler — `GET /ekap/recurring/`, `.../tenders/<key>/recurring/`

Kamu alımlarının büyük kısmı **yıllık tekrarlar**. Arşiv bunu görebildiği için kullanıcı
**ilan çıkmadan önce** haberdar olup hazırlanabilir — rakiplerin sunamadığı bir şey.

- **Anahtar ingest'te üretilir** (`series.series_key` → `apply_pro_fields`); tespit görevi
  yalnızca indeksli `varchar(40)` üzerinde GROUP BY yapar. Metin karşılaştırması yok.
  ⚠️ Trigram self-join **bilinçli olarak reddedildi** (bkz. `ekap/series.py`).
- **`detect_recurring_series`** — haftalık (Pazar 02:30), `celery` kuyruğu.
  ⚠️ `.values()` kullanır → `detail_raw` TOAST'ına **hiç dokunmaz**, dolayısıyla
  `sync_contractors`/`backfill_tender_fields` ile pencere çakışması sorunu YOK.
- **Periyot = aralıkların MEDYANI**, ortalaması değil: tek bir sıra dışı aralık ortalamayı
  kaydırırdı. `_periyot_tipi` takvim kaymalarına toleranslı sınırlar kullanır
  (330-400 gün → yıllık).
- ⚠️ **`guven` yalnızca üye sayısına bakmaz, DÜZENLİLİĞE de bakar** (`sapma/medyan`):
  5 üyeli ama aralıkları 30/400/60/380 gün olan bir "seri" tahmin üretmemeli.
  `yuksek` = ≥4 üye ve sapma/medyan ≤ 0.15.
- **`aktif`**: son ilandan bu yana 2 periyottan fazla geçtiyse seri sona ermiştir
  (ihtiyaç kalkmış / usul değişmiş) → liste varsayılan olarak yalnızca aktifleri döner.
- Upsert + buda: turda dokunulmayan eski seriler silinir (iskelet değişip artık oluşmayan
  gruplar). ⚠️ Budama yalnızca **süre dolmadıysa** yapılır; yarım turda budamak hayatta
  olan serileri silerdi.
- Para agregaları **ayrı adımda** (`_seri_para_agregalari`) — ana döngüde seri başına
  sorgu atmak N+1 olurdu.

#### İdare (alıcı) profili — `GET /ekap/authorities/profile/`

"Bu kurum ne alıyor, kime, kaça?" Ürün bugüne kadar yalnızca *ihaleyi* gösteriyordu;
alıcının davranışı teklif verecek firma için en az ihale kadar önemli. Mantık
`ekap/authority_profile.py`'de.

- **Kapsam üç yoldan biriyle** (`kapsam_coz`, öncelik sırası bilinçli):
  `en_ust_idare_kod` (**en ucuz** — tek indeksli eşitlik) > `idare_id` (yaprak) >
  `idare_detsis` (alt ağaç; `descendant_idare_ids ∩ tender_idare_id_set`, arama ucuyla
  ortak mantık).
- **`Kapsam` sınıfı `tender_q` ve `contract_q`'yu AYRI tutar.** `Contract` kendi
  `idare_id` ingest-kopyasını taşıdığı için o kapsamlarda `ekap_tender` JOIN'i yapılmaz
  (840k+ satırda kritik); `en_ust_idare_kod` ise henüz `Contract`'a kopyalanmadığından
  `tender__` üzerinden gider. Tek Q'dan türetmeye çalışmak (`kosul.children[0]`)
  kırılgan olurdu.
- **Canlı hesap, materialize değil** — `ContractorDetailView._dagilim` ile aynı gerekçe:
  tek idarenin sözleşmeleri yüzler mertebesinde. ⚠️ Ölçek istisnası: DETSIS alt ağacı on
  binlerce `idare_id`'ye açılabilir ve o `IN` listesi planlayıcıyı bozar →
  `PROFILE_MAX_IDARE` (2000) aşılırsa **ayrıntılı kırılım hesaplanmaz**, özet + yıllık
  seri döner ve `kapsam.cok_genis` + `mesaj` ile bildirilir.
- **Yoğunlaşma (HHI)** = Σ(pay²); 1'e yakın = tek firma hâkimiyeti. Firma sayısı
  `HHI_MAX_FIRMA`'yı (5000) aşarsa `yaklasik: True` döner — kesilmiş bir küme üzerinden
  hesaplanan HHI tam değildir.
- ⚠️ **`itiraz_orani`'nın paydası TÜM ihaleler DEĞİL**, bayrağı **bilinen** ihalelerdir
  (`Count("itirazen_sikayet_var")`): detayı gelmemiş kayıtlarda bayrak NULL ve onları
  "itirazsız" saymak oranı sistematik olarak düşük gösterirdi. Yanıt
  `itiraz_ornek_sayisi` ile birlikte döner.
- **Ortalamalar her zaman örneklem sayısıyla** (`ortalama_indirim` +
  `indirim_ornek_sayisi`, `ortalama_istekli_sayisi` + `istekli_ornek_sayisi`) — kapsam
  kısmi olduğu için tek başına gösterilirse yanıltır.
- **`.order_by()` her agregasyonda ŞART** (Meta.ordering GROUP BY'a sızmasın).
- **JSONB `contains` (e-ihale payı) yalnızca PostgreSQL'de** — SQLite'a düşen yerel
  geliştirmede o sayım atlanır (`_jsonb_var`), uç 500 vermez.
- ⚠️ **TODO (doldurma sonrası):** `en_ust_idare_kod` ve `okas_ana_kod` `Contract`'a
  ingest-kopyası olarak taşınmalı → bakanlık kapsamı ve OKAS kırılımı da JOIN'siz olur.

#### Fiyat istihbaratı — `GET /ekap/tenders/<key>/benchmark/`

"Kaça verilir?" — açık bir ihaleye benzer, **sonuçlanmış** işlerin kazanan bedel/indirim
dağılımı, beklenen rekabet ve karşılaştırma listesi. Mantık `ekap/benchmark.py`'de
(HTTP'den bağımsız, test edilebilir), yüzdelikler `ekap/aggregates.py`'de.

- **Benzerlik = genişleyen merdiven**, tek tanım değil: `idare` → `il` → `ulke` → `grup`
  → `idare_tur`. Örneklem yeterli olana kadar genişler ve **kullanılan kademe yanıtta
  `kapsam.seviye` ile döner** (kullanıcı "bu idarede" mi "Türkiye genelinde" mi baktığını
  bilmeli). Tek sabit tanım ya n=2 verir ya hastane tekstilini köy yoluyla karşılaştırır.
  ⚠️ Kademe **`idare_tur`** (OKAS'sız) şart: üretimde ölçüldü, **ihalelerin ~%19'unda
  OKAS kalemi yok** → o ihalelerde diğer kademeler hiç eşleşmez.
- **Para için `PERCENTILE_DISC`, oran için `PERCENTILE_CONT`.** `PERCENTILE_CONT` yalnızca
  `double precision` alır; `NUMERIC(20,2)` bir bedel sessizce float'a çevrilir, iki değer
  arasında enterpolasyon yapılır ve **hiç var olmamış** bir tutar döner. `DISC` gerçek bir
  satırın değerini verir — "tipik sözleşme bedeli" için de dürüst cevap budur.
- **Dizi formu** (`ARRAY[0.25,0.5,0.75]`) tek sort yapar; üç ayrı çağrı farklı direct
  argument taşıdığı için **üç bağımsız sort** üretirdi.
- ⚠️ **Şablona `%(filter)s` YAZILMAZ.** Django o yer tutucuyu yalnızca `filter=`
  verildiğinde `extra_context`'e koyar; koşulsuz durursa filtresiz her çağrı
  `KeyError: 'filter'` ile patlar. Doğru mekanizma `filter_template` (varsayılanı bu
  agregalarda olduğu gibi çalışır — `… WITHIN GROUP (…) FILTER (WHERE …)` geçerli SQL).
- ⚠️ **`%(percentile)s` ham SQL'e gömülür** (ordered-set agregalarında direct argument
  parametre olamaz) → yüzdelik listesi **yalnızca modül sabitlerinden** gelir, asla
  `query_params`'tan.
- **Yüzdelikler yalnızca PostgreSQL'de** eklenir; SQLite'a düşen yerel geliştirmede
  sayım/ortalama yine döner (`aggregates.percentile_destekleniyor`).
- **Dürüstlük** (ürün açısından kritik): her dağılım `ornek.indirim_ornek_sayisi` +
  `ornek.guven` ile birlikte gelir; `yeterli_veri=false` ise dağılım gösterilmez.
  **Yıllar arası tek para medyanı verilmez** (TL enflasyonu) → `yillara_gore` her zaman
  döner, varsayılan pencere **5 yıl** (3 değil: 2024-2025 backfill sürerken seyrek).
- **Free = maskeleme, 403 DEĞİL**: uç `200` + `kilitli: true` döner, örneklem sayıları
  görünür ama değerler `null`. "47 benzer iş bulundu · medyan indirim %••,•" teaser'ı
  dönüşümü artırır. ⚠️ Mobil bunu 403'ten **ayrı** ele almalı: 403 doğrudan Paywall'a
  atlar, `kilitli` maskeli gösterip dokunuşta Paywall açar.
- ⚠️ `benzer_ihaleler` listesi `.select_related` + **`.defer(tender__detail_raw/list_raw)`**
  kullanır — sözleşme uçlarında yaşanan TOAST hatasının aynısı burada da olurdu.
- ⚠️ **TODO (doldurma sonrası):** OKAS'lı kademeler şimdilik `tender__okas_ana_kod`
  üzerinden JOIN yapıyor. `Tender.okas_ana_kod` dolunca `Contract`'a ingest-kopyası
  olarak taşınmalı (mevcut `idare_id`/`il_id`/`ihale_tip` gibi) → tüm kademeler tek tablo
  olur. **Şimdi taşınamaz**: kaynak kolon henüz boş, kopyalanacak veri yok.

#### Pro arama filtreleri (`_PRO_PARAMS`)

Gelişmiş filtreler Pro'ya kilitlidir; **temel arama herkese açık kalır** (uç hâlâ
`AllowAny`). `TenderListView` istekte `_PRO_PARAMS`'tan biri varsa `require_premium`
çağırır → `403` + `errors.code=premium_required` (mobilin zaten işlediği sözleşme).

- ⚠️ **Parametreyi sessizce yok saymak YANLIŞ olurdu**: kullanıcının istediğinden *daha
  fazla* sonuç dönerdi — limit kılığına girmiş bir doğruluk hatası. Açık 403 doğrusu.
- Filtreler: tutar aralıkları (`yaklasik_maliyet_*`, `sozlesme_bedeli_*`), rekabet
  (`teklif_sayisi_*`, `istekli_sayisi_*`), `indirim_orani_*`, `sonuclanmis`, `iptal`,
  `e_ihale`, sinyal bayrakları (`fiyat_disi_unsur_var`, `itirazen_sikayet_var`, …),
  `okas_ana_kod`, `en_ust_idare_kod`, `seri_anahtar`.
- ⚠️ **`_PRO_PARAMS` ile `_PRO_SCHEMA_PARAMS` aynı adları taşımalı.** Biri güncellenip
  diğeri unutulursa uç ya belgelenmemiş bir filtre kabul eder ya da belgelenen bir filtre
  403 vermez. (Testi kolay: iki kümenin farkı boş olmalı.)
- ⚠️ **`_PRO_PARAMS`'ı `_COUNT_IGNORED_PARAMS` ile karıştırmayın.** İkincisi cache
  anahtarından **dışlananlar**dır; Pro filtreleri sonucu değiştirdiği için anahtara
  **girmelidir**.
- **Üç değerli bayraklarda `False` istenince `exclude(True)` DEĞİL `filter(False)`**
  kullanılır: `exclude` NULL'ları (detayı gelmemiş ihaleler) da toplar ve "itirazsız
  ihaleler" listesi bilinmeyenlerle şişerdi. `_as_bool` bu yüzden üç değerli döner —
  `None` ("parametre yok") ile `False` ("hayır olanlar") ayrı davranır.
- **`e_ihale` mevcut JSONB etiketini kullanır** (`ozellikler__contains=["E_IHALE"]`),
  `Tender.e_ihale` boolean'ını DEĞİL: o kolon indekssiz ve tutarsız doluyor
  (`sync.py` yalnızca payload'da varsa yazıyor), `ozellikler` ise GIN indeksli.
- **Dürüstlük uyarısı**: kapsamı kısmi kolonlarda (`yaklasik_maliyet_num`,
  `teklif_sayisi`, `istekli_sayisi`) aralık filtresi, değeri **bilinmeyen** ihaleleri de
  sessizce eler (NULL hiçbir aralığa girmez). Bu filtreler kullanılınca yanıt
  `data.uyari` taşır → istemci "sonuç yok" ile "veri yok"u ayırt edebilsin.
- ⚠️ **İndeksler henüz YOK.** Doldurma bitmeden kurulursa ~1M satırlık UPDATE onları
  şişirir. `AddIndexConcurrently` doldurma tamamlanınca eklenecek; o zamana kadar dar
  aralıklı sorgular için **`EXPLAIN (ANALYZE, BUFFERS)` şart** (`ORDER BY tarih DESC
  LIMIT N` + seçici filtre bu kod tabanını üç kez ısırmış plan tuzağıdır).

#### Doldurma: `backfill_tender_fields`

`sync_contractors`'ın ikizi — zaman bütçeli, PK imleçli, gece pencereli arşiv taraması.
Görev + elle komut (`python manage.py backfill_tender_fields [--dry-run] [--limit N]
[--from-pk N] [--restart]`) **aynı `tender_fields` checkpoint'ini paylaşır**.

- ⚠️ **Yüklenici süpürmesine ÖNCELİK verir**: `SyncCheckpoint(name="contractors").done`
  olmadan çalışmaz, her tetikte bedavaya "atlandı" döner.
- ⚠️ **Atlama kontrolleri `_run`'DAN ÖNCE yapılır** → iş yapılmayan turda `SyncRun` satırı
  **yazılmaz**. Görev 5 dk'da bir tetiklendiği için aksi hâlde günde ~288 boş kayıt
  birikiyordu; admin'de hepsi `ok / 0 / 0` görünüp sebebi göstermediğinden gerçek
  çalışmalar kayboluyor ve "görev hiç çalışmamış" izlenimi doğuyordu. Atlama sebepleri
  log'a ve dönüş değerine yazılır. Gerekçe: ikisi de arşivin
  `detail_raw`'ını (~40 KB/satır) okur; aynı gecede koşarlarsa TOAST trafiği ikiye katlanır
  ve **ikisi de** yarı hızda ilerler. Yüklenici süpürmesinin bitmesi `indirim_orani` +
  `sozlesme_sayisi` + `toplam_sozlesme_bedeli` alanlarını doldurduğu için önce o bitirilir.
  Süpürme bitince bu görev kendiliğinden devralır.
- `celery` kuyruğuna yönlendirilir (EKAP'a gitmiyor) — `settings.py`'de `ekap.tasks.*`
  joker'inden **ÖNCE** gelmeli.
- `.only("pk", "detail_raw", "idare_id", "ihale_adi")` — son ikisi **şart**, `seri_anahtar`
  onları kullanıyor; eksik kalsalar alan başına ek sorgu atılırdı (sessiz N+1).
  `list_raw` bilerek dışarıda → taranan TOAST hacmi yarıya iner.
- İmleç `try`'dan **önce** ilerletilir (bozuk tek satır imleci kilitlemesin).
- Ayarlar: `PRO_BACKFILL_START/END` (vars. 0–7), `PRO_BACKFILL_MAX_SECONDS` (270,
  `CELERY_TASK_TIME_LIMIT=300` altında).

### Yüklenici (Firma) Kaydı — `ekap.Contractor`

Sözleşme imzalayan yükleniciler normalize firma kayıtlarına dönüştürülür; her `Contract`
o firmaya bağlanır. Böylece "hangi firma hangi işleri aldı", geçmişi, indirim oranı,
hangi idarelerle çalıştığı sorgulanabilir.

- **Kimlik = normalize ünvan** (`contractors.canonical_key`). EKAP payload'ında
  **VKN/vergi no/TCKN YOKTUR** — başka kimlik kaynağı yok. Boru hattı: Unicode+Türkçe
  katlama → **noktalı** kısaltma açma (`İNŞ.`→insaat, `TİC.`→ticaret; nokta şartı
  güvenlik içindir, çıplak `İT` dokunulmaz) → noktalama temizliği → tüzel biçim
  tekilleştirme (`LİMİTED ŞİRKETİ`≡`LTD.ŞTİ.`→`ltdsti`). **Tüzel biçim SİLİNMEZ** —
  `X LTD ŞTİ` ile `X A.Ş.` farklı tüzel kişilerdir.
  ⚠️ Bulanık/edit-distance eşleştirme **yoktur**: yanlış-birleştirme geri dönülmez şekilde
  geçmişi kirletir, yanlış-ayırma ise alias tablosundan görülüp düzeltilebilir.
- **Yazım varyantları = `ContractorAlias`** (`ham_ad_norm` GLOBAL unique → hem O(1) ingest
  cache hem "bir yazım tek firmaya gider" DB garantisi). `kisimItemDto.yuklenici` ve sonuç
  ilanının `istekliAdi`'sı **bağımsız çözülmez** — aynı sözleşme satırında oldukları için
  firması kesindir, `attach_alias` ile konum üzerinden bağlanır. Gerekçe: gerçek veride
  bunlar **yazım hatası** içeriyor (`TAAHÜT`/`TAAHHÜT`, `TIBBI`/`TIBBİ`, `İTVE` bitişik) →
  bağımsız çözülselerdi mükerrer firma üretirlerdi.
  - **`"alias atlandı"` logu HATA DEĞİLDİR** (INFO seviyesi; çalışma özetinde
    `alias_cakismasi` olarak sayılır). Anlamı: varyant yazım **zaten başka bir firmanın
    kanonik anahtarı**; bağlansaydı o yazımı bir firmadan alıp diğerine verirdik
    (yanlış-birleştirme). Sözleşme yine `yukleniciAdi`'ndan çözülen firmaya bağlı kalır,
    yalnızca varyant kaydı atlanır. Tipik sebep yazımların **gerçekten farklı olması**
    (`ENSAR DOĞAN` vs `ENSAR OSMAN DOĞAN`) — noktalı/açık kısaltma farkı değil, o zaten
    aynı anahtara düşer (`TİC.LTD.ŞTİ.` ≡ `TİCARET LİMİTED ŞİRKETİ`).
    Sayı beklenmedik biçimde yüksekse kanonikleştirici `--dry-run` ile incelenmeli.
- **Ortak girişim**: JV'nin kendisi bir `Contractor` (`kind=ortak_girisim`), üyeler ayrı
  firma, arada `ContractorMembership`. Ayrıştırma **marker kapısı** ister
  (`iş ortaklığı`/`ortak girişim`/`konsorsiyum`, string'in son 40 karakterinde);
  **marker yoksa asla bölünmez** (sıradan ünvandaki `-`/`,` bölmeyi tetiklemesin). Ayırıcı
  şelalesi `" - "`→`" & "`→`" ile "`→…→`","` (virgül ek korumalı). Üye tavanı 6.
  Güven `dusuk` ise **üyelik YAZILMAZ**, `uyeleri_cozumlendi=False` olur (admin'de filtre).
- **Agregalar denormalize** (`sozlesme_sayisi`, `toplam_sozlesme_bedeli`,
  `son_sozlesme_tarihi` = firma listesinin `ORDER BY` anahtarları). ⚠️ `sozlesme_sayisi`
  ≠ `ihale_sayisi`: kısımlı ihalede bir firma 3 kısım alırsa 3 sözleşme / 1 ihale olur.
  `ortalama_indirim_orani` **her zaman `indirim_orani_ornek_sayisi` ile birlikte** döner
  (yaklaşık maliyet kapsamı kısmi). İl/yıl/idare kırılımları denormalize edilmez — tek
  firmayla sınırlı olduğu için detay ucunda canlı hesaplanır.
- **Uçlar** (hepsi `AllowAny`, mevcut `ekap/` uçlarıyla tutarlı):
  - `GET /ekap/contractors/?q=&kind=&il_id=&min_sozlesme=&order=` — firma arama
    (`q` normalize sütunda, **Türkçe-i güvenli**).
  - `GET /ekap/contractors/<id>/` — kimlik + `istatistik` + `dagilim` + `aliaslar` +
    `ortak_girisimler` + `uyeler`.
  - `GET /ekap/contractors/<id>/contracts/` — sözleşme geçmişi (filtre + sayfalı).
  - `GET /ekap/tenders/<ekap_id>/contracts/` — ihalenin sözleşmeleri + gömülü yüklenici.
    (İKN `/` içerdiği için **ekap_id** kullanılır.)
  - İhale listesinde yeni filtre: `yuklenici_id` / `yuklenici` (+`ortakliklari_dahil_et`,
    vars. açık → firmanın ortak girişim üyesi olarak aldığı işler de gelir).
  - Alan adları **snake_case Türkçe** (EKAP camelCase değil): EKAP'ta yüklenici nesnesi
    yok, yansıtılacak şekil ve korunacak mobil mapper da yok.
- **Güncel kalma (sürekli)**: EKAP sözleşmeleri zamanla değiştirir (bedel/tarih girilir,
  sonuç ilanı sonradan yayımlanır, sözleşme eklenir/kaldırılır). Zincir şudur:
  `refresh_stale` (3 saatte bir) detayı yeniler → `upsert_tender_detail` → `_sync_children`
  → **`sync_contracts_from_raw` senkron çağrılır** → sözleşmeler kararlı anahtarla upsert
  edilir, yüklenici yeniden çözülür, Sonuç İlanı yeniden ayrıştırılır. Yani beat görevini
  beklemeye gerek yok; detay her yenilendiğinde yüklenici verisi de yenilenir.
  - ⚠️ **`recompute=True` (varsayılan) şart**: `Contract` satırı düzelse bile firma
    sayaçları (`sozlesme_sayisi`, `toplam_sozlesme_bedeli`, `son_sozlesme_tarihi` =
    firma listesinin sıralama anahtarları) ayrı tutulduğu için elle yenilenmeli.
    Artımlı görev de yakalayamaz — `sync_contracts_from_raw` `contractors_synced_at`'i
    güncellediği için ihale "bayat" görünmez. Toplu çağıranlar `recompute=False` verip
    birleşik kümeyi sonda tek seferde hesaplar.
  - ⚠️ **Budanan satırların firmaları da "dokunulan" sayılır**: EKAP bir sözleşmeyi
    kaldırırsa/başka firmaya geçirirse kaybeden firmanın sayaçları da düşmeli. Yalnızca
    yeni listedeki firmaları toplamak onu bayat bırakırdı. Sıfır-sözleşme dalı da aynı
    şekilde önce firmaları toplar. **Firma kaydı asla silinmez** (geçmiş korunur).
- **Toplama**: `sync_contractors` beat görevi (**10 dk**) — **EKAP'a gitmez**,
  `Tender.detail_raw` arşivinden çalışır (`.only()` ile: `list_raw` okunmaz). Süpürme modu
  PK imleciyle tüm arşivi tarar, bitince artımlı moda geçer
  (`contractors_synced_at < detail_synced_at` → `refresh_stale` bir detayı yenileyince
  kendiliğinden yakalar). Sınır **süre bütçesidir**
  (`max_seconds=90`, global `CELERY_TASK_TIME_LIMIT=300` altında), sabit sayı değil:
  iş tamamen DB-içi olduğu için hız makineye göre çok değişir.
  ⚠️ **Gerçek üretim verimi: ~2,8 ihale/sn** (2026-08, 681k detaylı ihale, 3 günlük
  `SyncRun` ortalaması). Burada eskiden yazan "~200 ihale/sn" **yanlıştı** — muhtemelen
  küçük/sıcak bir veri kümesinde ölçülmüştü ve planlamayı 70× iyimser gösteriyordu.
  Darboğaz I/O değil **CPU**: satır başına Sonuç İlanı HTML ayrıştırma + firma çözümleme
  + sözleşme upsert'i var. Tur başına ortalama 180 sn (bütçe 270 sn) → **bütçe değil,
  gece penceresi sınırlayıcı**. ETA hesabı için: `SyncRun`'dan `sum(items)/sum(süre)`.
  ⚠️ **Duty cycle bilinçli düşük tutulur (~%15).** Eskiden 5 dk × 240 sn (~%80) idi;
  `detail_raw` (~40 KB/satır) arşiv boyunca okunduğu için Postgres buffer cache'i sürekli
  boşalıyor ve ihale arama sorguları diskten okumak zorunda kalıyordu. Süpürme artık
  birkaç kat uzun sürer ama arama ucu nefes alır; artımlı moda geçince yük zaten düşer.
  Görev **`celery` kuyruğuna yönlendirilir** (settings'te `ekap.tasks.*` joker'inden
  ÖNCE gelen istisna) — EKAP'a hiç gitmediği için tek-concurrency'li `ekap` kuyruğunda
  yer tutup `sync_recent`/`backfill`'i bloklamamalı. İmleç `try`'dan **önce** ilerletilir
  (bozuk tek ihale imleci kilitlemesin). Elle: `python manage.py rebuild_contractors
  [--dry-run] [--ikn X] [--limit N] [--from-pk N] [--restart] [--aggregates-only]
  [--reset] [--purge]`. Komut **aynı `contractors` checkpoint'ini paylaşır** → arka
  arkaya çalıştırınca kaldığı yerden devam eder ve her turda "kalan ihale" basar
  (imleçsiz sürüm hep aynı ilk N ihaleyi işliyordu).
  `--dry-run` kanonikleştiricinin **ayar döngüsüdür** — kural değiştirmeden önce çalıştır.

#### ⚠️ EKAP tutar verisi — bozulma haritası (KRİTİK)

Canlı `detail_raw` üzerinde doğrulandı. **Bu tabloyu okumadan tutar alanına dokunmayın.**

| Alan | Durum |
|---|---|
| `sozlesmeBilgiList[].{sozlesmeBedeli,enDusukTeklif,enYuksekTeklif}Degeri` (float) | ✅ Güvenilir — **bunları kullan** |
| `sozlesmeBilgiList[].yaklasikMaliyet` (string) | ❌ **BOZUK, geri kazanılamaz** |
| `sozlesmeBilgiList[].yaklasikMaliyetDegeri` | ❌ Daima `0.0` |
| `kisimItemDto.kisimList[]` tutarları (string) | ❌ **BOZUK** (`*Degeri` karşılığı yok) |
| Sonuç İlanı (`ilanTip=4`) HTML tutarları | ✅ **Tek doğru yaklaşık maliyet kaynağı** |

EKAP `yaklasikMaliyet` string'ini float'ın **ondalık noktasını silip** yeniden gruplayarak
üretiyor: `repr(11454672.76)` → `"1145467276"` → `1.145.467.276,00 TRY` (100× şişme),
`repr(4991250.0)` → `"49912500"` → `49.912.500,00 TRY` (10× şişme). Ölçek kayması float'ın
ondalık hane sayısına bağlı olduğundan **hiçbir bölenle geri alınamaz** → `yaklasik_maliyet_num`
**yalnızca Sonuç İlanı'ndan** doldurulur, yoksa `NULL` kalır ve `yaklasik_maliyet_kaynak`
alanı istemciye "veri yok"u ayırt ettirir. Kısım tutarları için sayısal sütun **eklenmez**.

Ayrıca `utils.parse_money` artık **format tespitlidir**: EKAP aynı nesnede TR (`529.820,00`)
ve EN (`18,128.00`) formatını karıştırıyor. Eski koşulsuz TR kuralı `"18,128.00 TRY"`ı
`18.13`e, `"2,017,840.00 TRY"`ı `NULL`a çeviriyordu. Yeni kurallar: iki ayırıcı varsa **son
görünen** ondalıktır; tek ayırıcıda son grup 3 hane ise binliktir. `*Degeri` float alanları
için `parse_money_value` (0.0 → None), seçim için `pick_money(...)` kullanılır.

##### ⚠️ Kısımlı ihalede "kısım maliyeti" ihale toplamı olabilir (indirim oranını şişirir)

Sonuç İlanı **güvenilir kaynak** ama her zaman *kısım* maliyetini ayrı yayımlamıyor;
bazen yalnızca ihalenin toplam maliyeti var. Ayrıştırıcı onu `kisim_yaklasik_maliyet`
olarak döndürünce tek kısım alan firmanın indirim oranı yapay olarak 1'e yaklaşıyor:
10 kısımlı ihalede toplam YM 10M, bir kısım 1M → `(10−1)/10 = 0,9`.

**Üretimde ölçüldü** (220.248 sözleşme, 2026-08) — 2×2 kırılım kesin:

| çok sözleşmeli | kısım YM == ihale YM | n | ortalama indirim | >%70 |
|---|---|---|---|---|
| hayır | hayır | 7.197 | 0,12 | %1,1 |
| hayır | **evet** | 8.431 | 0,14 | **%2,7** ← **meşru** (tek kısım = ihalenin kendisi) |
| evet | hayır | 185.117 | 0,19 | %3,2 |
| **evet** | **evet** | **19.509** | **0,88** | **%86,6** ← **bozuk** |

→ `sync._kisim_maliyeti_belirsiz(kisim_ym, ihale_ym, cok_sozlesmeli)`: **çok sözleşmeli
VE eşit** ise kısım maliyeti "bilinmiyor" sayılır → `yaklasik_maliyet_num` + `indirim_orani`
`NULL`, `yaklasik_maliyet_kaynak` boş. İhalenin toplamı (`tender_yaklasik_maliyet_num`)
DOĞRUdur, korunur. ⚠️ **Tek sözleşmeli ihalede eşitlik meşrudur, dokunulmaz.**

Geçmiş satırlar: `python manage.py fix_indirim_orani [--dry-run]` (saf DB işi, `detail_raw`
okumaz). Süpürme zamanla aynı işi yapar ama imleci geçtiği satırlara dönmez → tek seferlik
onarım gerekli.

⚠️ **Ders**: `indirim_orani` fiyat analizinin manşet sayısı ve kullanıcı buna bakıp teklif
fiyatı belirliyor. Yanlış bir sayı göstermektense **"veri yok" demek doğrudur** — aynı ilke
`ornek_sayisi` olmadan ortalama göstermeme kuralının kaynağı.

#### İhale durum kodları (üretim dağılımı — küçük örneğe bakıp budamayın)

`IHALE_DURUM` **iki kodlama şemasının birleşimidir**; ikisi de canlı veride bir arada
bulunur. Üretim dağılımı (505.588 detaylı ihale, 2026-07-31):

| durum | adet | açıklama | sonuçlanmış sayılır |
|---|---|---|---|
| **15** | **414.032** | **Sonuç İlanı Yayımlanmış** — arşivin %82'si, ASIL sonuçlanma yolu | ✅ |
| 6 | 68.279 | İptal Edilmiş | ✅ |
| 2 | 7.971 | Katılıma Açık | — |
| 5 | 6.661 | Sözleşme İmzalanmış | ✅ |
| 4 | 5.984 | Değerlendirme Tamamlanmış | — |
| 3 | 2.661 | Teklif Değerlendirme | — |

`DURUM_SONUCLANMIS = {5, 6, 10, 15, 20}` · `DURUM_SOZLESME_IMZALANDI = {5, 20}`.
Eski küme `{10,15,20}` **5 ve 6'yı kaçırıyordu** (75k ihale) — onlar eklendi.

⚠️ **Uyarı — bir kez yapılan hata:** 28 satırlık geliştirme veritabanında yalnızca 2/4/5
göründüğü için 10/15/20 "hiç gözlemlenmedi" denip haritadan silinmiş ve `DURUM_SONUCLANMIS`
`{5,6}`'ya indirilmişti. Bu, sonuçlanan ihalelerin **%98'ini** (durum 15) kümenin dışında
bırakıp "İhale Sonuçlandı" bildirimini asıl yolda tamamen susturuyor ve 414k ihaleyi yanlış
tazeleme dalına düşürüyordu. **Durum kodlarını yerel örneğe bakarak budamayın** — üretim
dağılımını sorgulayın.

⚠️ `sonuc_ilani_eksik` carve-out'u: Sonuç İlanları imzadan *sonra* yayımlandığı için,
sözleşmesi olup ilanı gelmemiş ihale 7 gün yerine **2 günde** tazelenir.
(Küme değişikliğinde push fırtınası riski yok: `_detect_alarm_events` bir **geçiş** arar,
snapshot'ı zaten sonuçlanmış olan alarm olay üretmez.)

#### Çocuk tablolar artık upsert (PK churn giderildi)

`_sync_children` eskiden her detay senkronunda `tender.sozlesmeler.all().delete()` + yeniden
`create` yapıyordu; `refresh_stale` 3 saatte bir çalıştığı için `Contract` PK'ları sürekli
değişiyordu — firma bağlantısı kurulacaksa kabul edilemez. Artık kararlı EKAP anahtarlarıyla
upsert + budama yapılır: `Announcement.ekap_ilan_id`, `Contract.ekap_sozlesme_id`
(`sozlesmeBilgiList[].id`), `ContractSection.ekap_kisim_id` (`kisimList[].id`).
Anahtarsız satıra sentetik `noid:{i}` verilir (koşulsuz unique constraint'e hazırlık).
- **Toplu yazma şart**: `_bulk_upsert_children` satır sayısından bağımsız ~4 sorgu yapar.
  Satır başına `update_or_create` döngüsü 69 kısımlı ihalede **315 sorguya** çıkıyordu;
  şimdi **16**. (13 sözleşmeli ihale: 368 → 39.)
- **Mevcut satırlar anahtar başına tekilleştirilir**: eski sil-yeniden-yaz döneminden
  kalan satırların HEPSİ `ekap_*_id=""` taşır, yani bir ihalede N tane aynı-anahtarlı
  satır olabilir. Bunları doğrudan sözlüğe çevirmek fazlalıkları gizler ve budama
  listesi onları hiç görmez → her ihalede N-1 yetim satır kalırdı (prod'da 60k+ ihale).
  `_bulk_upsert_children` bu yüzden queryset'i **satır satır** gezip fazlalıkları
  `stale`'e ekler.
- **Dedupe zorunlu**: `ilanList` ile ayrı announcements yanıtı birleştiğinde aynı `id`
  tekrar edebiliyor; Postgres `ON CONFLICT`in aynı satıra iki kez dokunmasını reddeder.
- `Contract` üzerinde `idare_id`/`il_id`/`ihale_tip` **ingest-kopyasıdır** → firma
  sorguları `ekap_tender` JOIN'i yapmaz (milyonlarca satırda kritik).

#### Hesaplanamayanlar (API'de alan AÇILMAYACAK)

- **Kazanma oranı**: `sozlesmeBilgiList` yalnızca **imzalanmış** sözleşmeleri içerir;
  kaybedilen teklif veri kaynağında yok. `tebligatAlanIstekliList` isimsiz opak id listesi.
- **İstekli bazlı teklif tutarları**: yalnızca teklif kümesinin min/max'ı var →
  "hangi ihaleye ne teklif verdi" sorusu **yalnızca kazandığı işler için** yanıtlanabilir.
- **VKN/TCKN**: yok → aynı ünvanlı iki gerçek firma birleşir, ünvan değiştiren ikiye ayrılır.
  Şeffaflık için `aliaslar` her zaman API'de açık.

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
  - **Eşleştirme kapsamı** (`match_tenders_for_profile`): temel filtre **durum 2/3 (katılıma
    açık) + teklifi geçmemiş** (`ihale_tarihi >= now` ya da boş). İki opsiyonel pencere:
    (a) `published_on=<tarih>` **KATI** → yalnızca `ilan_tarihi` tarihi tam o güne eşit (NULL
    hariç); **günlük öneri bildirimi (beat) bunu BUGÜN'le çağırır** → yalnızca bugün yayınlanan
    ihaleler önerilir. (b) `since` **gevşek** → son X gün, `ilan_tarihi` boş olanlar dahil
    (`--days N`, N>1 backfill). Sohbet/canlı eşleştirme ikisini de vermez (geniş kapsam).
    Kullanıcının zaten **kaydettiği** ihaleler (`SavedTender`) önerilerden **exclude** edilir
    (İKN ile açıkça sorulursa yine gelir).
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

## Abonelik / Premium (Pro) Katmanı

Uygulama **abonelik usulüdür**: bazı özellikler yalnızca Pro üyelere açıktır. Free
(ücretsiz) üyeler sınırlanır ama üzülmez — kısıtlı uçlar net bir Türkçe mesaj ve
makine-okunur `errors.code = "premium_required"` ile **HTTP 403** döner; mobil bu
kodu görünce abonelik paketlerini sunar.

- **Model** (`accounts.User`): `subscription_tier` (`free`/`pro`, `Tier` choices) +
  `subscription_expires_at` (boş=süresiz; dolu=o tarihten sonra otomatik Free). Katman
  **API'de read-only**; admin panelinden (kullanıcı → "Abonelik" bölümü) veya ileride
  ödeme entegrasyonundan ayarlanır. `User.is_premium` property tek doğruluk kaynağıdır:
  `superuser` her zaman premium; aksi halde `tier==pro` ve (varsa) `expires_at` gelecekte.
- **Kapılama altyapısı** (`accounts/premium.py`): `PremiumRequired` (APIException, 403,
  `errors={code,detail}`), `require_premium(user, mesaj)` (bir işlemi Pro'ya kilitler),
  `enforce_free_limit(user, current_count, limit, mesaj)` (sayı-tabanlı Free limiti —
  **jenerik**, şu an hiçbir uçta kullanılmıyor; sayısal limitler kaldırıldı ama helper
  ileride kullanılabilsin diye durur) ve uca özel Türkçe mesajlar. Model importu yapmaz →
  sirküler import yok. Yeni bir premium kısıt eklerken **bu modülü kullan**, elle
  `{success:false}` kurma.
- **Sayısal Free limiti YOK**: kayıtlı filtre, kayıtlı ihale ve favori idare **kaydetme**
  Free dahil **sınırsızdır** (eski 3/5/10 limitleri kaldırıldı). Kısıt artık **alarm/bildirim
  özelliklerinde**dir (aşağıda) — kaydetmenin kendisi değil.
- **Free kısıtları** (Pro'da hepsi açık):
  - **İhale alarmı kurma** (`TenderAlarmListCreateView.perform_create`) → 403; `require_premium`
    ile tamamen Pro'ya özel (kurma/güncelleme kilitli). Alarm **listeleme/silme** (GET/DELETE)
    serbest. Bildirim tarafında da tutarlı: `check_tender_alarms` **premium olmayan**
    kullanıcının alarmını atlar (`is_premium` property → Python'da elenir).
  - **Kayıtlı filtre ALARMI** → filtre kaydetmek serbest ama `alarm` **açık** kaydedilir/
    güncellenirse Pro gerekir (`SavedFilter{ListCreate,Detail}View`; `_alarm_enabled(alarm)` +
    `require_premium`, `MSG_FILTER_ALARM`). Alarmı **kapatmak/alarmsız kaydetmek** her üyeye
    serbest. `check_saved_filter_matches` premium olmayanı atlar (downgrade koruması).
  - **Favori idare ALARMI** → idareyi favorilemek serbest (sınırsız) ama alarm bildirimi
    (o idare yeni ihale yayınlayınca) **Pro'ya özeldir**: `check_favorite_authority_matches`
    premium olmayan kullanıcıyı atlar. Favorileme uçta 403 vermez (Free de favorileyebilir);
    yalnızca push/bildirim Pro iken üretilir. (Böylece "favori idare = bedava filtre alarmı"
    açığı kapanır; filtre alarmıyla tutarlı.)
  - **Takip edilen firma ALARMI** → firmayı takip etmek serbest (sınırsız) ama "yeni iş
    aldı" bildirimi **Pro'ya özeldir**: `check_favorite_contractor_matches` premium
    olmayanı atlar. Uçta 403 verilmez (favori idareyle tutarlı).
  - **Tekrar eden ihale takibi** (`/ekap/recurring/`, `/ekap/tenders/<key>/recurring/`) → 403.
  - **Asistan sohbeti** (`ChatSendView`) → 403 (profil oluşturma **serbest**; yalnızca
    mesaj gönderme kilitli).
  - **AI doküman analizi** (`AnalyzeView`) → 403, en başta (cache dahil hiç işlenmez).
    Doküman **indirme** (`ekap document-url`) serbesttir; TTS de kısıtlanmaz.
  - **Gelişmiş arama filtreleri** (`TenderListView`, `_PRO_PARAMS`) → 403. Temel arama
    (`q`, il, tür, tarih, OKAS, idare…) **herkese açık kalır**; yalnızca tutar/rekabet/
    indirim/şikâyet/kategori filtreleri kilitli (bkz. "Pro arama filtreleri").
  - **İhale Asistanı bildirimleri** → `match_recommendations` Free üyeyi **atlar**
    (öneri/digest/push üretilmez; `.select_related("user")` + `is_premium` kontrolü).
  - **Destek talebi oluşturma** (`SupportTicketView.perform_create`) → 403; talep
    **listeleme** (GET) her üyeye açık.
- **Mobil görünürlük**: `UserSerializer` `is_premium` + `subscription_tier` +
  `subscription_expires_at` alanlarını (read-only) döner → login ve `GET /auth/profile`
  yanıtında gelir; mobil Pro özellikleri buna göre gösterir/gizler.
- **Pro'ya geçiş = RevenueCat** (bkz. aşağıdaki bölüm). Admin de elle Pro atayabilir.

### RevenueCat entegrasyonu (`subscription/`)

Pro aboneliği **RevenueCat (RC)** ile satılır; backend RC'yi doğrulayıp `User`
katmanını senkronlar. Sözleşme: **`app_user_id = str(user.id)`**.

- **Ayarlar** (`.env` → settings): `REVENUECAT_SECRET_KEY` (`sk_...`),
  `REVENUECAT_PROJECT_ID` (`proj3e35bc67`), `REVENUECAT_ENTITLEMENT` (vars. `pro`),
  `REVENUECAT_WEBHOOK_AUTH` (webhook paylaşılan sırrı — RC panelindeki Authorization
  başlığıyla birebir aynı). `.env.prod`'da (gitignore) gerçek değerler; `.env.example`'da
  placeholder. Anahtar boşsa uçlar 502 döner.
- **Servis** (`subscription/services/revenuecat.py`): `resolve_pro_status(cust_id)` →
  RC v2 `GET .../customers/{id}/active_entitlements` sorgular; `lookup_key == pro` varsa
  Pro. Expiry önce entitlement'ın `expires_at`'inden, yoksa `.../subscriptions`'ın en ileri
  `current_period_ends_at`'inden alınır (RC ms-epoch → aware datetime). `sync_user_subscription`
  katmanı yazar (yalnızca değiştiyse — idempotent). `requests` kullanılır (RC'de TLS
  parmak izi engeli YOK; EKAP kuralı burada geçerli değil). `resolve_user_from_event`
  event'teki `app_user_id`/`aliases`'tan **sayısal** id'yi (=`user.id`) çözer (RC anonim
  `$RCAnonymousID:...` atlanır). `apply_event_fallback` yalnızca RC API'ye ulaşılamazsa
  event verisinden kaba senkron yapar (EXPIRATION/geçmiş expiry→Free; grant/CANCELLATION +
  gelecek expiry→Pro).
- **Uçlar** (`/api/v1/subscription/...`):
  - `POST verify/` (mobil→backend, **JWT**, gövde boş `{}`): RC'yi **senkron** sorgular,
    katmanı günceller, güncel kullanıcıyı `data.user` (login/profile şeklinde) döner. Mobil
    satın alma sonrası çağırır → anında Pro görür. RC hatası → 502.
  - `POST revenuecat-webhook/` (RC→backend, **JWT yok**): `Authorization` başlığı
    `REVENUECAT_WEBHOOK_AUTH` ile eşleşmeli (yoksa 401). Event'ten kullanıcı çözülür,
    senkron **Celery'ye atılır** (`sync_subscription_task`, event yedeğiyle) ve **hızlıca
    200** döner. Kullanıcı eşleşmezse (anonim id) yine 200 `handled:false`. Test Store
    da webhook fırlatır → sandbox'ta test edilebilir.
- **Tek kod yolu**: webhook event tipiyle elle uğraşmaz; her zaman `active_entitlements`'i
  yeniden sorgular → durum her zaman RC ile tutarlı. `verify` ve webhook aynı
  `sync_user_subscription`'ı kullanır.

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
  gece EKAP `sync_recent` bittikten sonra; **Pro'ya özel**, profil tabanlı)
- `recommend_by_saved_okas` — kayıtlı ihalelerin OKAS kodlarıyla son 24s yayınlanan
  ihale önerisi + push (günlük 08:00, **Free/Pro herkese**)
- `check_tender_alarms` — ihale alarm hatırlatıcıları + push (günlük 09:00)
- `check_saved_filter_matches` — kayıtlı filtre yeni-ihale bildirimi + push (günlük 10:00)
- `check_favorite_authority_matches` — favori idare yeni-ihale bildirimi + push (günlük 11:00)
- `check_favorite_contractor_matches` — takip edilen firma yeni iş aldı bildirimi (günlük 12:00, **Pro'ya özel**)
- `weekly_free_teaser` — ücretsiz üyeye haftalık "kaçırdıklarınız" özeti (Pazartesi 10:00, **yalnızca Free**)
- `detect_recurring_series` — tekrar eden ihale serilerini tespit eder (Pazar 02:30; EKAP'a
  gitmez, `detail_raw` OKUMAZ → süpürme penceresiyle çakışmaz)
- `backfill_tender_fields` — Pro sinyal kolonlarını `detail_raw` arşivinden doldurur
  (5 dk'da bir; EKAP'a gitmez, gece penceresi, **yüklenici süpürmesi bitene kadar
  kendini geri çeker** — bkz. "Pro sinyal kolonları")
- `sync_contractors` — sözleşmeleri yüklenici firmalara bağlar (**10 dk'da bir, 90 sn
  bütçe**; EKAP'a gitmez, `detail_raw` arşivinden çalışır — bkz. "Yüklenici (Firma)
  Kaydı"). ⚠️ Duty cycle bilinçli olarak düşük: eskiden 5 dk × 240 sn (~%80) idi ve
  Postgres buffer cache'ini boşaltıp ihale aramasını yavaşlatıyordu.

## Bildirim Servisi (Push)

Eski `~/Desktop/ihaletakip-scheduler` (Python + `firebase-admin`, Firebase projesi
`ihale-53fbf`) servisinin işlevi API'ye taşındı; artık kendi Postgres/EKAP verimizden
push üretiyoruz. Beş kaynak vardır. Bildirimler **abonelik-başına AYRI**dır: her kayıtlı
filtre / favori idare / ihale alarmı için **ayrı** uygulama-içi satır + **ayrı** push atılır
(kullanıcı başına birleşik özet DEĞİL) — 10 filtreden 8'i eşleşirse **8 ayrı bildirim** gider.
OKAS önerisi istisnadır (kullanıcı başına tek özet). Çoğalma **abonelik-başına atomik
gün-kilidiyle** (Redis `cache.add`, `tenders.tasks._ROW_DEDUP_TTL`) önlenir: görev yinelenmiş/
interval beat ile çok kez tetiklense bile öğe günde bir kez işlenir.

- **Katmanlar** (`tenders/services/`):
  - `push.py` — FCM gönderici (`firebase_admin` **lazy** import; `FCM_CREDENTIALS` boşsa
    no-op). `send_fcm()` durum döner: `sent`/`invalid_token`/`error`/`disabled`. Ölü token
    (`UnregisteredError`, `SenderIdMismatchError`, token'a dair `InvalidArgumentError`) →
    `invalid_token`.
  - `notify.py` — **kayıt ve push ayrıdır**: `record_notification()` yalnızca uygulama-içi
    `Notification` satırı yazar; `push_to_user()` pacing kapılarından geçen **tek** push atar.
    `notify_and_push()` tek-olay kısayolu (öneriler).
  - `templates.py` — Türkçe metinler (İhale Günü / Doküman Güncellendi / İhale Sonuçlandı,
    `alarm_tender` (ihale-başına birleşik), filtre eşleşmesi, favori idare eşleşmesi, OKAS önerisi).
- **Zamanlama (kademeli, ≥1 sa arayla)**: 07:00 asistan öneri digest'i (`match_recommendations`),
  08:00 OKAS önerisi (`recommend_by_saved_okas`), 09:00 alarm hatırlatıcıları
  (`check_tender_alarms`), 10:00 filtre eşleşmeleri (`check_saved_filter_matches`), 11:00
  favori idare eşleşmeleri (`check_favorite_authority_matches`). Alarm/filtre/idare kategorileri
  **abonelik-başına ayrı push** atar (o kategorinin görev turunda arka arkaya).
- **Çoğalma önleme = abonelik-başına ATOMİK gün-kilidi** (`cache.add`, race-safe): her filtre/
  idare/alarm için `{"filter"|"authority"|"alarm"|"okasrec"}:{uid}:{item_id}:{date}` anahtarı
  öğe işlenmeden **atomik** rezerve edilir. Görev yinelenmiş/interval beat ile aynı gün çok kez
  tetiklense bile öğe **bir kez** işlenir → uygulama-içi satır da push da çoğalmaz. Filtre/idare
  ayrıca `last_notified_at` **watermark**'ıyla cross-day dedup yapar (dün bildirilen ihale bugün
  tekrar bildirilmez; `_seen_ikns` KALDIRILDI). Alarm'da mevcut snapshot/`completed_notified`
  guard'ları + gün-kilidi yeterli.
- **Pacing kapıları** (`django.core.cache`=Redis, ayar-tabanlı): sessiz saat
  (`NOTIF_QUIET_START/END_HOUR`, vars. 22–07), günlük limit (`NOTIF_DAILY_CAP`=**50**, `≤0`=
  sınırsız), min aralık (`NOTIF_MIN_GAP_MINUTES`=**0**=kapalı), kullanıcı tercihi
  (`User.preferences["notifications"]["push"]`, vars. açık) + `is_active` + dolu `fcm_token`.
  Kapı engellese de **uygulama-içi satır yazılır**, yalnızca push atlanır. **Cap/min-gap neden
  gevşek?** Abonelik-başına ayrı push tasarımında (bir görev turunda çok push) düşük cap/min-gap
  meşru bildirimleri **düşürürdü** (ör. 8 filtreden 7'si). Bombardıman kontrolü artık:
  abonelik-başına gün-kilidi + kategori-başına staggered saat + sessiz saat + gün-kilidi.
  - **Not (çift bildirim)**: mobil uygulamada özel FCM handler yok (bildirim OS sistem
    tepsisinden `notification` bloğuyla gösterilir) → çift bildirim gelirse kaynak backend'de
    **çift gönderim**tir. Kod artık gün-kilidiyle idempotent; kalırsa prod'da **yinelenmiş
    `PeriodicTask`** (django_celery_beat) ya da hâlâ çalışan eski `ihaletakip-scheduler`.
    Denetim: admin → Periodic Tasks (aynı `task` için yinelenmiş/interval girdi = sil).
- **Kaynaklar**:
  1. **Öneri** — `match_recommendations` digest'i (mevcut) artık push de atar
     (`type=CHAT`, `conversation_id` → mobilde digest sohbeti açılır). idem `digest:{uid}:{date}`.
  2. **Alarm** (`TenderAlarm.reminder_day/document_change/completed`) — `ekap.Tender` ile
     karşılaştırır: ihale günü (`ihale_tarihi`=bugün), doküman değişikliği
     (`dokuman_sayisi != last_dokuman_sayisi`; ilk görüşte sessiz), sonuçlandı (durum
     `DURUM_SONUCLANMIS`'e geçiş; `completed_notified` ile tek sefer). **Her ihale için AYRI**
     push (o ihalenin olayları `templates.alarm_tender` ile tek bildirimde birleşir; başlık =
     ihale adı). `type=ALARM` + `tenderId`/`tenderIkn` → tıklanınca ihale detayı. Gün-kilidi
     `alarm:{uid}:{ekap_id}:{date}`. Snapshot alanları `TenderAlarm`'da.
  3. **Kayıtlı filtre** (`SavedFilter.alarm` truthy) — filtreye uyan ve **yalnızca `ilan_tarihi`
     BUGÜN olan** açık ihaleler (dün/eski/backfill DEĞİL; "bugün" filtresi cross-day dedup'ı da
     sağlar → watermark KALDIRILDI). `ilan_tarihi` detay senkronundan dolduğundan bugün yayınlanan
     bir ihale ancak detayı gelip `ilan_tarihi=bugün` olunca eşleşir. Filtre semantiği
     `ekap.views.apply_tender_filters` ile view'la **ortak**.
     **Her filtre için AYRI** bildirim/push (10 filtreden 8'i eşleşirse 8 ayrı). **Mesaj**:
     başlık = filtre adı, gövde = "{filtre} filtrenize uygun N adet ihale bulundu."
     (`templates.saved_filter_match`). **Derin bağlantı = filtre**: `type=TENDER` +
     `filter_id=SavedFilter.id`; `tender_id`/`tender_ikn` **doldurulmaz**. Mobil `filter_id`
     ile filtreyi (`GET /saved-filters/{id}/`) yükleyip arama sonuçlarını açar (push data →
     `filterId`). Gün-kilidi `filter:{uid}:{sf.id}:{date}`. **Pro'ya özel** (premium olmayan atlanır).
  4. **Favori idare** (`FavoriteAuthority.alarm=True`) — favori idarenin **yalnızca `ilan_tarihi`
     BUGÜN olan** açık ihaleleri (dün/eski DEĞİL; "bugün" filtresi cross-day dedup'ı sağlar).
     `detsis_no` `descendant_idare_ids` ile alt birim
     `idare_id`'lerine genişletilir (ihale/tarama uçlarıyla ortak). **Her favori idare için
     AYRI** bildirim/push (başlık = idare adı). **Derin bağlantı = idare listesi**: `type=TENDER`
     + `authority_detsis=detsis_no`; mobil o idarenin listesini (`GET /ekap/tenders/?idare_detsis=`)
     açar (push data → `authorityDetsis`). Gün-kilidi `authority:{uid}:{detsis_no}:{date}`.
     **Pro'ya özel**: görev premium olmayanı atlar.
  5. **OKAS önerisi** (`recommend_by_saved_okas`, 08:00) — **Free/Pro fark etmez, HERKESE**.
     Kullanıcının kaydettiği ihalelerin (`SavedTender`) OKAS kodlarını toplar (benzersiz,
     azami 20 kod; `OkasItem.kodu`), o kodlarla **son `NOTIF_OKAS_PUBLISH_DAYS`=1 günde
     yayınlanan** (`ilan_tarihi`) açık + teklifi geçmemiş ihaleleri bulur (kullanıcının zaten
     kaydettikleri hariç). Eşleşme varsa tek özet bildirim + push. **Mesaj**: "Size Özel
     İhaleler" / "İlgilendiğiniz kategorilerde N yeni ihale yayınlandı." **Derin bağlantı =
     OKAS araması** (tek ihale DEĞİL): bildirim `type=TENDER` + `okas_kodlar` (CSV);
     `tender_id`/`tender_ikn` **doldurulmaz**. Mobil bildirime basınca `okas_kodlar` ile
     `GET /ekap/tenders/?okas_kod=<CSV>` arama sonuçlarını açar (push data → `okasKodlar`).
     idem `okas:{uid}:{date}`. **Premium DEĞİL** — asistan önerisinden (Pro, profil tabanlı)
     ayrıdır. Mobil deep-link önceliği: `conversation_id` (CHAT) > `filter_id` >
     `authority_detsis` > `okas_kodlar` > `tender_ikn`/`tender_id`.

- **`ekap.views.apply_tender_filters`** (arama ucu + bildirim ortak filtresi) — **tek
  adlandırma: parametre adları `Tender` model alan adlarıdır** (native/kısa alias YOK).
  Desteklenen alanlar: `ihale_adi`, `ikn`, `ikn_yil`, `ikn_sayi`, `il_id`, `ihale_tip`,
  `ihale_usul`, `ihale_durum`, `idare_id`, `yasa_kapsami` (null-inclusive → detayı gelmemiş
  ihaleyi dışlamaz), `okas_kod`, `okas_adi`, `ozellik` (OZELLIK_MAP anahtarları),
  `ihale_tarihi_min/max`, `ilan_tarihi_min/max`. Liste alanları hem virgüllü string (query
  param) hem gerçek liste (`SavedFilter.filters` JSON) kabul eder. **Mobil, arama VE kayıtlı
  filtre gövdesini bu model adlarıyla göndermeli** (Postman güncel). Gelişmiş alanlar
  (`il_id`, `ihale_usul`, `yasa_kapsami`, `ozellikler`, `okas_kalemleri`, **`ilan_tarihi`**)
  **detay senkronunda** dolar (`upsert_tender_detail`); `ilan_tarihi` EKAP liste yanıtında
  boş gelir, detay `ilanList`'inden (İhale İlanı `ilanTip=1`) `_publish_date_from_ilanlar`
  ile doldurulur.
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

(Firestore karşılığı olmayan) `tenders.TenderGroup` — kayıtlı ihale klasörleri, bkz. aşağıda.
(Firestore karşılığı olmayan) `ekap.Contractor` / `ContractorAlias` / `ContractorMembership`
— yüklenici firma kaydı, bkz. "Yüklenici (Firma) Kaydı".

## Kayıtlı İhale Klasörleri (`tenders.TenderGroup`)

Kullanıcı kayıtlı ihalelerini klasörlere ayırabilir (`/tender-groups/` uçları).

- **Varsayılan klasör ("Genel") bir satır DEĞİLDİR**: `SavedTender.group is None`
  demektir. Uçlarda dönmez, oluşturulamaz (`DEFAULT_TENDER_GROUP_NAME` rezerve),
  mobil listenin başına kendisi ekler. Böylece özellik öncesi kayıtlar taşıma
  gerektirmeden Genel'de görünür.
- `SavedTender.group` → `TenderGroup` FK, **SET_NULL**: klasör silinince kayıtlar
  silinmez, Genel'e döner.
- Uçlar: `GET/POST /tender-groups/`, `GET/PATCH/DELETE /tender-groups/<pk>/`,
  kayıt taşıma `PATCH /saved-tenders/<ikn>/` (`{"group": <pk|null>}`),
  `GET /saved-tenders/<ikn>/` artık `is_saved` + `group` + `group_name` döner.
- Sınırlar: kullanıcı başına `MAX_TENDER_GROUPS` (20) klasör, ad benzersiz.
  Benzersizlik karşılaştırması `_tr_fold` ile (İ/I → i/ı): `casefold()`/`iexact`
  Türkçe İ'de yanılır ("İşleri" ≠ "işleri"). Sınır yok (Free + Pro).

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
- **⚠️ Her deploy'dan sonra `docker compose restart nginx` ŞART** (yaşanmış arıza: tüm
  site **502**). `docker/nginx/default.conf` statik upstream kullanır
  (`upstream django_app { server web:8000; }`) ve nginx `web` adını **yalnızca
  başlangıçta** çözüp IP'yi kalıcı önbelleğe alır. `up -d --build` web konteynerini
  yeniden yaratınca yeni IP verilir; nginx'in kendi imajı/konfigi değişmediği için
  compose onu yeniden başlatmaz (`ps`'te `Up 4 hours` görünür) ve **ölü IP'ye**
  proxy'lemeye devam eder. Belirti: `docker compose ps` web'i `healthy` gösterir ama
  `curl /health/` **502** döner — yani "web sağlıklı ama site kapalı".
  `install.sh` bunu artık otomatik yapar; elle `up -d` çalıştırırsan sen ekle.
- **⚡ Güncelleme betiği `install.sh`** (repo kökü): tek komutla `git pull --ff-only` +
  `docker compose up -d --build` + servis durumu + `/health/` doğrulaması yapar → her
  seferinde elle uğraşmaya gerek yok. `./install.sh` (normal), `./install.sh --logs`
  (ardından web loglarını izle), `./install.sh --migrate` (yalnızca worker'da migrate —
  ağır data-migration için). `.env.prod`/`credentials/`/sertifikalara dokunmaz.
- **⚠️ Ağır data-migration (dolu tabloda backfill)**: Entrypoint `migrate`'i senkron
  çalıştırır; **dolu bir tabloda** uzun süren bir backfill (ör. `0005_search_norm` norm
  sütunları) `web` healthcheck penceresini (`start_period=180s`) aşarsa `web` "unhealthy"
  olur ve `up` başarısız görünür. Migration'lar **atomic**'tir (kesilirse temiz rollback),
  ama bu durumda migrate'i **elle** çalıştır (healthcheck baskısı yok):
  `docker compose exec worker python manage.py migrate` → bitince `docker compose up -d`.
  (Taze deploy'da tablolar boş → backfill anlık, bu sorun yaşanmaz.)
- **⚠️ `web`'in entrypoint'i HER başlangıçta migrate çalıştırır** (`RUN_MIGRATIONS`
  varsayılanı `true`) → elle migrate ile **yarışabilir**. Yaşanmış hata:
  `docker compose up -d --build worker` komutu `depends_on: [web]` yüzünden **web'i de
  yeni imajla yeniden yarattı**, web entrypoint'te migrate'e başladı, ardından elle
  başlatılan `exec worker … migrate` aynı indeksi kurmaya çalışıp
  `relation ... already exists` ile patladı; web'in migrate'i de 180 sn healthcheck
  penceresini aşınca `web` unhealthy oldu ve nginx düştü.
  **Doğru yol — bağımlılık başlatmayan, entrypoint'i atlayan tek seferlik konteyner:**
  ```bash
  docker compose stop web nginx     # web'in entrypoint migrate'i yarışmasın
  docker compose run --rm --no-deps --build --entrypoint python worker manage.py migrate
  docker compose up -d --build      # bitince hepsini geri getir
  ```
  - `--no-deps` → web'i ayağa kaldırmaz (`depends_on` zinciri tetiklenmez).
  - `--entrypoint python` → entrypoint'in migrate/collectstatic sarmalayıcısını atlar.
  - **`--build` ŞART.** `docker compose run` varsayılan olarak imajı yeniden
    kurmaz; `git pull` yapılmış olsa bile **bayat imajdaki eski migration dosyası**
    çalışır. Yaşanmış hata: idempotency düzeltmesi çekilmişti ama `run` eski imajı
    kullandığı için migration yine `already exists` ile patladı.
- **⚠️ `atomic=False` + `CREATE INDEX CONCURRENTLY` migration'ları** (`0007_search_indexes`):
  - Dolu tabloda düz `CREATE INDEX` yazmaları ACCESS EXCLUSIVE ile kilitler; ingest
    görevleri sürekli çalıştığı için `AddIndexConcurrently` kullanılır. CONCURRENTLY
    transaction içinde çalışamaz → `atomic = False` **zorunlu**, yani **rollback YOK**.
  - **CONCURRENTLY açık uzun transaction'ları BEKLER.** `sync_contractors`/`backfill`
    koşarken migration saatlerce asılı kalabilir. Migration boyunca admin → Periodic
    Tasks'tan `ekap-sync-contractors` ve `ekap-backfill`'i geçici kapatın.
  - 500k satırda GIN trigram indeksleri toplam 15-40 dk sürer, ~0.5-1 GB yer ister
    (`df -h` ile önce bakın). `tmux` kullanın ki SSH kopunca iş sürsün.
  - **Yarıda kesilirse geçersiz indeks kalır**:
    `SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;` →
    `DROP INDEX CONCURRENTLY <ad>;` → `migrate ekap` tekrar.
  - Bitince: `ANALYZE ekap_tender; ANALYZE ekap_okasitem;`
  - Yerel geliştirme SQLite'a düştüğü için (`DATABASE_URL` yoksa) migration'daki
    Postgres'e özel işlemler `_PostgresOnly` karışımıyla sarmalanır — **state ilerler,
    yalnızca DB işlemi atlanır**. `TrigramExtension` bunu `database_forwards`'ta kendi
    yapar ama `database_backwards`'ta YAPMAZ (geri alma SQLite'ta patlar) → o da sarmalı.
  - **Yarım kalmış migration kurtarma** (eski `atomic=False` sürümünden kalma
    "column already exists"): sütunlar var ama migration kayıtlı değilse
    `... migrate ekap 0005 --fake` ile kaydet, sonra norm'u elle doldur (Tender/Okas
    bulk_update + `run_ingest --task authorities`).
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
