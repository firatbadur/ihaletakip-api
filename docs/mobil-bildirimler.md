# Mobil Entegrasyon — Bildirimler & Alarmlar

Bu doküman IhaleTakip mobil uygulamasının **push bildirimleri, alarmlar, favori idareler
ve kayıtlı filtre alarmlarını** uçtan uca uygulaması için gereken tüm API'leri ve
davranışları anlatır.

> Kaynak: IhaleTakip API (Django REST). Tüm uçlar `https://ihale-takip.envisoft.com.tr/api/v1/`
> altındadır. Bu dosya elle yazılmaz kabul edilebilir; kaynak view'lardaki `@extend_schema`
> ve `docs/openapi.yaml` / `docs/postman_collection.json` her zaman günceldir.

---

## 0. Ortak kurallar

- **Taban URL**: `https://ihale-takip.envisoft.com.tr/api/v1/`
- **Kimlik**: Her istek `Authorization: Bearer <access_token>` başlığı ister
  (FCM token kaydı ve tüm alarm/favori/bildirim uçları JWT'lidir).
- **Yanıt zarfı** — TÜM yanıtlar şu biçimdedir:
  ```json
  { "success": true, "message": "", "data": { } }
  ```
  Liste uçları `data`'yı **doğrudan dizi** döner (sayfalama yok):
  ```json
  { "success": true, "message": "", "data": [ { ... }, { ... } ] }
  ```
- **Hata zarfı**:
  ```json
  { "success": false, "message": "Açıklama", "data": null, "errors": { } }
  ```
- **Premium (Pro) gerektiren işlem** Free üyede **HTTP 403** döner ve şu gövdeyi verir —
  mobil bunu görünce **abonelik paketleri ekranını** açmalıdır:
  ```json
  {
    "success": false,
    "message": "İhale alarmı kurma Pro aboneliğe özeldir. ...",
    "data": null,
    "errors": { "code": "premium_required", "detail": "İhale alarmı kurma Pro..." }
  }
  ```
  **Kontrol**: `response.status == 403 && errors.code == "premium_required"`.

Kullanıcının Pro olup olmadığı login ve `GET /auth/profile/` yanıtındaki `user` nesnesinde
gelir: `is_premium` (bool), `subscription_tier` (`free`/`pro`), `subscription_expires_at`.

---

## 1. FCM token kaydı (push için ön koşul)

Push alabilmek için cihazın FCM token'ı backend'e kaydedilmelidir. Uygulama açılışında ve
token yenilendiğinde çağırın.

**`POST /auth/fcm-token/`**
```json
{ "fcm_token": "fMEP0vJqR0m2Xy1s_cihaz_token_abc123" }
```
Yanıt: `{ "success": true, ... }`. `FCM_CREDENTIALS` sunucuda tanımlı değilse push devre
dışıdır ama uç yine 200 döner (uygulama-içi bildirimler çalışmaya devam eder).

Ölü/geçersiz token backend tarafından otomatik temizlenir; yeni token ürettikçe tekrar
kaydetmeniz yeterli.

---

## 2. Push mesajının yapısı (FCM)

Backend her push'u **`notification` + `data`** bloklu gönderir (Android channel:
`ihaletakip`, ses: default; iOS `content_available`). `data` alanındaki **tüm değerler
string**'tir.

`data` bloğu her zaman `type` içerir; ayrıca bildirim türüne göre **bir derin-bağlantı
anahtarı** taşır:

| `data.type` | Ek `data` anahtarı | Anlamı / Açılacak ekran |
|-------------|--------------------|--------------------------|
| `alarm`  | `tenderId`, `tenderIkn` (yalnız tek olayda) | İhale alarmı özeti → tek ihale varsa o ihalenin **detayı** |
| `tender` | `filterId`         | Kayıtlı filtre eşleşmesi → o filtrenin **arama sonuçları** |
| `tender` | `authorityDetsis`  | Favori idare eşleşmesi → o idarenin **ihale listesi** |
| `tender` | `okasKodlar` (CSV) | OKAS önerisi → o kategorilerin **arama sonuçları** |
| `chat`   | `conversationId`   | Asistan digest'i → o **sohbet** ekranı |

> Aynı bildirimde bu anahtarlardan **yalnızca biri** dolu olur. Yönlendirme sırası için
> aşağıdaki §4 tablosuna bakın (uygulama-içi bildirim objesiyle aynı mantık).

Örnek (favori idare eşleşmesi push data'sı):
```json
{ "type": "tender", "authorityDetsis": "24308110" }
```

---

## 3. Uygulama-içi bildirim listesi

Push, pacing (sessiz saat, günlük limit, min aralık) nedeniyle atlanabilir; ama
**uygulama-içi bildirim satırı her zaman yazılır**. Bildirim zilini/rozetini bu uçlardan
besleyin.

| Uç | Açıklama |
|----|----------|
| `GET /notifications/` | Kullanıcının bildirimleri (okunmuş + okunmamış), en yeni önce. `data` = dizi. |
| `GET /notifications/unread-count/` | `{ "unread": 3 }` — rozet için. |
| `POST /notifications/{id}/read/` | Tek bildirimi okundu yap. `{ "updated": 1 }`. |
| `POST /notifications/read-all/` | Tümünü okundu yap. `{ "updated": 5 }`. Gövde gerekmez. |

### Bildirim (Notification) objesi
```json
{
  "id": 42,
  "type": "tender",              // tender | alarm | chat | info
  "title": "Ankara Büyükşehir Belediyesi",
  "body": "3 yeni ihale yayımlandı",
  "tender_id": "1234567",        // ekap_id (tek ihale detayına gider)
  "tender_title": "…",
  "tender_ikn": "2025/1234567",
  "institution": "…",
  "conversation_id": null,       // type=chat → asistan sohbeti id'si
  "filter_id": null,             // type=tender → kayıtlı filtre id'si
  "authority_detsis": "24308110",// type=tender → favori idare detsis_no'su
  "okas_kodlar": null,           // type=tender → OKAS kod CSV'si
  "read": false,
  "created_at": "2026-07-24T11:00:00Z"
}
```

---

## 4. Bildirime tıklanınca yönlendirme (push + uygulama-içi aynı mantık)

Bir bildirime (veya push'a) basıldığında **sırayla** şu alanlara bakıp ilkine göre
yönlendirin. Böylece "tek ihale mi, liste mi" doğru seçilir:

1. **`conversation_id`** dolu → Asistan **sohbet** ekranı
   `GET /assistant/conversations/{conversation_id}/`
2. **`filter_id`** dolu → **Kayıtlı filtre sonuçları**
   `GET /saved-filters/{filter_id}/` ile filtreyi al → `filters` JSON'unu
   `GET /ekap/tenders/?<filters>` olarak uygula (ya da doğrudan kayıtlı filtre ekranını aç).
3. **`authority_detsis`** dolu → **İdare ihale listesi**
   `GET /ekap/tenders/?idare_detsis=<authority_detsis>`
4. **`okas_kodlar`** dolu → **OKAS arama sonuçları**
   `GET /ekap/tenders/?okas_kod=<okas_kodlar>`  (CSV olduğu gibi gönderilir)
5. Yukarıdakiler boş, **`tender_id`** (veya `tender_ikn`) dolu → **Tek ihale detayı**
   `GET /ekap/tenders/<tender_id>/` (`tender_id` = `ekap_id`)

> Push tarafında karşılıkları: `conversationId` → `filterId` → `authorityDetsis` →
> `okasKodlar` → `tenderId`/`tenderIkn` (aynı sıra).

---

## 5. Favori İdareler (+ yeni ihale alarmı) — **YENİ**

Kullanıcı bir idareyi (DETSIS kurum düğümü) favoriler. `alarm` açıkken (varsayılan) o idare
**yeni bir ihale yayınladığında** kullanıcıya bildirim gider; bildirime basınca o idarenin
ihale listesi açılır.

- Favorileme **sınırsızdır** (Free + Pro).
- **Alarm bildirimi Pro'ya özeldir**: Free kullanıcı favoriyi kurar ama alarm push/bildirimi
  yalnızca Pro iken üretilir (uç 403 vermez; sadece bildirim gelmez). Pro'ya geçince başlar.

| Uç | Açıklama |
|----|----------|
| `GET /favorite-authorities/` | Favori idareleri listele. |
| `POST /favorite-authorities/` | Favoriye ekle / güncelle (upsert). |
| `GET /favorite-authorities/{detsis_no}/` | `{ "is_favorite": true }` |
| `DELETE /favorite-authorities/{detsis_no}/` | Favoriden çıkar (idempotent, 204). |

**İdare nereden bulunur?** İdare seçim ekranından (`GET /ekap/authorities/tree/` gezinme,
`GET /ekap/authorities/search/?q=` arama). Her düğümde `detsis_no` gelir — favoriye onu gönderin.

**Ekleme isteği** — yalnızca `detsis_no` yeterli; `ad`/`idare_id`/`has_items` sunucuda
`ekap.Authority`'den doldurulur:
```json
POST /favorite-authorities/
{ "detsis_no": "24308110", "alarm": true }
```
`alarm: false` → yalnız hızlı erişim (bildirim yok). Alan gönderilmezse `alarm` **true** kabul edilir.

**Liste yanıtı** (öğe):
```json
{
  "id": 7,
  "detsis_no": "24308110",
  "alarm": true,
  "idare_id": "12345",
  "ad": "Ankara Büyükşehir Belediyesi",
  "has_items": true,
  "added_at": "2026-07-24T09:00:00Z"
}
```

**Favoriye basınca** o idarenin ihaleleri:
`GET /ekap/tenders/?idare_detsis=<detsis_no>` (üst düğüm seçilirse alt birimlerin ihaleleri
de gelir).

**Alarm bildirimi** günde bir (11:00) kontrol edilir; push data'sı `{ "type": "tender",
"authorityDetsis": "<detsis_no>" }`, uygulama-içi satırda `authority_detsis` dolu gelir →
§4'e göre idare listesine yönlendirin.

---

## 6. İhale Alarmları (tekil ihale) — **Pro'ya özel**

Belirli bir ihale için alarm: ihale günü hatırlatması, doküman değişikliği, sonuçlanma.

- **Alarm kurma/güncelleme Pro'ya özeldir** → Free üye `POST` yapınca **403
  `premium_required`**. Alarmları **listeleme ve silme her üyeye açıktır**.

| Uç | Açıklama |
|----|----------|
| `GET /alarms/` | Kullanıcının alarmlarını listele. |
| `POST /alarms/` | Alarm kur/güncelle (upsert; `tender_id` anahtar). **Pro.** |
| `GET /alarms/{tender_id}/` | İhaleye kurulu alarmı getir (yoksa `data: null`). |
| `DELETE /alarms/{tender_id}/` | Alarmı sil (idempotent, 204). |

`tender_id` = ihalenin **`ekap_id`**'sidir (İKN değil).

**Kurma isteği**:
```json
POST /alarms/
{
  "tender_id": "1234567",
  "tender_ikn": "2025/1234567",
  "tender_title": "Bilgisayar ve Çevre Birimi Alımı",
  "institution": "Ankara Büyükşehir Belediyesi",
  "reminder_day": true,       // ihale günü hatırlat
  "document_change": true,     // doküman değişince bildir
  "completed": false           // sonuçlanınca bildir
}
```

Alarm bildirimleri günde bir (09:00) kontrol edilir; kullanıcı başına **tek özet push**
(`data.type = "alarm"`; tek olayda `tenderId`/`tenderIkn` ile ihale detayına gider).

---

## 7. Kayıtlı Filtre Alarmı — filtre serbest, **alarm Pro'ya özel**

Kullanıcı bir arama filtresini kaydeder; `alarm` açıkken filtreye **uyan yeni ihaleler**
bildirilir.

- **Filtre kaydetmek sınırsız ve serbesttir** (Free + Pro).
- **Alarm açık** kaydedilir/güncellenirse **Pro** gerekir → Free üye **403 `premium_required`**.
  Alarmı **kapatmak / alarmsız kaydetmek** her üyeye serbesttir.

| Uç | Açıklama |
|----|----------|
| `GET /saved-filters/` | Kayıtlı filtreleri listele. |
| `POST /saved-filters/` | Filtre kaydet. `alarm` açıksa **Pro.** |
| `GET /saved-filters/{id}/` | Tek filtreyi getir. |
| `PUT/PATCH /saved-filters/{id}/` | Güncelle. Sonuçta `alarm` açık kalırsa **Pro.** |
| `DELETE /saved-filters/{id}/` | Sil (204). |

**Kaydetme isteği**:
```json
POST /saved-filters/
{
  "name": "Ankara bilgisayar alımları",
  "filters": { "q": "bilgisayar", "il_id": "251", "ihale_tip": "1" },
  "tags": ["donanım", "ankara"],
  "alarm": true
}
```
- `filters` = `GET /ekap/tenders/` sorgu parametreleri (alan adları Tender model
  alanlarıdır: `q`, `ihale_adi`, `il_id`, `ihale_tip`, `ihale_usul`, `ihale_durum`,
  `idare_detsis`, `okas_kod`, `ihale_tarihi_min/max`, `ilan_tarihi_min/max` …).
- `alarm`: `true` / `false` ya da `{ "enabled": true }` gibi bir nesne kabul edilir.

Filtre eşleşmesi günde bir (10:00) kontrol edilir; push data'sı `{ "type": "tender",
"filterId": <id> }`, uygulama-içi satırda `filter_id` dolu → §4'e göre filtre sonuçlarına yönlendirin.

---

## 8. Diğer bildirim kaynakları (bilgi)

Bu ikisi ekstra bir mobil işlem gerektirmez; sadece §4 yönlendirmesiyle ele alın:

- **OKAS önerisi** (`recommend_by_saved_okas`, herkese/ücretsiz): kullanıcının kayıtlı
  ihalelerinin OKAS kodlarına göre son 24 saatte yayınlanan ihaleler. Bildirim `type=tender`
  + `okas_kodlar` (CSV) → `GET /ekap/tenders/?okas_kod=<CSV>`.
- **İhale Asistanı digest'i** (`match_recommendations`, Pro): günlük profil-tabanlı öneri.
  Bildirim `type=chat` + `conversation_id` → ilgili asistan sohbeti açılır.

---

## 9. Özet — hangi özellik hangi katmanda

| Özellik | Free | Pro | 403 döner mi? |
|---------|:----:|:---:|:-------------:|
| Kayıtlı ihale / filtre / favori idare **kaydetme** | ✅ sınırsız | ✅ sınırsız | Hayır |
| Favori idare **kaydetme** | ✅ | ✅ | Hayır |
| Favori idare **alarm bildirimi** | ❌ (gelmez) | ✅ | Hayır (sessiz atlanır) |
| İhale alarmı **kurma** | ❌ | ✅ | **Evet** (POST /alarms/) |
| Kayıtlı filtre **alarmı** | ❌ | ✅ | **Evet** (alarm açık POST/PUT) |
| OKAS önerisi bildirimi | ✅ | ✅ | Hayır |
| Asistan digest bildirimi | ❌ | ✅ | Hayır (sessiz atlanır) |

**403 gördüğünüz iki yer**: `POST /alarms/` ve `POST|PUT|PATCH /saved-filters/` (alarm açık).
Her ikisinde de `errors.code == "premium_required"` → abonelik ekranını sunun.
