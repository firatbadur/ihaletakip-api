# Doğrudan Temin + KİK Kurul Kararları — Web Portalı (ihaletakip.com) Notu

Uçlar, alanlar, Free/Pro kuralları ve bildirim yükü mobil ile **aynıdır** — tam istek/yanıt
örnekleri: **`docs/mobil-dogrudan-temin.md`** ve **`docs/mobil-kurul-karar.md`**.
Bu not yalnızca web'e (Next.js BFF, sunucu tarafı render, arama motoru taraması) özgü
noktaları anlatır.

> **Durum (2026-10-27): üretimde açık.** Doğrudan temin (DT) toplayıcısı mesai içinde
> 5 dakikada bir yeni ilanları yokluyor; KİK uyuşmazlık kararları her sabah 06:40'ta
> kontrol ediliyor. Ölçülen hacim: günde ~900 yeni DT ilanı, aynı anda **≥ 1.500** açık
> alım; kurulda haftada ~60–70 karar.

| İş | Uç (`API_URL` + …) | EKAP'a gider mi? |
|---|---|---|
| Açık DT listesi | `GET ekap/dogrudan-temin/` | ❌ (veritabanından) |
| DT filtre seçenekleri | `GET ekap/dogrudan-temin/filtreler/` | ❌ (sabit) |
| DT detayı | `GET ekap/dogrudan-temin/{dtn}/` | ⚠️ **ilk açılışta EVET** (30 dk önbellek) |
| DT dokümanı | `GET ekap/dogrudan-temin/{dtn}/dokuman/` | ⚠️ **her indirmede EVET** |
| DT kayıtlı filtreler (giriş) | `GET/POST dogrudan-temin/kayitli-filtreler/` (+`{id}/`) | ❌ |
| Kurul kararları | `GET ekap/kurul-kararlari/`, `…/toplantilar/` | ❌ |
| İhalenin kurul kararları | `GET ekap/tenders/{ekap_id}/kurul-kararlari/` + ihale detayındaki `kurulKararlari` | ❌ |

---

## 1. ⚠️⚠️ EKAP'a giden iki uç ve arama motorları

DT **detay** ve **doküman** uçları, ihale uçlarından farklı olarak canlı EKAP isteği yapar
ve bu istekler mobil uygulamanın belge indirmeleriyle **paylaşılan günlük bir bütçeden**
düşer. Web sayfası her render'da ya da her bot ziyaretinde detay ucunu çağırırsa bu
bütçe tükenir ve mobil kullanıcı "kota doldu" mesajı görür.

Backend iki koruma uygular — **ama web tarafının da aynı ilkeye uyması gerekir**:

- **Doğrulanmış bot** (`X-IT-Verified-Bot: 1`, portal anahtarıyla) detayda **canlı
  istek tetiklemez**: yanıt liste alanlarıyla gelir, `detay` yalnızca önbellekte varsa
  dolu, yoksa `null`. Doküman ucu bota **403** döner.
- Bu iki uç `belge` hız sınırı kapsamındadır (portal modu `uygula`): anonim IP başına
  dakikada 20, günde 200.

**Önerilen sayfa kurgusu (`/dogrudan-temin/[dtn]`):**
1. Sayfayı sunucuda **EKAP'a gitmeyen** liste ucuyla render edin:
   `GET ekap/dogrudan-temin/?dtn=<dtn>` → `data.list[0]` (ad, idare, il, tür, madde, son
   teklif, kalan süre, `dokuman_var`). Kayıt yoksa (kapanmış/iptal) → "Bu alım artık
   açık değil" + liste sayfasına bağlantı.
2. Duyuru metni, iletişim ve idare hiyerarşisi gibi "Ayrıntılar" bölümünü **istemci
   tarafında**, kullanıcı sayfayı açtıktan sonra bir BFF route'u üzerinden çekin
   (`GET ekap/dogrudan-temin/{dtn}/`). İlk açılış birkaç saniye sürebilir (en kötü
   15–20 sn) — bölümde yükleniyor göstergesi, zaman aşımı ≥ 30 sn.
3. Bu ayrıntı çağrısını **önbellek dolumu** (`X-IT-Cache-Fill: 1`) ile yapmayın ve
   sunucu tarafı önbelleğe koymayın: backend zaten 30 dk önbellekliyor, web önbelleği
   yalnızca bayat veri ekler.
4. DT sayfaları kısa ömürlüdür (çoğu 1–5 gün içinde kapanıyor). Detay sayfalarını
   `noindex` yapmak ya da sitemap'e koymamak önerilir; arama trafiği için liste/filtre
   sayfaları (il, tür) daha kalıcı hedeflerdir.

## 2. BFF çağrıları

- Tüm DT ve kurul çağrılarında portal anahtarı + kullanıcı IP'si gitmeli
  (`client: await requestClient()`, e-posta notundaki gibi). Gitmezse bütün web
  kullanıcıları konteynerin tek IP'sini paylaşır; liste `ihale_arama`, detay/doküman
  `belge`, ihale kurul kararları `detay` kapsamındadır ve portalda sınır **uygulanır**
  (429).
- **Doküman indirme** tarayıcıdan API'ye doğrudan gidemez (BFF, CORS yok) → bir route
  handler dosyayı **akış olarak** aktarmalı: `Content-Type` ve `Content-Disposition`
  başlıklarını olduğu gibi geçirin, gövdeyi belleğe toplamayın (dosya biçimi serbest,
  RAR olabilir, boyut sınırsız). `dokuman_var: false` ise düğme göstermeyin.
- **Birim fiyat teklif cetveli** (`birim_fiyat_cetveli_url`) EKAP'ın kendi adresidir —
  `target="_blank"` ile açın, BFF'den geçirmeyin. ⚠️ Sunucudan bu adrese istek atmak
  **çalışmaz** (eski EKAP portalı sunucu istemcisini TLS'te reddediyor — ölçüldü);
  tarayıcıda açılır (telefondan doğrulandı, masaüstü tarayıcıda ayrıca denenmedi).

## 3. Liste ve zaman

- Liste 5 dakikada bir değişir ve kayıtlar son teklif anında düşer → sunucu tarafında
  **en fazla 60 sn** önbellekleyin.
- `kalan_dakika` yanıtın üretildiği andaki değerdir; önbellekli bir yanıtta bayatlar.
  Geri sayımı istemcide `son_teklif`ten (ISO, `+03:00`) hesaplayın.
- `son_teklif` `null` olabilir ("Tarih belirtilmemiş") — uydurma tarih göstermeyin.
- Sayfalama: `page` + `page_size` (en çok 100). ≥ 1.500 açık kayıt var; tek istekte
  hepsini çekmeye çalışmayın.

## 4. Kayıtlı filtre ve alarm (giriş gerekir)

- Kaydetmek Free + Pro; `alarm: true` **Pro** (Free'ye 403 `premium_required`). Web'de
  satın alma yok → mevcut "Pro mobilden alınır" akışı.
- Tanınmayan filtre anahtarı **400** döner (geçerli anahtarlar mobil notta §4).
- Filtreyi listede açmak: `GET ekap/dogrudan-temin/?kayitli_filtre={id}` — **girişli**
  istek gerekir; anonim istek 400.
- Bildirimler web'e push olarak gelmez ama uygulama-içi listede görünür. `dtn_liste`
  dolu bir bildirim → `/dogrudan-temin?dtn=<dtn_liste>`; kurul bildirimi (`type=alarm`,
  `karar_no` dolu) → ihale sayfası, ilgili karar vurgulanmış.

## 5. Kurul kararları ve SEO

Kurul kararları tamamen veritabanındandır (EKAP'a gitmez), kalıcıdır ve kamuya açıktır →
indekslenebilir. İhale sayfasında `kurulKararlari` bölümü (ihale detay yanıtında hazır)
"Bu ihaleye itiraz edildi mi, ne karar verildi?" sorusunu cevaplar. Ayrı bir
`/kurul-kararlari` sayfası `GET ekap/kurul-kararlari/?q=&toplanti=` ile kurulabilir;
toplantı seçici için `…/toplantilar/`.
`karar_tur_kod` ham EKAP kodudur, anlamı backend'de eşlenmedi — `karar_aciklama` metnini
gösterin.
