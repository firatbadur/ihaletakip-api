# Mobil — Free 3 sayfa sınırı, kişi başı EKAP kotası ve hesap askıya alma

**Durum:** backend hazır ve üretimde (2026-11-05). Mobil için **sayfa sınırı henüz
uygulanmıyor**. Backend onu yeni sürümün göndereceği bir başlıkla açacak (§1.3). Diğer
maddeler (kota, askıya alma) **bugünkü sürümü de etkiliyor**; ekranlar bugün genel bir
hata metni gösteriyor ve doğru mesaja bağlanmaları gerekiyor.

Yol referansları `~/Desktop/IhaleTakip/src/` altındadır.

## Neden

4 Kasım gecesi betikle açılmış tek kullanımlık bir hesap web portalında ~23 bin sayfa
gezdi. Mobil ile web'in **ortak** EKAP bütçesi (günde 600 canlı istek) 01:05'te bitti.
Ertesi gün gece yarısına kadar **mobilde de** doğrudan temin ayrıntısı, ihale doküman
listesi ve indirme çalışmadı. Kullanıcılar "doğrudan temin yüklenmiyor" diye bildirdi.

Backend'de üç önlem alındı:

1. **Kişi başı EKAP kotası**: tek bir hesap ortak bütçeyi tüketemez.
2. **Free 3 sayfa sınırı** (ürün kararı): Pro olmayan kullanıcı listelerde en çok 3 sayfa
   görür.
3. **Hesap askıya alma + IP kara listesi**: admin'deki yeni Güvenlik Paneli'nden.

Web'deki insan doğrulaması (Turnstile, HTTP 428) **mobilde yoktur**; mobil bu kodu hiç
almaz.

---

## 1. Free 3 sayfa sınırı

### 1.1 Ürün kuralı

- Pro olmayan kullanıcı (anonim dahil) aşağıdaki listelerde **en çok 3 sayfa** görür.
  4. sayfa Pro ister.
- **Bildirimden açılan listeler de dahil.** Örneğin "70 ihale" diyen filtre bildirimini
  açan Free kullanıcı 3 sayfayı görür, sonrası için Pro'ya yönlendirilir.
- Sayfa, ekrandaki sayfadır (mobilde 10'luk). Pro olmayan istekte `page_size` sunucuda
  **en çok 20'ye** kırpılır.

| Ekran | Uç | page_size | Dosya |
|---|---|---|---|
| İhale listesi (arama, filtre, bildirim) | `GET /ekap/tenders/` | 10 | `screens/TenderList/index.js` |
| Doğrudan temin | `GET /ekap/dogrudan-temin/` | 20 | `screens/DirectProcurementList/index.js` |
| Firma arama | `GET /ekap/contractors/` | 20 | `screens/ContractorSearch/index.js` |
| KİK kurul kararları | `GET /ekap/kurul-kararlari/` | 20 | `screens/KurulKararlari/…` |

Firma sözleşmeleri, beklenen ihaleler ve asistan zaten Pro'ya özel; bu kurala girmez.

### 1.2 API yanıtı

4. ve sonraki sayfalar **403** döner:

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

- `azami_sayfa` sayıdır ve sunucu ayarıyla değişebilir. Uygulama 3'ü koda yazmamalı,
  bu alanı okumalı.
- `api/http.js:56-63` bu yanıtı görünce zaten `emitPremiumRequired({message})` çağırıyor;
  `Route.js:288-297` de Paywall'u (anonimse Login'i) açıyor. Mesaj, Paywall'un bağlam
  satırında görünür (`Paywall/index.js:135,414`). **Bu kısım hazır.**

### 1.3 ⚠️ Yeni başlık: `X-IT-App-Build` (zorunlu)

Mağazadaki sürüm 4. sayfadaki 403'te **listenin tamamını hata ekranına düşürüyor**
(aşağıda §1.4). Bu yüzden backend sınırı **yalnızca bu düzeltmeyi taşıyan sürüme**
uygular.

- **Tüm isteklere** `X-IT-App-Build: <build numarası>` başlığı eklenmeli (ör. iOS
  `CFBundleVersion`, Android `versionCode`; `react-native-device-info`
  `getBuildNumber()`). Yeri: `api/http.js` istek interceptor'ı. RNFS indirmelerine
  gerek yok.
- Bu sürümün build numarasını bize iletin. Backend'de
  `LISTE_SAYFA_SINIRI_MOBIL_MIN_BUILD=<numara>` yazılınca o ve sonraki sürümlerde sınır
  devreye girer. Eski sürümler etkilenmez; onlarda sınır yalnızca sayılır.
- Eski sürümleri zorunlu güncellemeyle (`AppGateModal`) kapattığımızda sınırı herkese
  açacağız (`LISTE_SAYFA_SINIRI_DIGER=uygula`).

### 1.4 Yapılması gerekenler

**a) TenderList — 4. sayfa hata ekranına düşmemeli (şart)**

- Bugün `api/v1/api.js:175-177` `searchTenders` her hatayı yakalayıp `null` döndürüyor;
  durum kodu kayboluyor. Ekran `setError(...)` (`TenderList/index.js:421`) ile **tam hata
  ekranı** gösteriyor, "Tekrar Dene" 1. sayfayı yeniden yüklüyor (`:661-671`). Aynı anda
  interceptor Paywall'u açıyor. Kullanıcı Paywall'u kapatınca yüklediği sonuçlar yerine
  hata ekranı görüyor.
- İstenen:
  - `searchTenders` hatayı `{error: {status, code, sebep}}` gibi taşısın.
  - `page > 1` isteği `premium_required` alırsa liste **olduğu gibi kalsın**,
    `hasMoreRef` false olsun ve listenin altına satır içi bir altlık çıksın:
    **"İlk 3 sayfa ücretsiz — tüm sonuçları görmek için Pro'ya geçin"** + "Pro'ya geç"
    düğmesi (Paywall'a, `source: 'page_limit'`).
  - Tam hata ekranı yalnızca 1. sayfa hatasında kalsın.

**b) DT, kurul kararları, firma — durma bayrağı (şart)**

- `DirectProcurementList/index.js:183` ve kurul kararları `loadMore` (`:94`) hatayı
  sessizce yutuyor. `onEndReached` bir sonraki kaydırmada yeniden tetiklendiği için 403
  **sonsuza kadar tekrar istenir**: Paywall her kaydırmada yeniden açılmaya çalışır,
  sunucuya da gereksiz istek gider.
- `ContractorSearch` hatayı `emptyPage()` ile bitiyor gibi gösteriyor
  (`api/v1/contractors.js:66-68`): kullanıcı neden bittiğini anlamıyor.
- İstenen: üç ekranda da `page_limit` gelince `hasMore=false` + TenderList'teki altlığın
  aynısı.

**c) İstemci ön kapısı (önerilir)**

`isPremiumUser` (`constants/subscription.js:89`) false ise ve sayfa `azami_sayfa`'ya
ulaştıysa 4. sayfa hiç istenmesin, altlık doğrudan gösterilsin. Sunucu yine son
sözdür; ön kapı yalnızca gereksiz isteği keser. `constants/subscription.js:4-8`'deki
"istemcide sert engel yok" kuralı, sunucunun da aynı kuralı uyguladığı bu durumda
çiğnenmiş olmaz. `azami_sayfa` ilk 403'ten öğrenilip saklanabilir; ilk açılışta 3
varsayılabilir.

**d) Bildirimden açılan liste (bilgi)**

`components/helpers/notificationRouting.js` TenderList / DirectProcurementList'i açıyor;
yukarıdaki davranış aynen geçerli. Bildirimdeki sayı ("70 ihale") değişmez. Kullanıcı
3 sayfayı görür, altlık sonrasını Pro'ya bağlar.

**e) Paywall bağlamı (önerilir)**

`screens/Login/helpers.js:49-68` `LOGIN_REASONS`'a ve Paywall kaynaklarına
`page_limit` eklenmeli. Anonim kullanıcıda 4. sayfa Login'i açar; giriş sonrası Free
olduğu için Paywall gelir.

### 1.5 Kabul

- Free hesapla ihale listesinde 3 sayfa kaydırılır. 4. sayfada altlık çıkar, hata ekranı
  çıkmaz, yüklenmiş sonuçlar ekranda kalır.
- DT / kurul / firma listelerinde 403 bir kez gelir, kaydırma tekrar istek atmaz.
- Pro hesapta sınır yoktur.
- Backend'de `LISTE_SAYFA_SINIRI_MOBIL_MIN_BUILD` açılmadan önce yeni sürüm
  `X-IT-App-Build` gönderir. Admin → Güvenlik Paneli'nde istekler "mobil/API" olarak
  görünür.

---

## 2. Kişi başı EKAP kotası (bugünkü sürümü de etkiler)

### 2.1 Kural

EKAP'a **canlı** giden istekler kişi başı sayılır. Önbellekten ya da veritabanından dönen
cevap **sayılmaz**:

| | gün | saat |
|---|---|---|
| girişsiz | 0 | 0 |
| yeni hesap (< 24 saat) | 10 | 5 |
| Free | 30 | 15 |
| Pro | 60 | 30 |

Neyin sayıldığı:

- **DT ayrıntısı** (`GET /ekap/dogrudan-temin/<dtn>/`): yalnızca ayrıntısı daha önce hiç
  açılmamış alımda 1 istek. Açılmış alımın ayrıntısı veritabanında kalıcıdır ve sayılmaz.
- **İhale doküman listesi** (`GET /ekap/tenders/<key>/documents/`): ihale başına günde
  bir kez, 2 istek (ihale dokümanı + teknik şartname). 24 saat önbellekte kalır.
- **İndirme** (`…/document/`, DT `…/dokuman/`): her indirmede 2 istek (liste + indir);
  DT dokümanı 1.

Sayılar sunucu ayarıyla değişebilir; uygulamaya yazmayın.

### 2.2 API yanıtları

**DT ayrıntısı ve doküman listesi**: `200` kalır (yanıt şekli korunur). Yeni alan
`hata_kodu` eklendi:

```json
{
  "success": true,
  "data": {
    "dtn": "26DT2200418",
    "detay": null,
    "mesaj": "Bugünkü EKAP sorgu hakkınız doldu. Daha önce açılmış kayıtlar görüntülenmeye devam eder; yeni sorgular için lütfen yarın tekrar deneyin.",
    "hata_kodu": "ekap_quota"
  }
}
```

`hata_kodu` diğer durumlarda `null`'dır. Doküman listesinde (`documents/`) aynı iki alan
`data.mesaj` + `data.hata_kodu` olarak gelir.

**İndirmeler**: **429** + başlık **`X-IT-Hata: ekap_quota`** + aynı mesaj gövdede
(`errors.code = "ekap_quota"`).

Kotası bitmemiş ama ortak bütçesi dolmuş durum bugünkü gibi `503` döner (gövdede
"Bugünkü belge indirme kotası doldu").

### 2.3 Yapılması gerekenler

**a) DT ayrıntısı (şart)**

`DirectProcurementDetail/index.js:41-42` `mesaj`'ı bilinçli olarak göstermiyor
(EKAP'tan söz etmeme kuralı) ve `detay` null olunca "Ayrıntılar şu an alınamadı" +
"Tekrar dene" diyor (`:239-245`). Kota dolduysa tekrar denemek işe yaramaz ve kullanıcı
sistemi bozuk sanar.

- `hata_kodu === "ekap_quota"` ise sabit bir metin gösterin, "Tekrar dene" düğmesi
  olmasın: **"Bugünkü ayrıntı sorgulama hakkınız doldu. Daha önce açtığınız alımlar
  görüntülenmeye devam eder; yarın tekrar deneyebilirsiniz."**
- Kullanıcı Free ise altına "Pro ile günlük hakkınız iki katına çıkar" + Paywall
  bağlantısı.
- Mesaj metni sunucudan gelen `mesaj` da olabilir; ürün kuralınıza göre seçin. Sunucu
  metni EKAP'tan söz ediyor.

**b) İndirmeler (şart)**

İndirmeler `RNFS.downloadFile` ile yapılıyor; gövde okunmuyor, yalnızca durum kodu ve
başlıklar görülüyor:

- DT: `api/v1/directProcurement.js:166-219`
- İhale: `TenderDetail/components/DocumentSheet.js:175`

`429` bugün `DOC_GENERIC_ERROR`'a düşüyor (`directProcurement.js:144-147`,
`DocumentSheet.js:31-36`).

- `statusCode === 429` ve `begin` başlıklarında `x-it-hata === "ekap_quota"` →
  "Bugünkü belge indirme hakkınız doldu; yarın tekrar deneyin."
- Başlık adlarının büyük/küçük harfi platforma göre değişir; mevcut `h()` yardımcısını
  kullanın.
- Bu durumda `DocumentSheet.js:258`'deki "404'te listeyi yenile ve tekrar dene" yoluna
  girilmesin.

**c) Doküman listesi (şart)**

`getTenderDocuments` (`api/v1/api.js:215`) cevabında `hata_kodu === "ekap_quota"` →
listede "Doküman bilgisi alınamadı" yerine kota mesajı gösterilsin.

**d) Girişsiz kullanıcı (bilgi)**

Girişsiz kotası 0 olduğu için girişsiz kullanıcı açılmamış bir DT'nin ayrıntısını ve
dokümanları göremez. Mobilde DT ve dokümanlar zaten giriş istiyor
(`drawerMenu.js:159-183`, `requireAuth('document')`); fiilen değişiklik yok. Girişsiz
bir yoldan bu uçlara gidiliyorsa `hata_kodu` ile Login'e yönlendirin.

### 2.4 Kabul

- Kota dolmuş bir test hesabıyla (admin'den sayaç doldurulabilir) DT ayrıntısında kota
  metni görünür, "Tekrar dene" görünmez.
- İndirmede kota uyarısı çıkar.
- Kotası olan hesapta her şey bugünkü gibi çalışır.

---

## 3. Askıya alınan hesap ve IP engeli (bugünkü sürümü de etkiler)

Admin artık hesabı **askıya alabilir** (24 saat / 7 gün / süresiz) ve IP / ağ
engelleyebilir.

| Durum | Ne gelir | Bugün ne oluyor | İstenen |
|---|---|---|---|
| Askıya alınmış hesabın açık oturumu | Her istekte **401**; token yenileme de 401 | `http.js:66-90` → `clearSession()` → çıkış | Değişiklik gerekmez. |
| Askıdaki hesapla giriş (e-posta, Google, Apple) | **403** `errors.code = "account_suspended"`, mesaj "Hesabınız askıya alındı. Bir hata olduğunu düşünüyorsanız destek@ihaletakip.com adresine yazın." | Genel giriş hatası | `screens/Login/helpers.js` `parseAuthError`'a `account_suspended` ekleyin; mesajı olduğu gibi gösterin, tekrar deneme önermeyin. |
| Engelli IP | Tüm `/api/` isteklerinde **403** `errors.code = "ip_blocked"`, mesaj "Bu ağdan erişim engellendi." | Ekrana göre genel hata | `http.js`'te `ip_blocked` görülünce `AppGateModal` desenindeki tam ekran bir kapı: mesaj + "Destek" bağlantısı. Kapı her istekte yeniden açılmasın; uygulama açılışında bir kez yeterli. |
| Tek kullanımlık e-posta ile kayıt | `POST /auth/email/register/start/` → **400** `disposable_email` | Genel hata | `EmailAuthFlow.js` e-posta alanının altında mesajı gösterin. |

---

## 4. Değişmeyenler

- Liste, detay ve diğer uçların yanıt şekli aynı. Eklenen tek alan DT ayrıntısındaki
  ve `documents/`'teki `hata_kodu` (additive).
- Pro kullanıcı için sayfa sınırı yoktur; EKAP kotası Pro'da da vardır (60/gün).
- Web'in insan doğrulaması (HTTP 428 `human_verification_required`) mobile hiç gelmez.

## 5. Mobil ekipten beklenen

1. §1.3 `X-IT-App-Build` başlığı + build numarasını backend'e iletmek.
2. §1.4 a–b liste davranışları (şart), c–e (önerilir).
3. §2.3 a–c kota mesajları.
4. §3 `account_suspended`, `ip_blocked`, `disposable_email`.

Sürüm mağazaya çıkınca build numarasını iletin; sayfa sınırını backend'de o numarayla
açacağız.
