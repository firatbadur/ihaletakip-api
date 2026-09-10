# EKAP Mobil API — birincil veri kaynağı

**Durum:** EKAP'a **resmî başvuru yapıldı ve bu uç onaylandı.** Toplayıcı
`ekap/mobil/` altında; v2 (`ekapv2.kik.gov.tr`) **yedek** olarak kod düzeyinde
duruyor, beat'te kapalı.

**Neden geçildi:** EKAP 2026-09-08 akşamı web portalına Cloudflare Turnstile insan
doğrulaması koydu; doğrulamanın ömrü ~8 dakika ve sunucudaki otomasyon tarayıcısı
açıkça reddediliyor. Toplama bir kişinin tarayıcı sekmesinin açık kalmasına bağlı
hâle gelmişti (`tools/ekap-cerez-eklentisi/`). Mobil uçta Turnstile yok, imza yok,
kimlik yok — ama **kendi hız sınırı** var (bkz. "Hız sınırı ve CAPTCHA").

---
## Temel bilgiler

| | |
|---|---|
| Kök | `https://ekapmobil.kik.gov.tr/KikMobilServicesApi/api/Mobil/v1/` |
| Metot | Tümü **POST** |
| Sürüm | Her istekte `?api-version=1.0` |
| Kimlik | **Yok** — token, imza, çerez gerekmiyor |
| TLS taklidi | **Gerekmiyor** — düz istemci çalışıyor (v2'nin aksine `curl_cffi` taklidi şart değil) |

### Başlıklar

```
user-agent: EKAP/2.2.0 <cihaz-uuid> iOS 26.6.1 Darwin Kernel Version 25.6.0: ...
accept: */*
accept-language: en-us
accept-encoding: gzip, deflate, br
content-type: application/json; charset=utf-8   ← yalnızca gövdeli istekte (Liste)
content-type: text/plain; charset=utf-8         ← gövdesiz, query-param'lı isteklerde
x-skip-error-dialog: true                       ← bazı uçlarda (şartname/doküman)
```

⚠️ **`user-agent` içinde bir cihaz UUID'si var.** Uygulama kurulumunda üretiliyor.
Ölçüldü: hız sınırı bu kimliğe değil **IP'ye** bağlı (aynı UUID ile bizim sunucu
engellenmişken kullanıcının telefonu sorunsuz çalışıyordu).

### F5 ASM çerezi

Yanıtlar `Set-Cookie: TS015c8da3=...; Domain=.ekapmobil.kik.gov.tr` döndürüyor
(web tarafındaki `TS01725090`'ın karşılığı). Gerçek uygulama bu çerezi taşıyor;
her istekte sıfırdan bağlanan bir istemci ASM gözünde bot imzası verir.

---

## Uçlar

### 1. İhale listesi — keşif

```
POST /IhaleArama/Liste?api-version=1.0
Content-Type: application/json
```

Gövde (uygulamanın gönderdiğinin birebir aynısı):

```json
{
  "iknYili": 0, "iknSayi": 0, "ihaleTuru": -1,
  "ihaleTarihiBaslangic": "2026-09-09 00:00:00", "ihaleTarihiBitis": "",
  "ilanTarihiBaslangic": "", "ilanTarihiBitis": "",
  "ilKod": 0, "icerik": "",
  "ihaleBilgiSecim": 0, "ihaleBilgiOpsiyon": 0,
  "aramaTuru": 2, "ihaleUsulu": 0, "ihaleDurumu": 0,
  "orderBy": "IKN Desc", "yasaKapsam": 1
}
```

**Yanıt:** düz liste (sarmalayıcı yok). Alanlar:

```
ikn, ihaleAdi, idareAdi, idareIlAdi, ihaleTarihi ("07.10.2026 11:00"), ihaleTipi (int)
bagliOlduguEnUstIdare, bagliOlduguIdare, ihaleDurumu, ihaleKapsamTurUsul,
ihaleYer, isinYeri, onayTarihi, ilanSekli, ilanHtml, idariSartnameHtml,
ihaleIlani, duzeltmeIlanlari, iptalIlani, onIlan, sonucIlanlari, ilan2015SonrasiMi
```

⚠️ **Listede yalnızca ilk altı alan doludur**; kalanlar `null`/`[]` gelir.
`ihaleBilgiSecim` / `ihaleBilgiOpsiyon` değerleri (0-3 denendi) bunu **değiştirmiyor**.

#### Parametre davranışı (ölçüldü)

| Parametre | Durum |
|---|---|
| `ihaleTarihiBaslangic` / `Bitis` | ✅ **çalışıyor** — geçmiş tarihler dahil |
| `ilKod` | ✅ çalışıyor — ⚠️ **PLAKA** (1-81), `City.ekap_il_id` DEĞİL (ölçüldü 2026-09-10: `ilKod=6` → ANKARA, `ilKod=251` → 0 kayıt) |
| `ihaleTuru` (1 Mal, 2 Yapım, 3 Hizmet, 4 Danışmanlık) | ✅ çalışıyor |
| `icerik` (metin arama) | ✅ çalışıyor |
| `yasaKapsam` (0/1/2) | ✅ çalışıyor — farklı evrenler döndürüyor |
| `aramaTuru` | 1 ve 2 çalışıyor, 0 boş, 3 → HTTP 404 |
| `ilanTarihiBaslangic` / `Bitis` | ❌ **yok sayılıyor** |
| `orderBy` ("IKN Asc"/"Desc") | ❌ yok sayılıyor |
| `iknYili` / `iknSayi` | ❌ yok sayılıyor (tekil ihale için detay ucunu kullan) |
| `ihaleBilgiSecim` / `ihaleBilgiOpsiyon` | ❌ etkisi gözlenmedi |

#### ⚠️ 250 kayıt tavanı

Her istek **en fazla 250 kayıt** döndürür; sayfalama parametresi yoktur.
Aralık ne kadar geniş olursa olsun kesilir (2024 tüm yıl → 250).

**Aşma yöntemi: dilimleme.** Ölçüm (2026-09-14, tavana takılan gün):

| Sorgu | Kayıt |
|---|---|
| Tek gün, filtresiz | 250 (tavan) |
| Aynı gün, `ihaleTuru=1` | 106 |
| Aynı gün, `ihaleTuru=2` | 89 |
| Aynı gün, `ihaleTuru=3` | 61 |
| Aynı gün, `ihaleTuru=4` | 2 |
| **Türe bölünmüş tekil** | **258** |
| DB'deki gerçek sayı | 261 |

Yetmezse `ilKod` (81 il) ile ikinci kırılım mümkün.

#### Kapsam doğrulaması

15 günlük pencere gün gün tarandı → **2324 tekil İKN**, hepsinin DB'de karşılığı
var, **eksik 0**. Yani mobil API'nin evreni v2 ile örtüşüyor.

Arşiv sınırı **yok**: 2019 haftası mobilde 4, DB'de 5 kayıt (o hafta gerçekten seyrek).

---

### 2. İhale detayı

```
POST /IhaleArama/Ihale?iknYili=2026&iknSayi=1690784&api-version=1.0
Content-Type: text/plain    (gövde boş)
```

**Dolu gelen alanlar:**

| Alan | Örnek / açıklama |
|---|---|
| `ihaleIlani` | sözlük: `{ilanHtml, ilanTarihi, ilanTipi, ilanXml}` |
| **`ihaleIlani.ilanTarihi`** | `"08.09.2026 00:00:00"` → **`Tender.ilan_tarihi`** kaynağı |
| `ihaleDurumu` | metin: `"Sonuç İlanı Yayımlanmış"`, `"İhale İlanı Yayımlanmış/İlansız, Katılıma Açık"` |
| `ihaleKapsamTurUsul` | `"4734 Kapsamında - Mal - Açık"` / `"İstisna - Hizmet - 4734 / 3-g"` |
| `ihaleYer`, `isinYeri`, `onayTarihi` | metin |
| `ilanHtml` | İhale İlanı tam HTML |
| `idariSartnameHtml` | İdari şartname tam HTML (~120 KB) |
| `bagliOlduguEnUstIdare`, `bagliOlduguIdare` | idare hiyerarşisi **adları** (id değil) |

**Hep boş gelenler:** `sonucIlanlari`, `duzeltmeIlanlari`, `iptalIlani`, `onIlan`.
⚠️ `ihaleDurumu = "Sonuç İlanı Yayımlanmış"` olan **4 farklı ihalede de**
`sonucIlanlari` boş döndü — sonuç ilanı için ayrı uç kullanılmalı (aşağıda).

⚠️ `ihaleIlani.ilanXml` **boş** geliyor (İhale İlanı için); Sonuç İlanı ucunda ise dolu.

---

### 3. Sonuç ilanları — **para zincirinin kaynağı**

```
POST /IhaleArama/SonucIlanlari/Ilan?iknYili=2025&iknSayi=1367690&pageSize=10&pageNum=1&api-version=1.0
```

**Yanıt:** liste, her eleman `{ilanHtml, ilanTarihi, ilanTipi: 4, ilanXml}`.

⚠️ **Kısım/kazanan başına bir kayıt döner.** Örnek (2025/1367690): iki sonuç ilanı,
iki farklı kazanan ve bedel (AHMET ÖZTÜRK 235.888 / YUNUS ERDOĞAN 269.376).
Sözleşme tablosu için doğru granülerlik budur.

#### `ilanXml` etiketleri (tam liste)

```
SonucIlan
├── IdareAdi, IsinAdi, IhaleAdi, IhaleKayitNo
├── IhaleTarih, IhaleUsul, IhaleUsulAltMadde
├── eMunferitMi, PazarlikMi, PazarlikBcfMi, PazarlikBcfGerekce
├── IhaleYaklasikMaliyet          ← temiz ondalık, ör. 984646.56
├── IhaleKisimYMGosterilsinMi     ← kısım maliyeti gösteriliyor mu (bayrak)
├── IhaleKisimYaklasikMaliyet
├── TeslimYeri, IsinSuresi
├── EImzaliIndirenSayisi
├── ToplamTeklifSayisi, ToplamGecereliTeklifSayisi
├── Avantaj
├── SozlesmeTarih, SozlesmeBedel, SozlesmeSuresi
├── SozlesmeBedelDovizKontrol, SozlesmeBedelFarkliParaBirimi
├── IhaleKazanan                  ← kazanan firma ünvanı
├── YukleniciUyruk, IstekliAdres
└── StaAcikMi
```

#### ⚠️ Doğrulama: para verisi DB ile birebir tutuyor

| İKN | mobil YM | DB YM | mobil bedel | DB bedel |
|---|---|---|---|---|
| 2026/1542784 | 17649215.03 | 17649215.03 | 16500000 | 16500000.00 |
| 2026/1545640 | 6197796.69 | 6197796.69 | 93340 | 93340.00 |
| 2026/1510611 | 1645519.68 | 1645519.68 | 543056.25 | 543056.25 |
| 2026/1302191 | 57780112.77 | 57780112.77 | 52000000.00 | 52000000.00 |
| 2026/1512307 | 12470400 | 12470400.00 | 12294000.00 | 12294000.00 |

⚠️ **XML, v2'nin string alanından DAHA GÜVENİLİR.** `CLAUDE.md`'de belgelenen
v2 `yaklasikMaliyet` bozulması (ondalık noktası silinip 10×/100× şişme) burada
**yok** — değerler temiz ondalık geliyor.

⚠️ `IhaleKisimYMGosterilsinMi` bayrağı, bugünkü `sync._kisim_maliyeti_belirsiz`
sezgisinin **yerine geçti**: tahmin yerine kaynağın kendi beyanı
(`sync._kisim_maliyeti_belirsiz(..., beyan=...)`). Sezgi silinmedi — beyan gelmeyen
(v2) kayıtlarda yedek olarak duruyor.

✅ **Doğrulandı (2026-09-10, İKN 2024/1362677, 13 sonuç ilanı):** mobil, kısım
başına **ayrı ve doğru** yaklaşık maliyet veriyor (2.220.750 · 2.808.581,44 ·
17.480.803,85 …) ve ihale toplamını (65.596.282,36) ayrı alanda tutuyor. Bu
listede v2'nin bozduğu değer de var: `11454672.76` — CLAUDE.md'de belgelenen
100× şişme örneğinin ta kendisi — mobilde **temiz** geliyor.

✅ **Mevcut `parse_sonuc_ilani` mobil `ilanHtml` üzerinde birebir çalışıyor**
(aynı 13 ilanda kısım YM, ihale YM, bedel, teklif sayıları, uyruk/adres/il aynı) →
para için ikinci bir ayrıştırıcı YAZILMADI (tek çıkarım kaynağı kuralı). XML'den
yalnızca HTML'de olmayan iki şey alınır: `IhaleKazanan` (HTML ayrıştırıcısı ünvanı
kesiyor → kesilmiş ad mükerrer firma doğurur) ve kısım YM beyanı.

---

### 4. Doküman uçları

```
POST /IhaleArama/IhaleDokumani/Liste?iknYili=&iknSayi=&api-version=1.0
→ {"dokumanlar": [{"aciklama": "İhale Dokümanı", "boyut": 0,
                   "dosyaAdi": "{...}_{0}_{202609041600}_ihale_dokumani_2026_1690784.zip",
                   "id": "<64 hex>", "tarih": "04.09.2026 16:00:05"}],
   "sonuc": {...}}

POST /IhaleArama/IhaleDokumani/Indir?iknYili=&iknSayi=&dosyaId=<id>&api-version=1.0
→ application/octet-stream (gerçek ZIP; doğrulandı: 61.803 bayt, PK sihirli baytı)
   ekap-result: true
   content-disposition: attachment; filename="..."

POST /IhaleArama/TeknikSartname/Bilgiler?iknYili=&iknSayi=&api-version=1.0
→ [{"boyut": 2137543, "dosyaAdi": "...teknik şartname.pdf", "dosyaId": 30828013, "icerik": null}]

POST /IhaleArama/IdariSartname/Bilgiler?iknYili=&iknSayi=&api-version=1.0
→ {"mesaj": "", "resultCode": 0, "sonuc": true,
   "uniqueName": "{...}_{4}_{}_idari_sartname_2026_1690784.html"}
```

⚠️ `IhaleDokumani/Liste` dönen `id` **her çağrıda değişiyor** (tek kullanımlık).
Liste ve indirme **aynı istek zincirinde** yapılmalı; id önbelleklenemez.

---

### 5. Captcha uçları

⚠️ Bu uçlar **`Captcha/`** kökündedir, `IhaleArama/` altında **değil**.

Akış:

```
herhangi bir sorgu  →  HTTP 300 "CAPTCHA_REQUIRED"
                          ↓
                    POST /Captcha/Getir      → captchaId + captchaImage (base64 PNG)
                          ↓
                    (cevap üretilir)
                          ↓
                    POST /Captcha/Sonuc      → {"success": true}
                          ↓              ↘ success:false → baştan (yeni Getir)
                    sorgular kaldığı yerden devam eder
```

#### 5.1 `Captcha/Getir` — captcha üret

```
POST /KikMobilServicesApi/api/Mobil/v1/Captcha/Getir?api-version=1.0
```

Yanıt:

```json
{
  "captchaRequired": true,
  "captchaImage": "iVBORw0KGgoAAAANSUhEUgAAALQAAAA8CAYAAADPLpCHAAAAAXNSR0IArs4c6QAA
                   AARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAPRSURBVHhe7ZjR
                   leMgDEXTl5txK27EfaSOFOS1zxxPHPMkJBvYjHgf92s8IMRFiDxer9dCSBQoNAkF
                   hSahoNAkFBSahIJCk1BQaBIKCk1CQaFJKCg0CQWFJqGg0CQUFJqEgkKTUFBoEgoK
                   TUJBoUkoKDQJBYUmoaDQJBSv5R8Gq4+axngtTAAAAABJRU5ErkJggg==",
  "captchaId": "822c775652584936bdcbe71df54dc4f4"
}
```

| Alan | Açıklama |
|---|---|
| `captchaRequired` | bool |
| `captchaImage` | **base64 PNG**, `data:` öneki yok — doğrudan `base64.b64decode()` |
| `captchaId` | 32 hex karakter; `Sonuc` çağrısında bu id gönderilir |

Gözlemlenen resim boyutu: **180 × 60 px** (PNG başlığından: `0xB4 × 0x3C`).
Örnek cevap metni: `HagB21` — 6 karakter, harf + rakam, büyük/küçük karışık.

⚠️ Bu ucun **isteği yakalanmadı**; yalnızca yanıtı elimizde. Gövde muhtemelen boş,
diğer gövdesiz uçlar gibi `content-type: text/plain; charset=utf-8` ile.
Captcha hatası ile karşılaşınca Captcha base64 image ekap/tools/ocr ile çözülerek sonuç apisine iletilecek. Bu kik.gov.tr tarafından önerilen kullanımdır.

#### 5.2 `Captcha/Sonuc` — cevabı doğrula

```
POST /KikMobilServicesApi/api/Mobil/v1/Captcha/Sonuc?api-version=1.0
user-agent: EKAP/2.2.0 58cf399f-a709-45f0-9f39-3df12b2936b4 iOS 26.6.1 Darwin Kernel Version 25.6.0: ...
connection: keep-alive
accept: */*
accept-language: en-us
accept-encoding: gzip, deflate, br
content-type: application/json; charset=utf-8
content-length: 73
host: ekapmobil.kik.gov.tr
```

Gövde:

```json
{
  "captchaId": "44db9b9c7d664b0b9c5ee8c65350f2c3",
  "captchaAnswer": "HagB21"
}
```

Yanıt (HTTP 200):

```json
{ "success": true, "message": "Captcha doğrulandı" }
```

```
Set-Cookie: TS015c8da3=01af917a164a4776ba299d046bf207c2fd83378701649642c91e36a985604aefb...
            Path=/; Domain=.ekapmobil.kik.gov.tr
api-supported-versions: 1.0
```

⚠️ **`Sonuc` yanıtı da ASM çerezini tazeliyor.** Doğrulama sonrası isteklerin
aynı oturumla (aynı çerez taşınarak) devam etmesi gerekir; yeni bir oturum açmak
doğrulamayı boşa çıkarır.

⚠️ `success: false` gelirse **aynı `captchaId` tekrar denenmez** — yeni bir
`Getir` çağrısıyla yeni id/resim alınır.

#### 5.3 Çözüm politikası: OCR + insan yedeği

⚠️ Bu bölüm **2026-09-10'da değişti.** Önceki sürüm "captcha otomatikleştirilmemeli"
diyordu; o karar, uca **izinsiz** eriştiğimiz döneme aitti. EKAP'a resmî başvuru
yapıldı ve bu kullanım onaylandı → captcha artık bir "karşıda insan var mı" testi
değil, onaylı bir istemci için **hız sınırı kapısıdır**. KİK'in önerdiği kullanım
biçimi de budur.

Akış (`ekap/mobil/captcha.py`):

1. `Captcha/Getir` → base64 PNG
2. `ekap/tools/ocr.py` (Tesseract, yerel, ücretsiz, token harcamaz) ile çözülür
3. `Captcha/Sonuc` → `success:true` ise istek tekrarlanır
4. `EKAP_MOBIL_CAPTCHA_DENEME` kez tutmazsa **toplama durur**: resim admin ekranına
   düşer, operatör yazar (`manage.py mobil_captcha --cevap XXXXXX`) ve üstel geri
   çekilme (taban 30 dk) devreye girer.

⚠️ **Sistem hiçbir koşulda sessizce durmaz** — ya çözer ya operatöre sorar.

**OCR parametreleri ölçümle seçildi** (8 etiketli örnek × 12 kombinasyon):

| psm | buyut | isabet |
|---|---|---|
| **7** | **2** | **7/8** |
| 6 | 2 | 7/8 |
| 8 / 13 | 2 | 6/8 |
| 7 | 3-4 | 6/8 |

⚠️ **Büyütmek burada KÖTÜLEŞTİRİYOR.** `tools/ocr.py`'nin genel kuralı (küçük görseli
büyüt) bu captcha'da geçersiz: harfler zaten ~40 px ve temiz; 3-4× büyütme kenarları
yumuşatıp `s→S`, `D→J` karıştırıyor.
⚠️ **Cevap uzunluğu SABİT DEĞİL** (6-9 karakter gözlendi: `ExbT61`, `LhvHd20`,
`sKsXPP18`). İlk sürüm `== 6` kontrolü yapıyor ve doğru okunan cevapları atıyordu →
isabet %87'den %50'ye düşüyordu.
⚠️ Tek başarısız örnek, metni **canvas'a sığmayıp kenardan kırpılmış** olandı; onu
OCR'ı zorlayarak değil, yeni captcha isteyerek çözersiniz.

---

### Denenip bulunamayan uçlar (hepsi HTTP 404)

`IhaleArama` altında **yalnızca `Ihale`, `SonucIlanlari/Ilan`, `IhaleDokumani/*`,
`TeknikSartname/Bilgiler`, `IdariSartname/Bilgiler`, `Liste`** var. Denenenler:

```
Sonuc, SonucIlani, SonucIlanlari (tek başına), IhaleSonucu, IhaleSonuc,
Sozlesme, SozlesmeBilgileri, Sozlesmeler, Kisim, Kisimlar, Teklif, Teklifler,
Istekli, Istekliler, DuzeltmeIlani, DuzeltmeIlanlari/Ilan, IptalIlani,
IptalIlanlari/Ilan, OnIlan, OnIlanlar/Ilan, OnIlanlari/Ilan, Okas, Okas/Liste,
IhtiyacKalemleri, IhtiyacKalemleri/Liste, Kalemler, YaklasikMaliyet,
Ilan, Ilanlar, Ilanlar/Ilan, IhaleIlanlari/Ilan, Kisimlar/Liste
```

Düzeltme/iptal ilanları için uç **bulunamadı**; uygulamada o ekranlar açılıp
istek yakalanırsa harita tamamlanır.

---

## Hız sınırı ve CAPTCHA

Mobil API **korumasız değil.** Eşik aşılınca:

```
HTTP 300
CAPTCHA_REQUIRED        (gövde tam olarak bu, 16 bayt)
```

⚠️ **HTTP 300 alışılmadık bir koddur** (standартta "Multiple Choices"). İstemci
kütüphaneleri bunu hata saymaz; `r.status_code == 200` kontrolü yapan kod sessizce
boş veriyle devam eder. Kontrol **gövdeye** de bakmalı: `"CAPTCHA" in r.text`.

Engel kalkması için `Captcha/Getir` + `Captcha/Sonuc` akışı (bkz. 5. bölüm) ya da
yeterince uzun sessizlik gerekir.

### Ölçümler

| Tempo | Sonuç |
|---|---|
| ~50 istek/dk (1,2 sn aralık) | ~100 istekten sonra engel |
| 12 istek / 5 dk (**2,4 istek/dk**), kalıcı oturum | 12/12 engel |
| Tek istek, uzun sessizlikten sonra | ✅ 200 |
| 4 dk soğuma → tek istek | ❌ hâlâ engel |
| 30 dk soğuma → tek istek | ✅ 200 |
| ~40 dk soğuma → tek istek | ✅ 200 |

**Çıkarımlar:**

- Sınır **IP tabanlı** (kullanıcının telefonu aynı `user-agent` ile etkilenmedi)
- **Yuvarlanan pencere** gibi davranıyor: sessizlikten sonra açılıyor, sürdürülen
  tempoda hızla kapanıyor
- Kritik eşik **2,4 istek/dk'nın altında**; tam değer ölçülmedi
- Engel kalıcı değil, sıfırlanma 4 dk ile 30 dk arasında

⚠️ **Eşiği aramak için tekrar tekrar engellenmek yapılmadı** — tekrarlanan ihlal
cezayı büyütebilir ve **aynı sunucu IP'si** v2/tarayıcı köprüsü için de kullanılıyor;
çalışan yolu riske atmaya değmez.

### Üretim için tempo hesabı

| İş | Günlük istek |
|---|---|
| Keşif (gün × tür dilimleri) | ~10 |
| Yeni ihale detayı (~300 yeni/gün) | ~300 |
| Yeni sonuçlananların sonuç ilanı | ~300-700 |
| Belge indirme (kullanıcı tetikli) | değişken |

Toplam **~600-1000 istek/gün** ≈ 0,4-0,7 istek/dk. Ölçülen engel eşiğinin
(2,4/dk) altında ama **aynı büyüklük mertebesinde** → güvenli tarafta kalmak için
**2-3 dakikada bir, sapmalı** tempo öneriliyor.

⚠️ **Arşiv backfill'i mobil API'den yapılamaz** (1M ihale, bu tempoda yıllar).
Gerek de yok: arşiv v2'den toplanmış durumda; mobil API'nin işi **ileri akış**.

---

## v2 ile karşılaştırma: neyi kurtarıyor, neyi kurtarmıyor

### ✅ Mobil API'den alınabilenler

| Veri | Kaynak |
|---|---|
| Yeni ihale keşfi | `Liste` (gün × tür dilimli) |
| `ilan_tarihi` — **bildirimlerin tamamı buna bağlı** | `Ihale` → `ihaleIlani.ilanTarihi` |
| Durum, kapsam/tür/usul, ihale yeri, iş yeri, onay tarihi | `Ihale` |
| Yaklaşık maliyet, sözleşme bedeli/tarihi/süresi, kazanan firma | `SonucIlanlari/Ilan` → `ilanXml` |
| Teklif sayısı, geçerli teklif sayısı, e-imzalı indiren sayısı | aynı XML |
| Belgeler (ihale dokümanı, teknik/idari şartname) | doküman uçları |
| **OKAS kalemleri** | `idariSartnameHtml` içinden — bkz. aşağısı |

#### ⚠️ OKAS: idari şartnameden çıkarılabiliyor

Yöntem: `idariSartnameHtml` içindeki 8-9 haneli sayılar, DB'deki OKAS kataloğuyla
(`OkasCode.kod`, 9590 kod) kesiştirilir.

**Ölçüm — 25 ihale (2026, OKAS kalemi DB'de kayıtlı olanlar):**

| Metrik | Sonuç |
|---|---|
| Kesinlik (precision) | **%100** (35/35 — tek yanlış kod yok) |
| Duyarlılık (recall) | **%97,2** (35/36) |
| Tam yakalanan ihale | 24/25 |
| **Kaynak** | **24/24 idari şartnamede** — ilan HTML'inde değil |

Katalogla kesiştirme yanlış pozitifi sıfırlıyor.

### ❌ Mobil API'de olmayanlar

| Veri | Etkilediği özellikler |
|---|---|
| **`idare_id`** (yalnızca idare **adı** var) | Favori idare bildirimi, idare profili, DETSIS ağacıyla filtreleme, `seri_anahtar` |
| Düzeltme / iptal ilanları | Alarm: "doküman güncellendi", "iptal edildi" |
| `il_id` (yalnızca il **adı** var) | `City` tablosuyla eşlenebilir — sorun değil |

#### ⚠️ `idare_id` ad eşleştirmesi REDDEDİLDİ

`Tender.idare_adi` → `Authority.ad_norm` → `idare_id` eşleştirmesi 2000 ihalede
ölçüldü:

| Sonuç | Oran |
|---|---|
| Doğru eşleşti | %15,3 |
| **YANLIŞ eşleşti** | **%11,7** |
| Eşleşmedi | %71,0 |
| Belirsiz (aynı ad, birden çok idare) | %1,9 |

Yanlış `idare_id` ihaleyi yanlış kuruma bağlar ve kullanıcı bunu fark edemez.
**Eksik veri, yanlış veriden iyidir** — `indirim_orani` kararıyla aynı ilke.

Mobil `idareAdi` ile aynı test yapılamadı (araya CAPTCHA girdi), ama liste
ucundaki ad `Tender.idare_adi` ile aynı kaynaktan geldiği için sonucun
belirgin biçimde farklı çıkması beklenmiyor.

#### ⚠️ `idare_id` avı: HİÇBİR UÇTA YOK (ölçüldü 2026-09-10)

DB'de sayısal `idare_id`si bilinen 4 ihale için tüm mobil uçlar çağrıldı ve ham
yanıtın **her yerinde** o değer arandı (`manage.py mobil_probe --is idare`):

| Uç | Sonuç |
|---|---|
| `IhaleArama/Ihale` (kökler + `ilanHtml` + `idariSartnameHtml`) | ❌ yok |
| `IhaleDokumani/Liste` | ❌ yok |
| `IdariSartname/Bilgiler` | ❌ yok |
| `SonucIlanlari/Ilan` → `ilanXml` | ❌ yok (`IdareAdi` var, id yok) |

⚠️ Umut vaat eden iki iz **yanlış çıktı**: doküman adlarındaki `{76DC25C0…}` blokları
**belge GUID'i** (her belgede farklı), şartname ve ilanda ortak geçen `3231941` ise
idarenin **telefon numarası**.

→ **Karar:** `idare_id` mobil kayıtlarda **boş bırakılır**
(`ekap/mobil/adapt.detaydan` bu anahtarı hiç koymaz). Ad eşleştirmesi reddedildi
(yukarıdaki %11,7 yanlış eşleşme). "Eksik veri, yanlış veriden iyidir" —
`indirim_orani` kararıyla aynı ilke. Etkilenen özellikler: favori idare bildirimi,
idare profili, DETSIS ağacıyla filtreleme, `seri_anahtar`. Bunlar v2 çerezi geçerli
olduğu anlarda dar bir "detay tamamlama" turuyla doldurulabilir.

---

## Yan bulgu: `Tender.idare_id` biçim değişimi

Mobil API'den bağımsız, **mevcut sistemde** bir sorun. EKAP `idare_id` alanını
sayısaldan 64 karakterlik hash'e çevirmeye başlamış:

| Yıl | Toplam | hash64 | sayısal |
|---|---|---|---|
| 2026 | 55.052 | 1.974 (**%3,6**) | %96,4 |
| 2025 | 93.240 | 524 (%0,6) | %99,4 |
| 2024 | 92.770 | 13 (%0,0) | %100 |
| 2022 | 118.384 | 4 (%0,0) | %100 |
| 2019 | 85.255 | 0 | %100 |

`Authority.idare_id` ise **tamamen sayısal** (87.964 satır, hash yok).

⚠️ **Sonuç:** hash biçimli `idare_id` taşıyan ihaleler DETSIS ağacıyla
eşleşemez → favori idare bildirimi ve idare profili o ihalelerde çalışmaz.
Oran küçük ama **büyüyor**.

⚠️ Ayrıca: 2026'nın son 3000 ihalesindeki 2408 tekil `idare_id`'nin yalnızca
**1032'si (%43)** `Authority` tablosunda var — DETSIS ağacımız ihalelerdeki
idarelerin yarısından azını kapsıyor.

---



## Sözlük: `ihaleTipi`

| Değer | Anlam |
|---|---|
| 1 | Mal |
| 2 | Yapım |
| 3 | Hizmet |
| 4 | Danışmanlık |
