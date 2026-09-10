# Mobil Entegrasyon — İhale Dokümanı ve Teknik Şartname

Bu doküman, ihale detayındaki **"Doküman İndir"** akışının yeni hâlini anlatır.
Akış 2026-09-10'da değişti; mevcut `DocumentWebView` kurgusu **tek başına yetmiyor**.

> Taban URL: `https://ihale-takip.envisoft.com.tr/api/v1/`
> Kaynak: view'lardaki `@extend_schema`; `docs/openapi.yaml` ve
> `docs/postman_collection.json` her zaman günceldir.

---

## 0. Neden değişti

Eski akış EKAP'ın web servisinden (`ekapv2`) kısa ömürlü bir **belge sayfası adresi**
alıyordu; uygulama onu WebView'de açıyor, kullanıcı gerekirse captcha'yı çözüyor ve
"İndir" butonu `stream.kik.gov.tr`ye yönlenince uygulama dosyayı yakalayıp indiriyordu.

EKAP o servise **Cloudflare Turnstile insan doğrulaması** koydu: adresi alan istek
(bizim sunucumuzdan çıkan istek) artık `HTTP 428` alıyor. Doğrulama çerezi bir insan
tarafından tazelenmediği sürece **hiçbir ihalenin** belgesi indirilemiyordu.

Çözüm: belgeler artık EKAP'ın **mobil API'sinden** alınıyor. Orada kalıcı bir "belge
adresi" kavramı yok — dosya doğrudan bayt olarak veriliyor. Bu yüzden sunucu dosyayı
**akıtarak (streaming proxy)** veriyor.

⚠️ Eski yol **silinmedi**: doğrulama çerezi geçerliyse ve ihalenin gerçek EKAP `id`'si
varsa sunucu yine eski adresi döndürür (aşağıdaki `proxy: false`). Uygulama **iki modu
da** desteklemeli.

---

## 1. ⚠️ Önce sınırlar — UI tasarlamadan okuyun

| Gerçek | Sonucu |
|---|---|
| **Teknik şartname, ihale dokümanı ZIP'inin içinde DEĞİL** | Ayrı bir liste olarak gösterilmeli |
| Teknik şartname **tek dosya değil** — bir ihalede 10-11 dosya olabiliyor | Tek buton yetmez, liste gerekiyor |
| Ölçülen boyutlar: 0,05 · 0,27 · 1,25 · 3,96 · 9,32 · 9,83 ve **951 MB** | **Boyut mutlaka gösterilmeli**; kullanıcı mobil veriyle 1 GB indirmeye zorlanmamalı |
| Bazı ihalelerde teknik şartname **hiç yok** | Liste boş gelebilir, bu hata değildir |
| EKAP mobil API'nin IP tabanlı hız sınırı var | Nadiren `503` gelebilir; "tekrar deneyin" denmeli, hata ekranı basılmamalı |

---

## 2. Uçlar

### 2.1 `GET /ekap/tenders/{key}/document-url/` — giriş noktası (mevcut uç)

`key` = ihale detayında kullandığınız `ekap_id`.

**A) Eski mod (`proxy: false`)** — EKAP'ın kendi adresi döner:

```json
{ "success": true, "data": { "url": "https://ekapv2.kik.gov.tr/...", "proxy": false } }
```

→ **Mevcut `DocumentWebView` akışını aynen kullanın.** Değişiklik yok.

**B) Yeni mod (`proxy: true`)** — bizim adresimiz döner:

```json
{
  "success": true,
  "data": {
    "url": "https://ihale-takip.envisoft.com.tr/api/v1/ekap/tenders/mobil:2026-1711408/document/",
    "documents_url": "https://ihale-takip.envisoft.com.tr/api/v1/ekap/tenders/mobil:2026-1711408/documents/",
    "proxy": true
  }
}
```

→ ⚠️ **WebView AÇMAYIN.** `url` düz bir dosya adresidir: captcha yok, oturum yok, tek
kullanımlık token yok. Doğrudan `RNFS.downloadFile` ile inin.
→ Teknik şartname için `documents_url`'i çağırın (§2.2).

⚠️ **`proxy` bayrağına bakın, URL'e değil.** İleride adres yapısı değişebilir.

⚠️ `proxy: true` adresini WebView'e verirseniz *çalışır* ama dosya **iki kez** iner
(WebView bir kez, sonra `binary_content` yedeği bir kez daha) — kullanıcı beklemede
kalır ve sunucu boşuna iki kat trafik taşır.

### 2.2 `GET /ekap/tenders/{key}/documents/` — dosya listesi

```json
{
  "success": true,
  "data": {
    "ihale_dokumani": {
      "ad": "İhale Dokümanı",
      "tur": "ihale",
      "url": ".../tenders/mobil:2026-1711408/document/"
    },
    "teknik_sartnameler": [
      { "ad": "TEMİZLİK MALZEMELERİ.docx", "boyut": 48632, "dosya_id": 30882363,
        "tur": "teknik", "url": ".../document/?tur=teknik&dosyaId=30882363" },
      { "ad": "Tatlı ve Unlu Mamüller.docx", "boyut": 23522, "dosya_id": 30882403,
        "tur": "teknik", "url": ".../document/?tur=teknik&dosyaId=30882403" }
    ]
  }
}
```

- `boyut` **bayt** cinsindendir; `null` gelebilir (EKAP her dosya için vermiyor).
- `teknik_sartnameler` **boş dizi** olabilir → "Bu ihalede ayrı teknik şartname yok".
- `ad` zaten temizlenmiştir; EKAP'ın `{GUID}_{2}_{}_` önekini siz ayıklamayın.
- Sunucu bu listeyi **24 saat önbelleğe alır** → ekranı her açtığınızda çağırmanız
  sorun değil, EKAP'a gitmez.

### 2.3 `GET /ekap/tenders/{key}/document/` — indirme

| Sorgu | Ne iner |
|---|---|
| (parametresiz) | İhale dokümanı — **ZIP** (idari şartname, sözleşme tasarısı, birim fiyat cetveli, katılım belgesi) |
| `?tur=teknik&dosyaId=<id>` | Seçilen teknik şartname dosyası (`.docx`, `.pdf`, …) |

Yanıt gerçek dosya baytlarıdır (`application/octet-stream`) ve
`Content-Disposition: attachment; filename="..."` taşır. `Content-Length`
gelmeyebilir → indirme çubuğunu belirsiz modda çalıştırın (mevcut kodda bu var).

---

## 3. Önerilen ekran akışı

```
[Doküman] butonu
   └─ GET /document-url/
        ├─ proxy=false → mevcut WebView akışı (değişmedi)
        └─ proxy=true  → GET /documents/
                            ├─ "İhale Dokümanı (ZIP)"        → RNFS ile indir
                            └─ Teknik şartname listesi
                                 ad + boyut  → dokununca RNFS ile indir
```

Öneriler:

- **Boyutu her satırda gösterin** (`48 KB`, `9,3 MB`, `951 MB`). 951 MB'lık bir dosyayı
  uyarısız indirtmek kullanıcının faturasını yakar. ~100 MB üstünde "Wi-Fi önerilir"
  uyarısı iyi olur.
- **Tek tek indirme**; "hepsini indir" koymayın (toplam 1 GB'ı bulabiliyor).
- İhale dokümanı ZIP'ini indirdikten sonraki davranış (arşiv açma, DocumentExplorer)
  **aynen korunur** — dosya biçimi değişmedi.
- İndirilen teknik şartname dosyaları arşiv değildir; `isValidArchiveFile` kontrolünü
  onlara **uygulamayın** (docx/pdf, PK sihirli baytı taşımayabilir).

---

## 4. Hata durumları

| Kod | Anlamı | Kullanıcıya |
|---|---|---|
| `404` "Bu ihalede ayrı bir teknik şartname yok." | Teknik şartname yok | Liste zaten boş gelirse bu ekrana hiç düşmeyin |
| `404` "Bu dosya bulunamadı." | `dosyaId` eskimiş (liste 24 sa önbellekli) | Listeyi yenileyip tekrar deneyin |
| `503` "Bugünkü belge indirme kotası doldu" | Günlük koruma tavanımız doldu | "Yarın tekrar deneyin" |
| `503` "EKAP doğrulama istedi ve otomatik çözülemedi" | EKAP captcha sordu, sunucu çözemedi | "Birazdan tekrar deneyin" — hata ekranı basmayın |
| `502` | EKAP yanıt vermedi | "Tekrar deneyin" |

⚠️ Bunların hiçbiri kalıcı hata değildir; kullanıcıyı ihale detayından atmayın.

---

## 5. Mevcut kodda değişecek yerler

| Dosya | Değişiklik |
|---|---|
| `src/screens/TenderDetail/index.js` (`handleDownloadPress`, ~satır 388) | Yanıttaki `proxy` bayrağına bak: `false` → bugünkü `DocumentWebView`; `true` → doküman listesi ekranı/sheet aç |
| Yeni: doküman listesi bileşeni | `documents_url`'den listeyi çek, ad + boyut göster, dokununca `RNFS.downloadFile` |
| `src/screens/TenderDetail/components/DocumentWebView.js` | **Dokunulmuyor** — yalnızca `proxy:false` modunda kullanılacak |
| `src/api/v1/api.js` | `getTenderDocuments(ihaleId)` ekleyin (`/ekap/tenders/{id}/documents/`) |

⚠️ Geriye dönük uyum: `document-url` **eski sözleşmeyi koruyor** (`data.url` hep var).
Mobil hiç güncellenmese bile ihale dokümanı inmeye devam eder; yalnızca teknik
şartnameler görünmez ve `proxy:true` modunda indirme WebView üzerinden gereksiz yere
iki kez olur.
