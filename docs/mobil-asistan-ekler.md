# İhale Asistanı — doküman ve fotoğraf ekleri (mobil sözleşmesi)

**Durum:** backend kodda hazır (2026-11-11), **üretimde kapalı** (`ASSISTANT_EK_KAPSAM=kapali`).
Açılış sırası: kapalı deploy → yalnızca staff → herkes. Plan: `docs/plan-asistan-ekler.md`.
Mobil plan: `~/Desktop/IhaleTakip/docs/plan-asistan-ekler.md`.

## 0. Bir cümlede

Kullanıcı ihale dokümanını, telefondaki bir dosyayı ya da bir fotoğrafı asistan sohbetine
ekleyip o dosya hakkında soru sorabilir: dosya önce ayrı bir uçla yüklenir ve sunucuda
hazırlanır, sonra mesajla birlikte kimliğiyle gönderilir.

## 1. ⚠️ Önce okuyun

- **Türler ve sınırlar `GET /assistant/attachments/limits/`'ten gelir, koda yazılmaz.**
  `etkin=false` ise "+" düğmesi, doküman çipleri ve "Asistana sor" girişleri çizilmez.
  İstek başarısızsa yedek değerlerde `etkin=false` olmalı.
- **`etkin` kullanıcı bazındadır** (önce yalnızca staff açılır) ve **geri alınabilir**.
  `etkin=false` iken doküman analizi için eski `/ai/analyze/` yolu çalışmaya devam eder;
  uç kaldırılmadı. AI Analiz ekranı bu yüzden silinmemeli.
- **413 HTML döner.** 25 MB'ı aşan istek nginx'te düşer, gövde JSON değildir ve kod
  taşımaz. `status === 413` → `dosya_buyuk` sayın; boyutu yüklemeden önce
  `limits.azami_bayt` / `limits.foto_azami_bayt` ile denetleyin.
- **HEIC göndermeyin.** iOS fotoğrafını JPEG'e çevirin. Arşivler (ZIP/RAR/7z) de
  reddedilir; arşivi açıp içindeki dosyayı gönderin.
- **Dosya adı ayrı `ad` alanında gider.** Dosya yolu ASCII bir kopya olabilir; sunucu
  Türkçe orijinal adı `ad`'dan alır.
- **Ek varken boş mesaj serbesttir.** Sunucu varsayılan soruyu yazar ("Bu dokümanı
  özetler misin?" / "Bu fotoğrafta ne var, açıklar mısın?").
- **`yetenekler: ["ek"]`** her kullanıcı mesajında (yalnızca metin olsa da) ve
  **yalnızca `etkin=true` iken** gönderilir. Gönderilmezse asistan "+" düğmesinden söz
  etmez; `etkin=false` iken gönderilirse sunucu yine yok sayar.
- **Ek durumu `/ai/tasks/` ile DEĞİL, ekin kendi ucuyla yoklanır:**
  `limits.yoklama_aralik_ms`'de bir, en çok `limits.yoklama_azami_sn` boyunca. Yükleme
  **200 + `durum:"hazir"`** dönerse (tekilleştirme) hiç yoklanmaz.
- **Hata kodları iki yerde durur:**
  - 4xx: `e.response.data.errors.code`. Ek alanlar da orada: `errors.sifirlanma`,
    `errors.limit`. ⚠️ axios 4xx'te `unwrap`'tan önce reddeder; `e.apiError` boş kalır.
  - Hazırlama hatası: `ek.hata_kodu` + `ek.hata_mesaji`.
- **`ek.hata_mesaji` doluysa onu gösterin** (tek metin kaynağı); boşsa yerel yedek metin.
- **`gunluk_kalan`** her yükleme ve silme yanıtında gelir; önbellekteki değeri ezin.
  Sohbetteki kalan hak `sohbet_kalan`'dadır (sohbet ayrıntısı, sayfa 1).

## 2. Akış

```
GET  /assistant/attachments/limits/            → {etkin, sınırlar, gunluk_kalan, yoklama…}
POST /assistant/attachments/  (multipart)      → 202 {ek(hazirlaniyor), gunluk_kalan}
                                                  200 {ek(hazir), gunluk_kalan}  (tekrar)
GET  /assistant/attachments/<uuid>/  (yokla)   → {ek: hazir | hata}
POST /assistant/chat/ {message?, conversation?, tender?, ekler:[uuid], yetenekler:["ek"]}
                                               → 202 {task_id, conversation_id}  (değişmedi)
GET  /ai/tasks/<task_id>/                      → analysis = asistan mesajı (değişmedi)
GET  /assistant/conversations/<id>/  (sayfa 1) → + ekler, sohbet, sohbet_kalan
```

Yükleme ve gönderme bilinçli olarak ayrıdır: kullanıcı soruyu yazarken dosya hazırlanır,
"çok uzun" gibi hatalar göndermeden önce görünür.

## 3. Uçlar

Hepsi JWT ister, yanıtlar global zarf içindedir (`{success, message, data}`).

### 3.1 `GET /assistant/attachments/limits/`

```json
{
  "etkin": true,
  "azami_bayt": 20971520,
  "foto_azami_bayt": 10485760,
  "turler": [{"uzanti": "pdf", "tur": "dokuman"}, {"uzanti": "jpg", "tur": "fotograf"}],
  "mesaj_azami": {"dokuman": 2, "fotograf": 4},
  "sohbet_azami": {"dokuman": 5, "fotograf": 12},
  "gunluk_kalan": {"dokuman": 18, "fotograf": 40},
  "saklama_gun": 30,
  "yoklama_aralik_ms": 1500,
  "yoklama_azami_sn": 120
}
```

`turler` istemcinin baştan eleyebileceği uzantılardır: pdf, doc, docx, xls, xlsx, rtf,
txt, csv, htm, html, mht, mhtml (doküman) · jpg, jpeg, png, webp, gif (fotoğraf). Asıl
karar sunucuda **dosya imzasıyla** verilir.

### 3.2 `POST /assistant/attachments/` — `multipart/form-data`

| alan | zorunlu | not |
|---|---|---|
| `dosya` | ✓ | dosyanın kendisi |
| `ad` |  | Türkçe orijinal ad (boşsa dosya adı) |
| `tur` |  | `dokuman` \| `fotograf`. İmzayla çelişirse **imza kazanır** |
| `kaynak` |  | `ihale_dokumani` \| `arsiv` \| `cihaz` (vars.) \| `kamera` \| `galeri` |
| `ikn`, `ekap_id`, `dtn`, `parca` |  | etiket ve bağlam ("Teknik Şartname") |

`kaynak` eşlemesi: sohbetteki "İhale dokümanları" → `ihale_dokumani` · "Kayıtlı
dokümanlarım", doküman gezgini / DocumentSheet "Asistana sor", AI Analiz yönlendirmesi →
`arsiv` · "Telefondan dosya seç" → `cihaz` · "Fotoğraf çek" → `kamera` · "Galeriden seç" →
`galeri`.

| durum | anlam |
|---|---|
| `202` | kabul edildi, `ek.durum = "hazirlaniyor"`; yoklayın |
| `200` | aynı dosya daha önce hazırlanmış (tekilleştirme): `ek.durum = "hazir"`, hak harcanmadı, **yoklamayın** |
| `400` | `profil_yok` · `dosya_buyuk` (+`errors.limit`) · `desteklenmeyen_tur` · `gecersiz_dosya` |
| `403` | `premium_required` (paywall) |
| `413` | nginx, HTML gövde → `dosya_buyuk` sayın |
| `429` | `ek_kotasi` (+`errors.sifirlanma` ISO, `errors.limit`) |
| `503` | `ek_gecici_kapali` (kapsam kapalı) |

### 3.3 `GET /assistant/attachments/<uuid>/`

`{ "ek": {...} }` · başkasının ya da olmayan ek: `404 ek_bulunamadi`.

### 3.4 `DELETE /assistant/attachments/<uuid>/`

Yalnızca **sohbete bağlanmamış** ek (tepsiden çıkarma). Satır ve dosyalar silinir,
günlük hak iade edilir: `{ "silindi": true, "gunluk_kalan": {...} }`.
Bağlıysa `409 ek_bagli`.

### 3.5 `POST /assistant/chat/` — yeni alanlar

```json
{ "message": "", "conversation": 812, "tender": null,
  "ekler": ["6f1c2b8e-1d2a-4c55-9b0e-2f7e1a9c3d41"], "yetenekler": ["ek"] }
```

- `ekler`: en çok 6 kimlik; ayrıca mesaj başına ≤2 doküman ve ≤4 fotoğraf, sohbet başına
  ≤5 doküman, ≤12 fotoğraf ve toplam ≤200 bin token.
- Başka sohbete bağlı bir ek gönderilirse içeriği yeni sohbete **kopyalanır** (yeniden
  yükleme ve hak yok).
- Yanıt **değişmedi**: `202 {task_id, conversation_id}`.

| durum | `errors.code` | öneri |
|---|---|---|
| `400` | `profil_yok` | firma sihirbazı (mesaj: "Önce firma profilinizi oluşturun.") |
| `400` | `mesaj_ek_siniri` (+`errors.limit`) | tepside fazla dosya |
| `404` | `ek_bulunamadi` | kartı kırmızıya çevir + "Yeniden ekle" |
| `409` | `ek_hazir_degil` | yoklamayı sürdür, mesajı gönderme |
| `409` | `sohbet_ek_siniri` | "Bu sohbette çok fazla dosya var." + "Yeni sohbet aç" |
| `410` | `ek_suresi_doldu` | "Yeniden ekle" |
| `503` | `ek_gecici_kapali` | "Dosya ekleme şu an kapalı." (metin yalnız gönderilebilir) |

⚠️ Hata olursa **hiçbir şey yazılmaz** (sohbet ve mesaj da açılmaz).

### 3.6 `GET /assistant/conversations/<id>/` — sayfa 1'de ek alanlar

```json
{
  "list": [...], "totalCount": 6, "page": 1,
  "ekler": [{ "...ek nesnesi..." }],
  "sohbet": {"ikn": "2026/123456", "ekap_id": "mobil:2026-123456",
             "ihale_adi": "...", "il": "ANKARA", "ihale_tarihi": "02.12.2026 10:00"},
  "sohbet_kalan": {"dokuman": 4, "fotograf": 12}
}
```

- `ekler`: sohbetin **tüm** ekleri (süresi dolanlar dahil), sayfadan bağımsız.
- `sohbet`: ihale odaklı sohbetin ihalesi, ihale kartıyla aynı şekil →
  `cardToTenderContext(data.sohbet)` olduğu gibi kullanılır. İhalesiz sohbette `null`.
- 2. ve sonraki sayfalarda bu alanlar yoktur.

## 4. `ek` nesnesi

| alan | tip | not |
|---|---|---|
| `id` | uuid | |
| `tur` | `dokuman` \| `fotograf` | imzadan |
| `kaynak` | bkz. 3.2 | |
| `ad`, `uzanti`, `boyut` | string, string, int | |
| `durum` | `hazirlaniyor` \| `hazir` \| `hata` \| `suresi_doldu` | |
| `hata_kodu`, `hata_mesaji` | string \| null | bkz. 6 |
| `mod` | `metin` \| `pdf` \| `gorsel` \| null | modele nasıl gittiği |
| `sayfa_sayisi` | int \| null | `cok_uzun` / `taranmis_cok_sayfa`'da da dolu |
| `sira` | int \| null | sohbetteki "Ek N" (bağlanınca) |
| `ikn`, `ekap_id`, `dtn`, `parca` | string \| null | |
| `kucuk_resim` | string \| null | fotoğrafta ≤6 KB `data:image/jpeg;base64,…` (liste kartı). Tam ekran yerel dosyadan açılmalı |
| `created_at`, `expires_at` | ISO \| null | `expires_at` = silinme zamanı ("30 gün sonra silinir") |

Dosya içeriği ve sunucu tarafı dosya anahtarı **hiçbir yanıtta yoktur**.

## 5. Mesaj ve blok şekilleri

- Kullanıcı mesajının `payload.ekler`: gönderim anındaki **anlık özet**
  `[{id, tur, ad, uzanti, sira, parca, kucuk_resim}]`. Durum içermez; güncel durum ve
  sayfa sayısı `data.ekler`'den `id` ile birleştirilir (ör. süresi dolan kartta
  "Süresi doldu").
- Kullanıcı mesajının `payload.yetenekler`: gönderilen değer.
- Asistan mesajının `payload.blocks` içinde yeni blok **`dokuman_sec`**:

```json
{ "type": "dokuman_sec", "ikn": "2026/123456", "ekap_id": "mobil:2026-123456",
  "metin": "Şartnameyi okuyabilmem için dokümanı seçin." }
```

  Asistan şartnameye ihtiyaç duyup ek yoksa (ve `yetenekler:["ek"]` geldiyse) üretir;
  turda en çok bir tane. `ikn`/`ekap_id` karttadır, böylece `tenderContext` boşken de
  doküman seçici açılabilir. Bilinmeyen blok tipini atlayan eski sürümler etkilenmez.

## 6. Hata kodları ve önerilen metinler

`hata_mesaji` doluysa onu kullanın; tablo yerel yedek içindir.

| kod | nerede | metin |
|---|---|---|
| `premium_required` | 403 `errors.code` | paywall |
| `profil_yok` | 400 `errors.code` | firma sihirbazı |
| `ek_kotasi` | 429 `errors.code` | "Bugünkü doküman hakkınız doldu (20). Yarın yenilenir." |
| `dosya_buyuk` | 400 `errors.code` / 413 | "Dosya çok büyük (34 MB). En fazla 20 MB." |
| `desteklenmeyen_tur` | 400 `errors.code` | "Bu dosya türü okunamıyor. PDF, Word, Excel ya da fotoğraf gönderin." |
| `gecersiz_dosya` | 400 `errors.code` / `ek.hata_kodu` | "Dosya bozuk görünüyor. Dokümanı yeniden indirip deneyin." |
| `sifreli` | `ek.hata_kodu` | "Dosya şifreli olduğu için okunamıyor." |
| `cok_uzun` | `ek.hata_kodu` | "Doküman çok uzun (≈180 sayfa). En fazla ≈100 sayfa okunabilir. İlgili sayfaların fotoğrafını gönderebilirsiniz." |
| `taranmis_cok_sayfa` | `ek.hata_kodu` | "Taranmış dokümanlarda en fazla 30 sayfa okunabilir. …" |
| `metin_yok` / `okunamadi` / `eski_word` | `ek.hata_kodu` | "Dosya açılamadı. Fotoğrafını çekip gönderebilirsiniz." |
| `ek_bulunamadi` / `ek_hazir_degil` / `ek_suresi_doldu` | 404 / 409 / 410 | yeniden ekle / bekle |
| `mesaj_ek_siniri` | 400 | "Bir mesajla en fazla 2 doküman ve 4 fotoğraf gönderebilirsiniz." |
| `sohbet_ek_siniri` | 409 | "Bu sohbette çok fazla dosya var." + "Yeni sohbet aç" |
| `ek_bagli` | 409 (DELETE) | sohbete gönderilmiş ek silinemez |
| `ek_gecici_kapali` | 503 | "Dosya ekleme şu an kapalı." |

Bizden kaynaklanan `okunamadi` hatasında günlük hak iade edilir.

## 7. Ne değişmedi

- `POST /assistant/chat/` yanıtı ve `GET /ai/tasks/<id>/` sözleşmesi aynı.
- `message` hâlâ en çok 2.000 karakter; ek yokken boş mesaj yine 400.
- Ek gönderilmeyen sohbetler bugünkü gibi çalışır.
- `/ai/analyze/` ve AI Analiz ekranı yerinde (kapatma anahtarının yedeği).
- Sohbet ayrıntısındaki `list/totalCount/page` aynı; yeni alanlar yalnızca eklenir.

## 8. Yapılacaklar

**Zorunlu**
- `/limits/`'i oku; `etkin=false` iken hiçbir ek girişi çizme.
- Multipart yükleme (`transformRequest: d => d`, `Content-Type: multipart/form-data`).
- 4xx gövdesini `e.response.data.errors` üzerinden oku (sohbet gönderimi dahil).
- `yoklama_azami_sn` boyunca yokla; 200 + `hazir`'da yoklama.
- `yetenekler:["ek"]` her mesajda, yalnızca `etkin=true` iken.
- HEIC → JPEG.

**Önerilen**
- `gunluk_kalan` / `sohbet_kalan` ile tepsi slotlarını soluk göster.
- `mergeAttachments(payload.ekler, data.ekler)` ile geçmişteki ek kartları.
- `dokuman_sec` bloğunu `MessageBlocks` tablosuna ekle.

**Yapmayın**
- Sınırları ve tür listesini koda yazmayın.
- Dosya içeriğini ya da istemcide gördüğünüz bir dosya kimliğini mesajla göndermeyin;
  yalnızca `ek.id`.
- AI Analiz ekranını `etkin=false` dalı test edilmeden silmeyin.

## 9. Gizlilik metni

Ekler yalnızca analiz için yapay zeka sağlayıcısına (Anthropic) iletilir ve
`saklama_gun` (30) gün sonra silinir. Önerilen metin:

> Asistana eklediğiniz dosya ve fotoğraflar yalnızca analiz için yapay zeka sağlayıcımıza
> (Anthropic) iletilir; 30 gün sonra sistemlerimizden silinir ve sağlayıcıya silme talebi
> gönderilir. Sağlayıcı, kendi saklama politikası gereği bunları kısa bir süre daha tutabilir.

Fotoğraflar sunucuda yeniden kodlanır; konum (GPS) ve diğer EXIF bilgileri silinir.

## 10. Test senaryoları

1. Metinli PDF yükle → 202 → yokla → `hazir`, `mod=metin`, `sayfa_sayisi` dolu.
2. Aynı dosyayı tekrar yükle → 200 + `hazir`, `gunluk_kalan` değişmedi.
3. 31 sayfalık taranmış PDF → `hata`, `taranmis_cok_sayfa`, `hata_mesaji` dolu.
4. `.pdf` uzantılı HTML (EKAP captcha sayfası) → 400 `gecersiz_dosya`.
5. HEIC → 400 `desteklenmeyen_tur`.
6. Boş mesaj + ek → asistan dokümanı özetler; kullanıcı balonunda varsayılan soru.
7. Aynı sohbette ikinci soru → cevap Ek 1'e dayanır, madde numarası verir.
8. Ekli mesajı yetersiz hakla gönder → doğru 4xx kodu ve eylem düğmesi.
9. Tepsideki eki ✕ ile çıkar → 200, `gunluk_kalan` arttı.
10. `ASSISTANT_EK_KAPSAM=staff` iken staff olmayan hesap → `etkin=false`, "+" yok.
11. Geçmişten ekli sohbeti aç → `data.ekler` ile kartlar, `data.sohbet` ile ihale bandı.
12. Maliyet düğmesi (ihale sohbeti, ek yok) → asistan benzer işleri karşılaştırır, tek
    rakamlık tahmini bedel vermez.
