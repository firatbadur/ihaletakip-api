# EKAP Mobil API — keşif notları

**Keşif tarihi:** 2026-09-09 / 2026-09-10
**Durum:** Araştırma tamamlandı, **kod yazılmadı**. Üretim hâlâ v2 + tarayıcı köprüsü ile çalışıyor.

## Neden araştırıldı

EKAP 2026-09-08 akşamı web portalına (`ekapv2.kik.gov.tr`) **Cloudflare Turnstile**
insan doğrulaması koydu; doğrulamanın ömrü ~8 dakika ve sunucudaki otomasyon
tarayıcısı Cloudflare tarafından açıkça reddediliyor. Bunun üzerine EKAP'ın **mobil
uygulamasının** kullandığı ayrı API keşfedildi: `ekapmobil.kik.gov.tr`.

Bu API'de **Turnstile yok, imza yok, kimlik doğrulama yok.** Ama kendi **hız
sınırı** var (bkz. "Hız sınırı ve CAPTCHA").

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
| `ilKod` | ✅ çalışıyor |
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
sezgisinin yerine geçebilir: tahmin yerine kaynağın kendi beyanı.

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

Hız sınırına takılınca kullanılıyor (bkz. aşağısı).

```
POST /Captcha/Getir?api-version=1.0
→ {"captchaRequired": true,
   "captchaImage": "<base64 PNG>",
   "captchaId": "822c775652584936bdcbe71df54dc4f4"}

POST /Captcha/Sonuc?api-version=1.0
Content-Type: application/json
{"captchaId": "...", "captchaAnswer": "HagB21"}
→ {"success": true, "message": "Captcha doğrulandı"}
```

`success:false` gelirse yeni captcha alınıp tekrar denenir. Doğrulandıktan sonra
sorgular kaldığı yerden devam ediyor.

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

## Tasarım çıkarımları (kod yazılmadı)

Mobil API üzerine bir toplayıcı kurulacaksa:

1. **Tek kalıcı oturum** — ASM çerezi (`TS015c8da3`) her istekte geri gönderilmeli.
   Her çalıştırmada sıfırdan bağlanmak bot imzasıdır.
2. **2-3 dakikada bir, sapmalı tempo.** Sabit aralık da bir imzadır. Patlama yasak;
   tek tüketici, paralel worker yok.
3. **`HTTP 300 CAPTCHA_REQUIRED` ayrı ele alınmalı** — retry yok (406 deseninin
   aynısı): üstel geri çekilme (30 dk taban), bayrak, panoda görünürlük.
   Çözüm insan-döngüde: `Captcha/Getir` resmi admin'de gösterilir, bir kişi yazar,
   `Captcha/Sonuc`'a gider.
4. **İki kaynaklı mimari zorunlu** — mobil ve v2 aynı `Tender` tablosuna yazar,
   biri kapanınca diğeri devralır. Tek kaynağa yaslanmak 2026-09-08 krizinin
   tekrarı demektir.
5. **`_LISTE_EZMEZ` kuralı burada da geçerli** — mobil liste `ilan_tarihi`/`il_id`
   vermiyor; koşulsuz yazılırsa detaydan gelen değerleri NULL'lar
   (`CLAUDE.md`'de belgelenen üretim arızası).
6. Dedup anahtarı yine **İKN**.

## Sözlük: `ihaleTipi`

| Değer | Anlam |
|---|---|
| 1 | Mal |
| 2 | Yapım |
| 3 | Hizmet |
| 4 | Danışmanlık |
