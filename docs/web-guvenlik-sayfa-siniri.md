# Web portalı — güvenlik katmanı ve Free 3 sayfa sınırı (7. not)

> **Okuyucu:** web portalı ekibi (`ihaletakip-web`) · **Hazırlayan:** `ihaletakip-api` ·
> **Tarih:** 2026-11-05 · **Durum:** backend hazır ve üretimde; web tarafı bekleniyor.
> Numaralar `docs/backend/portal-istekleri-6.md`'den (#18) devam eder.

## Neden

4 Kasım gecesi (00:00–03:30 TR) betikle açılmış tek kullanımlık bir hesap
(`…@mailinator.com`, IP `78.164.78.132`, `HeadlessChrome` + `curl`) ihaletakip.com'da
**~23 bin sayfa** gezdi: `/ihaleler`, `/belge`, `/ihale/2016-…` ve her ihalede
"Dokümanları getir". Her doküman listesi EKAP'a 2 canlı istek atıyor. Mobil ve web'in
**ortak** EKAP kullanıcı bütçesi (günde 600 istek) 01:05'te bitti. O andan gece yarısına
kadar mobilde de web'de de **herkes** için doğrudan temin ayrıntısı, doküman listesi ve
indirme çalışmadı.

Aynı gece web'in iki davranışı sorunu büyüttü:

- Token taşıyan isteklere de `X-IT-Cache-Fill: 1` konuyordu. API bu yüzden isteği
  kullanıcının değil sitenin ortak dolum kovasına yazdı. Kazıyıcı bu kovayı doldurunca
  gerçek web kullanıcıları da 429 aldı (o gece 1.584 adet).
- Doğrudan temin ayrıntısı token'sız gidiyordu; API kimin istediğini göremiyordu.

Backend'de şunlar kuruldu. Hepsi bu notta ayrıntılı:

1. **Kişi başı EKAP kotası.** Yalnızca EKAP'a giden gerçek istek sayılır, önbellekten
   dönen cevap sayılmaz. Günlük: anonim **0**, yeni hesap (< 24 saat) **10**, Free **30**,
   Pro **60**. Saatlik tavan: yeni 5, Free 15, Pro 30.
2. **Free 3 sayfa sınırı.** Pro olmayan kullanıcı ihale, doğrudan temin, firma ve kurul
   kararı listelerinde en çok 3 sayfa görür. Sayfa boyutu en çok 20'dir. Bildirimden
   açılan listeler de dahil.
3. **İnsan doğrulaması (Cloudflare Turnstile).** Yalnızca web'de, yalnızca çok istek
   atan kimlikte ve e-posta kaydında istenir. Oturum başında istenmez.
4. **Erişim engelleri.** IP/CIDR kara listesi, hesap askıya alma ve tek kullanımlık
   e-posta alanı yasağı. Hepsi admin'deki yeni **Güvenlik Paneli**'nden (`/admin/guvenlik/`)
   yönetilir.

## Özet

| #   | Konu | Öncelik | Ne zaman |
| --- | --- | --- | --- |
| #19 | Token'lı isteğe `X-IT-Cache-Fill` koyma, kullanıcı IP'sini geçir | **Şart** | Hemen |
| #20 | Doğrudan temin ayrıntısına token + client | **Şart** | **Hemen** (şu an web'de yeni DT ayrıntısı gelmiyor) |
| #21 | Free 3 sayfa sınırı (`MAX_PAGE.free = 3`) + derin sayfaları token'la iste | **Şart** | Hemen (sınır web'de zaten açık) |
| #22 | 428 `human_verification_required` → Turnstile kutusu | **Şart** | Backend'i açmadan önce |
| #23 | Kayıt formunda Turnstile | **Şart** | Backend'i açmadan önce |
| #24 | Yeni hata kodlarını ekrana bağla | **Şart** | Hemen |
| #25 | Gerçek tarayıcı kimliğini (`X-IT-Client-UA`) ilet | Yüksek | Hemen |
| #26 | Ek sertleştirmeler (Cloudflare WAF, prefetch, `noindex` …) | Öneri | Uygun zamanda |

Backend'deki açma düğmeleri ve yayın sırası en sondaki **"Yayın sırası"** bölümünde.

---

## #19 Token'lı isteğe `X-IT-Cache-Fill` koyma (şart)

### Sorun

`src/lib/api/core.ts:59-66` ve `setClientHeaders` (`core.ts:139-148`) `X-IT-Cache-Fill: 1`
başlığını yalnızca `client` geçilip geçilmediğine bakarak koyuyor; token olup olmamasına
bakmıyor. Token taşıyıp `client` geçmeyen ve **önbelleğe alınmayan** istekler bu yüzden
"dolum" olarak gidiyor, IP'leri de API'ye ulaşmıyor:

- `auth/profile/` — `src/lib/auth/viewer.ts:21`
- benchmark — `src/features/tenders/benchmark.ts:45`
- **doküman listesi** — `src/features/tenders/documents.ts:48`
- pazar — `src/features/market/queries.ts:14,27`
- idare raporu — `src/features/authorities/queries.ts:59`
- hesap ve onboarding okumaları — `src/features/account/queries.ts:27`

### Backend'de yapılan

API artık **girişli isteği dolum kovasına hiç koymuyor**: token varsa kimlik kullanıcıdır,
`X-IT-Cache-Fill` yok sayılır. Ortak kovaya düşme sorunu web değişmeden de kapandı.

Kişinin **IP'si** ise hâlâ gelmiyor. IP şu iki iş için gerekli:

- **IP kara listesi.** Engelli bir IP'den gelen web kullanıcısını API ancak
  `X-IT-Client-IP` ile tanır. Bu başlık gelmezse istek, web konteynerinin kendi adresinden
  (Docker ağ geçidi) gelmiş gibi görünür ve o adres engellenemez.
- **İnsan doğrulamasının IP sayacı.** Girişli istekte hesabın sayacına ek olarak IP'nin
  sayacı da tutulur. Aynı IP'den hesap çoğaltan kazıyıcı bu sayaçla yakalanır.

### İstenen

- `setClientHeaders`: token varsa **dolum başlığını koyma**, `client` verilmemişse de
  `requestClient()` ile IP'yi ekle.
- Kural: **token taşıyan istek ⇒ `X-IT-Client-IP` var ⇒ `X-IT-Cache-Fill` yok.**
- `X-IT-Cache-Fill` yalnızca gerçekten kullanıcısız ve önbelleğe yazılan isteklerde kalsın:
  liste/detay satırları, `"use cache"`.

### Kabul

- Girişli bir kullanıcı "Dokümanları getir"e bastığında API logunda kimlik `u:<id>`, IP
  kullanıcının gerçek IP'si görünür. Admin → **Güvenlik Paneli** → "Bugün en yoğun
  kimlikler" satırında `web` ve doğru IP yazar.
- Token'lı hiçbir istekte `X-IT-Cache-Fill` başlığı yoktur (birim testi:
  `setClientHeaders`).

---

## #20 Doğrudan temin ayrıntısına token + client (şart, hemen)

### Sorun

`src/features/direct-procurement/queries.ts:45` DT ayrıntısını (`ekap/dogrudan-temin/<dtn>/`)
`requestClient()` ile ama **token'sız** istiyor. Sayfa giriş istiyor
(`dogrudan-temin/[dtn]/page.tsx:49`), istek ise anonim gidiyor.

### Backend'de değişen

Anonim kimliğin EKAP kotası **0**. Bu kararla anonim istek, ayrıntısı daha önce hiç
açılmamış bir alım için EKAP'a gidemez. O alımda yanıt yine `200` döner, ama `detay`
alanı `null` olur:

```json
{
  "success": true,
  "data": {
    "dtn": "26DT2200418",
    "detay": null,
    "mesaj": "Bu bilgiyi görüntülemek için giriş yapmanız gerekiyor.",
    "hata_kodu": "ekap_quota"
  }
}
```

Daha önce açılmış alımlar etkilenmez: ayrıntı veritabanında kalıcı duruyor
(`DogrudanTemin.detail_raw`) ve oradan döner. **11.917 alımın yalnızca ~520'sinin**
ayrıntısı kayıtlı, yani web kullanıcılarının çoğu bu düzeltme yapılana kadar ayrıntıyı
**göremez**.

### İstenen

`queries.ts:45`: DT ayrıntısını kullanıcının token'ı ve `client` ile iste, önbelleğe
alma, `X-IT-Cache-Fill` koyma. `/belge` DT dokümanını zaten token'la çekiyor
(`src/app/belge/route.ts:100`); bu doğru.

### Kabul

- Girişli Free kullanıcı, ayrıntısı daha önce alınmamış bir DT'yi açınca ayrıntı gelir.
- Panelde o kullanıcının EKAP sayacı (gün/saat) 1 artar.

---

## #21 Free 3 sayfa sınırı (şart)

### Ürün kararı

**Pro olmayan kullanıcı listelerde en çok 3 sayfa gezebilir.** 4. sayfa Pro ister. Kural
anonim kullanıcıyı da kapsar; web'de anonim kullanıcı zaten 1 sayfa görüyor ve öyle kalır.
Kapsam:

| Liste | API ucu | Web dosyası |
|---|---|---|
| İhaleler (arama, filtre, bildirimden açılan liste) | `GET /ekap/tenders/` | `tender-results.tsx` |
| Doğrudan temin | `GET /ekap/dogrudan-temin/` | `dt-results.tsx:33` |
| Firmalar | `GET /ekap/contractors/` | `contractor-results.tsx` |
| KİK kurul kararları | `GET /ekap/kurul-kararlari/` | `decision-results.tsx:18` |

### API davranışı (üretimde açık)

- Pro olmayan kullanıcının `page ≥ 4` isteği **403** alır:

```json
{
  "success": false,
  "message": "Daha fazla sonuç görmek Pro üyelere özeldir. Ücretsiz üyelikte ilk 3 sayfa görüntülenebilir; tüm sonuçlar için Pro'ya geçin.",
  "data": null,
  "errors": {
    "code": "premium_required",
    "detail": "…",
    "sebep": "page_limit",
    "azami_sayfa": 3
  }
}
```

- `azami_sayfa` **sayıdır** (metin değil). Değeri env'den değişebilir; web bu alanı
  okumalı, 3'ü koda yazmamalı.
- Pro olmayan istekte `page_size` sunucuda **20'ye kırpılır.** `page_size=100` ile
  3 sayfa 300 kayıt etmesin diye. Web zaten 20 kullanıyor.
- Pro kullanıcı ve süper kullanıcı etkilenmez.

### ⚠️ Açık kalan yol: token'sız derin sayfa

Girişli kullanıcının düz listesi bugün **token'sız**, paylaşılan önbellekten ve dolum
olarak gidiyor (`tender-results.tsx:107-110`, `tenders/queries.ts:48-58`). Dolum isteği
kimin için yapıldığını taşımaz; API kullanıcının Pro olup olmadığını bilemez. Bu yüzden
dolum için sayfa sınırı **kapalı** (`LISTE_SAYFA_SINIRI_DOLUM=kapali`). Açsaydık web'in
**Pro** kullanıcısı da 4. sayfada kesilirdi. Bu yol açık kaldıkça Free kullanıcı web'de
düz listelerde derine inebilir; sınır yalnızca anahtar kelimeli/firma filtreli aramalarda
(token'lı istekler) çalışır.

### İstenen

1. `src/config/security.ts:7,14`: `MAX_PAGE = { anonymous: 1, free: 3, pro: 50 }`.
   Pro bilgisi `getViewer()`'daki `is_premium`'dan gelir. Sınırın ötesindeki sayfada API
   hiç çağrılmaz; anonim için bugünkü `LoginForMore` gibi, Free için bir Pro kutusu
   gösterilir.
2. **Sayfa > 1 istekleri token + client ile gitsin** (dolum değil). 1. sayfa paylaşılan
   önbellekte kalabilir; SEO ve hız orada.
3. 403 + `errors.sebep === "page_limit"` → genel hata paneli yerine Pro kutusu:
   "İlk 3 sayfa ücretsiz, tüm sonuçlar Pro'da" + QR geçişi (#15–#17). `ApiErrorPanel`
   bunu göstermemeli. `src/lib/api/errors.ts:95-114` zaten `PremiumRequiredError`
   üretiyor; `sebep` alanını taşısın.
4. Bildirimden açılan listeler (`filtre_idler`, `pencere_bas/bit`) **dahil**: 3. sayfadan
   sonrası yine Pro.

### Kabul

- Free hesapla `/ihaleler?sayfa=4` → Pro kutusu, API çağrısı yok.
- Elle `page=4` istendiğinde 403 `page_limit` → Pro kutusu (hata paneli değil).
- Pro hesapla 4.+ sayfa çalışır.
- 2 numaralı madde yayılınca bize haber verin. Biz `LISTE_SAYFA_SINIRI_DOLUM=uygula`
  yapacağız; o andan sonra token'sız derin sayfa isteği de 403 alır.

---

## #22 İnsan doğrulaması: 428 → Turnstile (şart, backend'i açmadan önce)

### Ürün kararı

EKAP'ın kendi portalındaki "robot değilim" kutusunun aynısı. Ama **herkese oturum başında
değil**, yalnızca çok istek atan kimliğe (+ kayıt formunda, #23). Normal bir kullanıcı
bunu neredeyse hiç görmez.

### API davranışı

- Web'den gelen her kapsamlı çağrı sayılır: ihale/firma/DT listesi, detay, belge.
  Sayaç girişli kullanıcıda hesaba, anonimde IP'ye bağlıdır; 10 dakikalık pencere
  kullanılır.
- Varsayılan eşik: anonim **100**, girişli **150** çağrı. Bir ihale sayfası ~4 çağrı
  ettiği için bu ~25–40 sayfa demektir. Girişli istekte IP'nin kendi sayacı da tutulur
  (eşik 400).
- Eşik aşılınca istek **HTTP 428** alır:

```json
{
  "success": false,
  "message": "Çok sayıda istek algılandı. Devam etmek için lütfen robot olmadığınızı doğrulayın.",
  "data": null,
  "errors": { "code": "human_verification_required", "detail": "…" }
}
```

- Doğrulama ucu: `POST /api/v1/guvenlik/insan-dogrulama/`, gövde `{"token": "<turnstile token>"}`.
  - **Server Action'dan** çağrılır. Portal anahtarı, `X-IT-Client-IP` ve girişliyse
    `Authorization` gönderilir.
  - API token'ı Cloudflare `siteverify`'a `remoteip` ile doğrulatır. Hostname
    `ihaletakip.com` / `www.ihaletakip.com` olmalıdır.
  - Geçerse o kimliğin sayaçları **sıfırlanır** → `200 {"dogrulandi": true}`.
  - Yanıtlar: `400 human_verification_required` (token geçersiz/süresi dolmuş;
    `errors.nedenler` Cloudflare hata kodları), `403` (portal anahtarı yok), `503`
    (Turnstile yapılandırılmamış).
- Turnstile token'ı **tek kullanımlık** ve ~300 sn geçerlidir. Her denemede yeni token alın.
- Kazıyıcı her ~30 sayfada bir kutuyu çözmek zorunda kalır. Çözmezse mevcut hız sınırı
  ve kademeli geçici engel (15 dk / 1 sa / 24 sa) devreye girer.

### İstenen

1. **Widget**: Cloudflare Turnstile, *managed* mod.
   - Site key env'den gelsin: `NEXT_PUBLIC_TURNSTILE_SITE_KEY` = `0x4AAAAAAF0mPlLwrUn3B2L7` (üretim
     widget'ı; site key herkese açıktır, secret key yalnızca API sunucusunda durur).
   - `action: "api_dogrulama"` verin. Backend şimdilik action'ı zorunlu tutmuyor; ileride
     tutabilir.
2. **CSP** (`next.config.ts:20-33`): `script-src` ve `frame-src`'ye
   `https://challenges.cloudflare.com` ekleyin. Eklenmezse kutu hiç yüklenmez.
   ⚠️ `docs/gelistirme-rehberi.md` §9.2'deki (2026-10-26) "Turnstile yok" kararı bu notla
   değişti; rehberi güncelleyin.
3. **428'i yakalama** (`src/lib/api/errors.ts`): yeni bir `HumanVerificationRequiredError`.
   - Sayfa render'ında: hata paneli yerine Turnstile kutusu.
   - Server Action'da: `toActionError` → `human_verification_required` → istemci kutuyu
     açar.
4. **Akış**: kutu token verir → Server Action `POST guvenlik/insan-dogrulama/` → `200` →
   asıl isteği **bir kez** tekrarla. `400` gelirse kutuyu sıfırlayıp tekrar göster.
   Sonsuz döngüye girmeyin: art arda 3 başarısız doğrulamadan sonra "lütfen daha sonra
   deneyin" deyin.
5. **Bot**: doğrulanmış arama botu (`X-IT-Verified-Bot`) ve kullanıcısız önbellek
   dolumu bu kontrolden muaf. Web'in ek bir şey yapmasına gerek yok.

### Kabul

- Test ortamında eşiği 2 yapıp (`GUVENLIK_DOGRULAMA_ESIK={"anonim":2,…}`) üçüncü
  istekte kutu çıkar. Çözülünce sayfa açılır.
- Panelde "İnsan doğrulaması istendi / geçti" olayları görünür.

---

## #23 Kayıt formunda Turnstile (şart, backend'i açmadan önce)

### Sorun

Kazıyıcı hesabını bir betikle açtı: `curl /kayit`, 3 dakika sonra hesap hazırdı.

### API davranışı

- `POST /auth/email/register/start/` gövdesine **`turnstile_token`** alanı eklendi.
- Backend'de `TURNSTILE_KAYIT_ZORUNLU=True` iken web'den gelen kayıt bu token olmadan
  **400** `human_verification_required` alır. Mobil kayıt etkilenmez.
- **Tek kullanımlık e-posta alanları artık reddediliyor** (mailinator, yopmail,
  guerrillamail, 10minutemail, temp-mail …; liste admin'den genişletilebilir):
  **400** `disposable_email`, mesaj "Tek kullanımlık e-posta adresleriyle kayıt
  olunamıyor…". Bu kural **şu an açık**.

### İstenen

- Kayıt formunun 1. adımına Turnstile kutusu (managed). Token `turnstile_token` olarak
  gönderilsin.
- `disposable_email` hatası e-posta alanının altında gösterilsin.

### Kabul

Form token'la kayıt başlatır. Web yayılınca bize haber verin; `TURNSTILE_KAYIT_ZORUNLU`'yu
açacağız.

---

## #24 Yeni hata kodları (şart)

| Kod | HTTP | Nerede | Web ne yapmalı |
|---|---|---|---|
| `ekap_quota` | **200** + `data.hata_kodu` | DT ayrıntısı, `documents/` | `data.mesaj`'ı göster. "Ayrıntı alınamadı, tekrar dene" DEME — tekrar denemek işe yaramaz. |
| `ekap_quota` | **429** + başlık `X-IT-Hata: ekap_quota` | `document/`, DT `dokuman/` (`/belge`) | `/belge` bugün düz metin 404/502 dönüyor (`belge/route.ts:112-120`); kota durumunda kullanıcıya mesajı göster. |
| `premium_required` + `sebep: page_limit` | 403 | 4 liste ucu | #21: Pro kutusu. |
| `human_verification_required` | 428 | kapsamlı uçlar | #22: Turnstile kutusu. |
| `human_verification_required` | 400 | doğrulama ucu, kayıt | Kutuyu sıfırla, tekrar dene. |
| `ip_blocked` | 403 | tüm `/api/` | "Bu ağdan erişim engellendi." Tekrar deneme düğmesi gösterme. |
| `account_suspended` | 403 | giriş (e-posta, Google, Apple) | Mesajı göster, giriş yapma. |
| (askıya alınan hesabın oturumu) | 401 | her istek | Token yenileme de 401 alır (hesap pasif) → bugünkü gibi çıkış yaptır. Sonraki giriş denemesinde `account_suspended` mesajı görünür. |
| `disposable_email` | 400 | kayıt başlatma | #23. |

`errors.ts:95-114` bugün `premium_required` dışındaki 403'leri genel `ApiError` yapıyor.
`ip_blocked` ve `account_suspended` için ayrı sınıf açın.

**Kota sayıları** (bilgi için; değişebilir, koda yazmayın):

| | gün | saat |
|---|---|---|
| anonim | 0 | 0 |
| yeni hesap (< 24 sa) | 10 | 5 |
| Free | 30 | 15 |
| Pro | 60 | 30 |

Önbellekten dönen cevap sayılmaz: aynı ihalenin doküman listesi 24 saat önbellekte,
DT ayrıntısı kalıcı. Bir indirme EKAP'a 2 istek atar (liste + indir), bir doküman
listesi de 2 (ihale + teknik şartname).

---

## #25 Gerçek tarayıcı kimliğini ilet (yüksek)

### Sorun

API'ye giden tüm web isteklerinin `User-Agent`'ı `node`. Kazıyıcıyı tanıtan şey
(`HeadlessChrome`, `curl`) yalnızca nginx'in web loglarında görünüyordu, API'de
görünmüyordu.

### İstenen

Kullanıcı adına giden her istekte (token'lı veya `client`'lı) yeni başlık:
`X-IT-Client-UA: <istemcinin User-Agent'ı>`, en çok 300 karakter. API bunu **yalnızca
portal anahtarı doğruysa** kabul eder.

### Kabul

Panelde "Tarayıcı" kolonunda gerçek tarayıcı yazar. Şüphe puanı `HeadlessChrome`/`curl`'ü
yakalar.

---

## #26 Ek sertleştirmeler (öneri)

1. **Cloudflare WAF hız kuralı**: `ihaletakip.com/ihale/*` ve `/belge` için doğrulanmamış
   botlara IP başına dakikada ~60 istek; aşanlara *Managed Challenge*. İstek web
   sunucusuna hiç ulaşmadan durur. (Cloudflare panelinde; bizim yerimize siz de
   açabilirsiniz.)
2. **Bot Fight Mode / Super Bot Fight Mode**: "Definitely automated" trafiğe challenge.
   Doğrulanmış arama botları muaftır; mevcut `X-IT-Bot` kuralı etkilenmez.
3. **"Dokümanları getir" düğmesi**: düğmenin yanında "Bu işlem günlük EKAP sorgu
   hakkınızdan düşer" notu. Kota dolunca düğmeyi pasifleştirip mesajı gösterin.
4. **Link prefetch**: liste sayfalarındaki ihale bağlantılarında Next.js prefetch'i
   kapatın (`prefetch={false}`). Her görünen bağlantı bir sunucu render'ı ve 4 API
   çağrısı tetikliyor; hem yük hem doğrulama sayacı gereksiz büyüyor.
5. **`noindex`**: 2. sayfa ve sonrası listeler (`?sayfa=N`) `noindex, follow` olsun.
   Arama motorunun derin liste taramasına gerek yok; ihale sayfaları zaten site haritasında.
6. **`/belge`**: bugünkü `sec-fetch-site` kontrolü doğru. Ek olarak aynı kullanıcı için
   aynı dosyanın 1 dakika içinde ikinci kez istenmesini web'de engelleyin (çift tıklama
   kotayı iki kez yakar).
7. **İzleme**: 428, 429 ve 403 `page_limit` sayıları web analitiğinde ayrı olay olsun.
   Ürün tarafı Pro dönüşümünü (`page_limit`) ve yanlış alarmı (428) buradan izleyecek.

---

## Yayın sırası (backend düğmeleri)

| Düğme (`.env.prod`) | Şu an | Ne zaman değişecek |
|---|---|---|
| Kişi başı EKAP kotası (`EKAP_KIMLIK_KOTA`) | **açık** | — |
| Girişli istekte dolumun yok sayılması | **açık** | — |
| Tek kullanımlık e-posta reddi | **açık** | — |
| IP / hesap engeli | **açık** | — |
| `LISTE_SAYFA_SINIRI_PORTAL` | **`uygula`** | — |
| `LISTE_SAYFA_SINIRI_DOLUM` | `kapali` | #21 madde 2 yayılınca → `uygula` |
| `GUVENLIK_DOGRULAMA_MODU` | `gozlem` (sayar, engellemez) | #22 yayılınca → `uygula` |
| `TURNSTILE_KAYIT_ZORUNLU` | `False` | #23 yayılınca → `True` |

Gözlem modunda API, kimin doğrulamaya düşeceğini sayar ama engellemez. Eşikleri bu
sayılara bakarak doğrulayıp sonra açacağız. API tarafında Turnstile secret key'i kurulu ve
Cloudflare'e karşı doğrulandı (2026-11-05); doğrulama ucu (`guvenlik/insan-dogrulama/`)
şimdiden çalışıyor, web geliştirirken üretim widget'ıyla uçtan uca denenebilir.
