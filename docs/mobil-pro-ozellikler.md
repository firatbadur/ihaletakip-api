# Mobil Entegrasyon — Yeni Pro Özellikleri

Bu belge, "teklif karar destek" katmanıyla gelen **5 yeni uç** ve **24 yeni filtreyi**
mobil tarafın nasıl kullanacağını anlatır. Mevcut `mobil-yukleniciler.md` ve
`mobil-bildirimler.md` ile aynı sözleşmeleri izler.

Kapsanan özellikler:

| Özellik | Uç | Kullanıcıya vaadi |
|---|---|---|
| Fiyat analizi | `GET /ekap/tenders/{ekap_id}/benchmark/` | "Bu işe ne teklif vereyim?" |
| İdare profili | `GET /ekap/authorities/profile/` | "Bu kurum ne alıyor, kime, kaça?" |
| Tekrar eden ihaleler | `GET /ekap/recurring/` · `.../tenders/{ekap_id}/recurring/` | "Bu iş her yıl ne zaman açılıyor?" |
| Rakip takibi | `GET/POST /favorite-contractors/` | "Rakibim yeni iş aldı mı?" |
| Gelişmiş filtreler | `GET /ekap/tenders/?...` | Tutar / rekabet / indirim / şikâyet |

---

## 0. ⚠️ İKİ farklı kilit mekanizması var — karıştırmayın

Bugüne kadar tek bir desen vardı: `403` + `errors.code = "premium_required"` → Paywall.
Artık **ikinci** bir desen daha var ve ikisi farklı davranmalıdır.

### A) `403 premium_required` → doğrudan Paywall'a atla

Mevcut davranış, değişmedi. Bu uçlarda içerik hiç üretilmez:

- `GET /ekap/authorities/profile/`
- `GET /ekap/recurring/`, `GET /ekap/tenders/{id}/recurring/`
- `GET /ekap/tenders/?<gelişmiş filtre>` — **yalnızca** gelişmiş filtre kullanılırsa
  (temel arama herkese açıktır, bkz. §6)
- `POST /tender-alarms/` — ihale alarmı **kurma**
- `POST /saved-filters/` — **yalnızca** `alarm: true` ile
- `POST /assistant/chat/`, `POST /ai/analyze/`, `POST /support/`

`http.js` interceptor'ı bunu zaten işliyor; ekstra iş yok.

⚠️ **Üçüncü bir durum daha var ve o hiç hata vermez**: favori idare ve takip edilen
firmada `alarm: true` yazmak **200** döner ama bildirim asla gelmez (Pro'ya özel).
Mobilin bunu telafi etmesi gerekiyor — bkz. **§1.5**.

### B) `200` + `kilitli: true` → maskeli göster, dokununca Paywall

**YENİ.** İki uç böyle davranır: **fiyat analizi** (`benchmark`) ve **pazar panosu**
(`/ekap/market/…`). Free kullanıcıya da `200` döner; **sayılar görünür, para/oran
değerleri `null`** gelir:

```json
{
  "ornek": {"sozlesme_sayisi": 47, "indirim_ornek_sayisi": 31, "guven": "yuksek"},
  "indirim_orani": null,
  "sozlesme_bedeli": null,
  "rekabet": null,
  "benzer_ihaleler": null,
  "kilitli": true
}
```

Mobil bunu **maskeli** göstermeli, boş göstermemeli:

> **47 benzer iş bulundu** · kazanan indirim medyanı **%••,•**

Mevcut `maskDigits` + `ProValue` bileşenleri bunun için zaten var. Karta dokununca
Paywall açılır. Değerin *varlığını* göstermek, hiç göstermemekten çok daha iyi dönüşür —
kullanıcı neyi satın aldığını görür.

`kilitli: false` geldiğinde tüm alanlar dolu, normal render.

📋 **Hangi özelliğin hangi kapıya girdiğinin TAM listesi §1'de** — koddan doğrulanmış,
mobil planlaması için oradan başlayın.

---

## 1. Free vs Pro — TAM liste (koddan doğrulandı, 2026-08-13)

⚠️ **Sayısal Free limiti YOKTUR.** Eski 3 filtre / 5 ihale / 10 idare limitleri
**kaldırıldı**. Free kullanıcı sınırsız kaydeder, favoriler, klasör açar. Kısıt
**otomasyon ve içgörüde**, kaydetmenin kendisinde değil.

### 1.1 Girişsiz (token olmadan) çalışan uçlar

Mobil, kullanıcı hesap açmadan da arama yaptırabilir:

`GET /ekap/tenders/` · `/ekap/tenders/{id}/` · `/ekap/tenders/{id}/announcements/` ·
`/ekap/tenders/{id}/contracts/` · `/ekap/tenders/{id}/document-url/` ·
`/ekap/contractors/` · `/ekap/contractors/{id}/` · `/ekap/contractors/{id}/contracts/` ·
`/ekap/okas/search/` · `/ekap/authorities/tree/` · `/ekap/authorities/search/` ·
`/ekap/cities/`

⚠️ `GET /ekap/tenders/` girişsiz çalışır **ama** gelişmiş filtrelerden biri istekte
varsa 403 döner (anonim kullanıcı `is_premium=False`). Bkz. §1.4.

### 1.2 Free — sınırsız ve tam açık

| Özellik | Uç | Not |
|---|---|---|
| İhale arama (temel) | `GET /ekap/tenders/` | `q`, `il_id`, `ihale_tip`, `ihale_usul`, `ihale_durum`, `idare_id`, `idare_detsis`, `okas_kod`, `okas_adi`, `ozellik`, tarih aralıkları |
| İhale detayı, ilanlar, sözleşmeler | `/ekap/tenders/{id}/…` | |
| Doküman indirme | `/ekap/tenders/{id}/document-url/` | AI analizi Pro ama **indirme serbest** |
| Firma arama / detay / geçmiş | `/ekap/contractors/…` | |
| **İhale kaydetme** | `POST /saved-tenders/` | **Sınırsız** |
| **Klasör (kayıtlı ihale grubu)** | `/tender-groups/` | Kullanıcı başına en fazla **20** — Pro limiti DEĞİL, akıl sağlığı sınırı |
| **Filtre kaydetme** (alarmsız) | `POST /saved-filters/` | **Sınırsız** — `alarm` yoksa/`false` ise |
| **İdare favorileme** | `POST /favorite-authorities/` | **Sınırsız** |
| **Firma takip etme** | `POST /favorite-contractors/` | **Sınırsız** |
| İhale alarmı **listeleme/silme** | `GET`/`DELETE /tender-alarms/…` | Kurma Pro (§1.4) |
| Bildirim listesi / okundu | `/notifications/…` | |
| Asistan **profili oluşturma** | `GET`/`PUT /assistant/profile/` | Sohbet Pro (§1.4) |
| Asistan öneri listesi | `GET /assistant/recommendations/` | Liste açık; **öneri üretimi** Pro (§1.5) |
| Sesli okuma (TTS) | `POST /ai/tts/` | |
| Destek talebi **listeleme** | `GET /support/` | Oluşturma Pro (§1.4) |
| Abonelik doğrulama | `POST /subscription/verify/` | |

### 1.3 Free — bildirim olarak gelenler

| Bildirim | Zaman | Kime |
|---|---|---|
| **OKAS önerisi** — kaydettiğiniz ihalelerin kategorilerinde yeni ilan | Her gün 08:00 | **Free + Pro** |
| **Haftalık teaser** — "bu hafta N ihale kaçırdınız" | Pazartesi 10:00 | **Yalnızca Free** |

⚠️ Teaser'ın derin bağlantısı yoktur (`type=INFO`, push data'da `teaser: "1"`) →
dokunuşta **Paywall** açın. Sayılar gerçektir.

### 1.4 Pro — 403 `premium_required` dönen işlemler

Mobil `http.js` interceptor'ı bunları zaten yakalayıp Paywall açıyor:

| İşlem | Uç |
|---|---|
| **İhale alarmı KURMA/güncelleme** | `POST`/`PATCH /tender-alarms/` |
| **Filtreyi `alarm: true` ile kaydetme/güncelleme** | `POST`/`PATCH /saved-filters/` |
| Gelişmiş arama filtreleri (24 parametre) | `GET /ekap/tenders/?…` — bkz. §6 |
| Tekrar eden ihaleler | `GET /ekap/recurring/`, `/ekap/tenders/{id}/recurring/` |
| **İdare profili** | `GET /ekap/authorities/profile/` |
| Asistan sohbeti | `POST /assistant/chat/` |
| AI doküman analizi | `POST /ai/analyze/` |
| Destek talebi oluşturma | `POST /support/` |

### 1.5 Pro — 403 YOK, ama arka planda çalışmayanlar ⚠️ EN ÖNEMLİ BÖLÜM

Bu dört özellikte **kaydetme serbesttir, bildirim Pro'dur**. Uç 403 vermez, kullanıcı
anahtarı açar ve **hiçbir şey olmaz**:

| Özellik | Kaydetme | `alarm` alanı | Bildirim |
|---|---|---|---|
| Kayıtlı filtre | serbest | `true` yazmak → **403** | Pro (10:00) |
| **Favori idare** | serbest | `true` yazmak → **serbest** | **Pro** (11:00) |
| **Takip edilen firma** | serbest | `true` yazmak → **serbest** | **Pro** (12:00) |
| İhale alarmı | **403** | — | Pro (09:00) |

⚠️ **Aynı görünen anahtar iki farklı davranış gösteriyor**: kayıtlı filtrede
`alarm: true` **403** döner, favori idare ve firmada **200** döner ama bildirim asla
gelmez. Bu asimetri bilinçlidir (favorileme kısıtlanmasın istendi) ama mobilde
**mutlaka telafi edilmelidir**:

> Free kullanıcıda favori idare / firma alarm anahtarını ya **kilit rozetiyle** gösterin
> ve dokunuşta Paywall açın, ya da açılmasına izin verip **"Bildirimler Pro üyelere
> gönderilir"** notunu anahtarın altına yazın.

Aksi hâlde kullanıcı anahtarı açar, günlerce bekler, hiçbir bildirim gelmez ve
**Pro'nun ne işe yaradığını hiç hissetmez** — ürünün asıl dönüşüm açığı buydu.

### 1.6 Pro — maskeli (403 DEĞİL, `200` + `kilitli: true`)

| Özellik | Uç | Free'de görünen |
|---|---|---|
| Fiyat analizi | `/ekap/tenders/{id}/benchmark/` | Örneklem sayıları görünür, değerler `null` |
| Pazar panosu | `/ekap/market/`, `/ekap/market/{bucket}/` | Sözleşme/firma sayıları görünür, para ve indirim `null` |

Bkz. **§0** — bu, 403'ten farklı bir mekanizmadır ve **ayrı ele alınmalıdır**.

---

## 2. Fiyat analizi — `GET /ekap/tenders/{ekap_id}/benchmark/`

**Önerilen yer:** `TenderDetail` ekranına 4. sekme — "Fiyat Analizi".

```
GET /api/v1/ekap/tenders/e8c865a28be0b142/benchmark/?yil_geri=5
```

| Parametre | Varsayılan | Not |
|---|---|---|
| `yil_geri` | `5` | 1-10. ⚠️ 3 yapmayın: 2024-2025 arşivi hâlâ doldurulduğu için dar pencere veri deliğine düşer |
| `kapsam` | `auto` | `auto` yeterli örnek bulunana kadar genişler |
| `limit` | `20` | Benzer iş listesi boyutu (en çok 50) |

### Yanıtın okunması

```json
{
  "kapsam": {"seviye": "il", "aciklama": "Aynı il, aynı iş kalemi", "yil_geri": 5},
  "ornek": {"sozlesme_sayisi": 142, "indirim_ornek_sayisi": 118,
            "yeterli_veri": true, "guven": "yuksek"},
  "indirim_orani": {"p25": "0.1120", "medyan": "0.2140", "p75": "0.3010"},
  "sozlesme_bedeli": {"p25": "…", "medyan": "1250000.00", "p75": "…"},
  "yillara_gore": [{"yil": 2025, "adet": 40, "medyan": "…", "indirim_ornek_sayisi": 22}],
  "rekabet": {"ortalama_teklif_sayisi": 6.4, "ortalama_istekli_sayisi": 11.2},
  "fiyat_disi_unsur_var": false,
  "benzer_ihaleler": [ … ],
  "uyari": "İndirim oranı 118/142 sözleşmeden hesaplandı…"
}
```

**Zorunlu davranışlar:**

- **`kapsam.aciklama` mutlaka gösterilmeli.** Kullanıcı "bu idarede" mi "Türkiye
  genelinde" mi baktığını bilmeli — aynı sayı iki kapsamda çok farklı anlam taşır.
- **`ornek.yeterli_veri === false` → dağılımı GÖSTERMEYİN.** Sayıları "yetersiz veri"
  mesajıyla değiştirin.
- **`ornek.guven`**: `yuksek` / `orta` / `dusuk`. `dusuk` ise sayıları rozet veya renkle
  zayıflatın.
- **`indirim_orani` ORAN'dır, yüzde değil**: `0.2140` = **%21,4**. 100 ile çarpın.
- ⚠️ **`sozlesme_bedeli.medyan`'ı tek başına "ortalama fiyat" diye göstermeyin.** TL
  enflasyonunda 2021 ile 2026 bedelini aynı torbaya atmak yanıltıcıdır. **`yillara_gore`
  serisini gösterin** — grafik için hazır, en yeni yıl başta.
- **`fiyat_disi_unsur_var === true`** ise belirgin bir uyarı gösterin: *"Bu ihalede fiyat
  dışı unsur var — en düşük fiyat tek başına kazandırmaz."* Teklif stratejisini
  doğrudan etkiler.
- `uyari` doluysa küçük punto ile gösterin.

**Hata:** detayı henüz çekilmemiş ihalede `422` + Türkçe mesaj döner (`success: false`).

---

## 3. İdare profili — `GET /ekap/authorities/profile/`

**Önerilen yer:** `SavedAuthorities`, `AuthoritySelect` ve ihale detayındaki **idare adına
dokunma** → "İdare Raporu" ekranı.

Kapsam **üç yoldan biriyle** verilir (birini gönderin):

```
?idare_id=28484            # yaprak idare
?idare_detsis=24308110     # DETSIS düğümü (alt birimler dahil)
?en_ust_idare_kod=15       # bakanlık geneli — EN HIZLI yol
?detay=false               # yalnızca özet + yıllık seri
```

> 💡 Bakanlık geneli için **`en_ust_idare_kod` kullanın**, `idare_detsis` değil.
> İkincisi on binlerce alt birime açılır ve sunucu ayrıntılı kırılımı hesaplamayı
> reddedebilir (aşağıya bakın).

### Yanıt bölümleri

- `toplam` — ihale/sözleşme sayısı, toplam bedel, iptal/sonuçlanma/e-ihale/itiraz oranı,
  ortalama indirim ve teklif/istekli sayısı
- `yillara_gore` — yıl bazlı harcama ve indirim serisi (grafik için hazır)
- `yuklenici_dagilim` — ilk 10 firma + `yogunlasma` (HHI, ilk5 payı)
- `dagilim` — `usul`, `tur`, `mevsimsellik` (ay bazlı), `il`, `okas`

**Zorunlu davranışlar:**

- ⚠️ **Ortalamaları örneklem sayısı olmadan göstermeyin**: `ortalama_indirim` yanında
  `indirim_ornek_sayisi`, `ortalama_istekli_sayisi` yanında `istekli_ornek_sayisi`.
  `ornek_sayisi === 0` ise ortalamayı **hiç göstermeyin**.
- ⚠️ **`itiraz_orani`'nın paydası tüm ihaleler DEĞİL**, bayrağı bilinen ihalelerdir
  (`itiraz_ornek_sayisi`). "Bu idarenin ihalelerinin %8'ine itiraz edilmiş" derken bu
  payda üzerinden konuşun.
- **`kapsam.cok_genis === true`** ise `yuklenici_dagilim` ve `dagilim` `null` gelir,
  `mesaj` alanı doludur → kullanıcıya "daha dar bir idare seçin" deyin.
- **`yogunlasma.hhi`**: 0'a yakın = parçalı pazar, 1'e yakın = tek firma hâkimiyeti.
  `yaklasik === true` ise "≈" ile gösterin.
- **`mevsimsellik`** = ihale takvimi; "bu idare genelde Mart'ta ihale açıyor" içgörüsü
  buradan çıkar. 12 aylık bar grafik için hazır (`{ay: 1..12, adet: N}`).

---

## 4. Tekrar eden ihaleler

### 3.1 İhale bazlı — `GET /ekap/tenders/{ekap_id}/recurring/`

**Önerilen yer:** ihale detayında "Bu iş her yıl tekrarlanıyor" bloğu.

```json
{"seri": {"periyot_tip": "yillik", "periyot_gun": 366, "guven": "yuksek",
          "beklenen_ay": "2026-10", "beklenen_ilan_tarihi": "2026-10-15",
          "ihale_sayisi": 5, "ortalama_bedel": "…",
          "ortalama_indirim": "0.1840", "indirim_ornek_sayisi": 4},
 "gecmis": [ /* EkapTenderListSerializer — mevcut mapper'ınız çalışır */ ]}
```

`seri === null` ise bu ihale bir seriye ait değil → bloğu hiç göstermeyin.

### 3.2 Liste — `GET /ekap/recurring/`

**Önerilen yer:** idare raporunda "Beklenen ihaleler" ve/veya ayrı bir "Takvim" ekranı.

```
?beklenen_gun=90&guven=yuksek,orta&order=beklenen
?idare_id= | ?idare_detsis= | ?en_ust_idare_kod= | ?okas_ana_kod= | ?il_id= | ?ihale_tip=
?periyot_tip=yillik&aktif=true
```

**Zorunlu davranışlar:**

- ⚠️ **`guven` alanını mutlaka yansıtın.** `beklenen_ilan_tarihi` bir **tahmindir**;
  `dusuk` güvende kesin tarih gibi sunulmamalı. Öneri: `yuksek` → "Ekim 2026 bekleniyor",
  `dusuk` → "≈ Ekim 2026 (zayıf tahmin)".
- **`periyot_gun` ± `sapma_gun`** tahminin ne kadar oynadığını gösterir.
- `aktif` varsayılan olarak `true` filtrelenir (sona ermiş seriler gelmez); geçmişi de
  görmek isterseniz `aktif=false` gönderin.
- `ortalama_indirim` yine **`indirim_ornek_sayisi` ile birlikte**.

---

## 5. Rakip takibi — `/favorite-contractors/`

**Önerilen yer:** `ContractorDetail`'e "Takip Et" butonu + drawer'a "Takip Ettiğim
Firmalar" (`SavedAuthorities` ekranının ikizi).

```
GET    /api/v1/favorite-contractors/
POST   /api/v1/favorite-contractors/        {"contractor": 1234, "alarm": true}
GET    /api/v1/favorite-contractors/1234/   → {"is_favorite": true}
DELETE /api/v1/favorite-contractors/1234/   → 204 (idempotent)
```

- Yalnızca `contractor` (id) gönderin; `ad`, `kind`, `sozlesme_sayisi`,
  `son_sozlesme_tarihi` sunucudan zenginleştirilmiş gelir.
- Aynı firma tekrar gönderilirse **upsert** (hata yok).
- ⚠️ **Takip etmek her üyeye açık ve sınırsız; bildirim Pro'ya özel.** Uç `403`
  **vermez** — Free kullanıcı takip edebilir ama "yeni iş aldı" bildirimi almaz.
  Favori idaredeki asimetrinin aynısı. Free kullanıcıya alarm anahtarının yanında
  "Pro ile bildirim al" ipucu göstermek doğru olur.

**Bildirim:** firma yeni iş aldığında `type=TENDER` + **`contractor_id`** dolu bildirim
gelir → firma detayını açın. Derin bağlantı önceliği güncellendi (bkz.
`mobil-bildirimler.md` §4): `conversation_id` > `filter_id` > `authority_detsis` >
**`contractor_id`** > `okas_kodlar` > `tender_ikn`.

---

## 6. Gelişmiş filtreler — `GET /ekap/tenders/`

**Temel arama herkese açık kalır.** Yalnızca aşağıdaki parametreler Pro'dur; biri
kullanılırsa Free/anonim istek `403 premium_required` alır.

**Önerilen yer:** `TenderFilter` ekranına "Gelişmiş (PRO)" bölümü — kilitli görünsün,
dokununca Paywall açılsın (istek atmadan).

| Grup | Parametreler |
|---|---|
| Tutar | `yaklasik_maliyet_min/max`, `sozlesme_bedeli_min/max` |
| Rekabet | `teklif_sayisi_min/max`, `istekli_sayisi_min/max` |
| Sonuç | `indirim_orani_min/max`, `sonuclanmis`, `iptal` |
| Nitelik | `e_ihale`, `kismi_ihale`, `ilansiz_mi`, `e_eksiltme_yapilacak` |
| Risk | `fiyat_disi_unsur_var`, `itirazen_sikayet_var`, `idareye_sikayet_var`, `sikayet_dilekce_var`, `duzeltme_ilani_var` |
| Kategori | `okas_ana_kod`, `en_ust_idare_kod`, `seri_anahtar` |

**Zorunlu davranışlar:**

- `indirim_orani_*` **oran** alır (`0.20` = %20), yüzde değil.
- ⚠️ **`data.uyari` geldiyse gösterin.** Kapsamı kısmi olan alanlarda (yaklaşık maliyet,
  teklif sayısı, istekli sayısı) aralık filtresi, değeri **bilinmeyen** ihaleleri de
  eler — kullanıcı "sonuç yok" ile "veri yok"u ayırt edebilmeli.
- Boolean filtreler `true`/`false` **ayrı anlamlıdır**: `sonuclanmis=false` "sözleşmesi
  olmayanlar" demektir, parametreyi hiç göndermemek "filtreleme" demektir.

---

## 7. Pazar panosu — `GET /ekap/market/`

"Kamu bu yıl neye ne kadar harcadı, kim kazandı?" İki uç:

| Uç | Ne döner |
|---|---|
| `GET /ekap/market/?yil=2025&limit=20` | Yıl özeti + o yılın en büyük iş grupları |
| `GET /ekap/market/{okas_bucket}/?yil=2025` | Grubun yıllara göre seyri + il + firma + yoğunlaşma |

### Ana ekran

```json
{
  "yil": 2025,
  "yillar": [2026, 2025, 2024, 2023, 2022],
  "ozet": {
    "sozlesme_sayisi": 160947, "firma_sayisi": 47213, "idare_sayisi": 8104,
    "toplam_bedel": "812450900000.00",
    "ortalama_indirim": "0.2358", "indirim_guven": "yuksek", "indirim_ornek_sayisi": 49772,
    "ortalama_teklif": "10.4519", "teklif_ornek_sayisi": 79900
  },
  "is_gruplari": [{
    "okas_bucket": "4523", "ad": "Devlet, il ve köy yolları yapım işleri",
    "sozlesme_sayisi": 5717, "toplam_bedel": "94210000000.00",
    "ortalama_indirim": "0.1086", "indirim_guven": "dusuk", "indirim_ornek_sayisi": 496,
    "ortalama_teklif": "6.1000", "teklif_ornek_sayisi": 3900
  }],
  "guncelleme": "2026-08-14T01:30:00+03:00",
  "kilitli": false
}
```

`yillar` dizisini yıl seçiciye bağlayın; varsayılan **en güncel yıl**dır.

### ⚠️ `ortalama_indirim` `null` gelebilir — bu bir hata değil

`indirim_guven` alanı dört değer alır ve **görsel olarak ayırt edilmelidir**:

| `indirim_guven` | Ne yapmalı |
|---|---|
| `yuksek` | Değeri normal göster. |
| `orta` | Değeri göster + örneklem sayısını yanına yaz. |
| `dusuk` | Değeri göster ama **soluk/uyarı rengiyle** + "n=496 sözleşmeden" notu. |
| `yetersiz` | **`ortalama_indirim` `null`'dur.** "Veri yetersiz" yazın — `0` ya da `—` göstermeyin, "veri yok" deyin. |

Sebebi ürün açısından önemli: indirim oranı yalnızca **Sonuç İlanı yayımlanmış**
sözleşmelerde bilinir ve o ilan imzadan **aylar sonra** çıkar. Bu yüzden **en güncel
yılda indirim verisi neredeyse yoktur** (üretimde 2026 kapsamı %1,7) ve o az sayıdaki
kayıt sistematik bir alt kümedir — sunucu bu durumda değeri bilinçli olarak `null`'lar.
Hacim metrikleri (`sozlesme_sayisi`, `toplam_bedel`) güncel yılda **sağlamdır**, onları
gösterin.

### ⚠️ İki sayı iş gruplarından TOPLANAMAZ

`ozet.firma_sayisi` ve `ozet.idare_sayisi` **tekil** sayılardır. Aynı firma birden çok
iş grubunda iş almış olabilir → `is_gruplari` satırlarını toplayarak bu sayılara
ulaşmaya **çalışmayın**. Sunucu onları ayrı hesaplar. Toplanabilir olanlar yalnızca:
`sozlesme_sayisi`, `toplam_bedel`, `*_ornek_sayisi`.

### ⚠️ "Sınıflandırılmamış" grubu listeden düşürmeyin

`okas_bucket: ""` gerçek veridir (sözleşmelerin ~%15'i OKAS'sız) ve `ad` alanı
`"Sınıflandırılmamış"` gelir. Listeden çıkarırsanız yüzdeler ve toplamlar tutmaz.

### Grup detayı

```json
{
  "okas_bucket": "4523", "ad": "Devlet, il ve köy yolları yapım işleri",
  "yillar": [2026, 2025, 2024],
  "yillara_gore": [{"yil": 2024, "sozlesme_sayisi": 5100, "toplam_bedel": "…",
                    "ortalama_indirim": "0.1846", "indirim_guven": "yuksek",
                    "indirim_ornek_sayisi": 2400}],
  "yil": 2025,
  "iller":    [{"il_id": 6, "ad": "ANKARA", "sozlesme_sayisi": 412, "toplam_bedel": "…"}],
  "firmalar": [{"contractor_id": 8123, "ad": "… LTD ŞTİ", "sozlesme_sayisi": 27,
                "toplam_bedel": "…"}],
  "yogunlasma": {"hhi": 0.0421, "firma_sayisi": 5527, "yaklasik": false},
  "kilitli": false
}
```

- `firmalar[].contractor_id` → mevcut firma detayına bağlanır (`GET /ekap/contractors/{id}/`).
- ⚠️ **`yillara_gore`'daki para değerlerini yıllar arası doğrudan karşılaştırmayın** —
  TL enflasyonu nedeniyle yanıltır. Trend çiziyorsanız bunu kullanıcıya belirtin;
  `sozlesme_sayisi` ve `ortalama_indirim` enflasyondan bağımsızdır, onlar güvenli.
- ⚠️ `yogunlasma.yaklasik: true` → firma sayısı hesap tavanını aşmış, HHI kesilmiş bir
  küme üzerinden hesaplanmıştır. Değerin başına "≈" koyun.

### Free maskeleme

**403 değil, `200` + `kilitli: true`.** Maskeleme sunucuda yapılır; Free kullanıcıda
şu alanlar `null` gelir: `toplam_bedel`, `ortalama_indirim`, `ortalama_teklif`,
`yogunlasma.hhi`. **Sayılar açık kalır** (`sozlesme_sayisi`, `firma_sayisi`,
`indirim_ornek_sayisi`) — teaser değeri buradan gelir:

> **2025'te en çok harcama yapılan iş grubu: Devlet, il ve köy yolları**
> 5.717 sözleşme · toplam •.•••.•••.••• ₺ · ortalama indirim %••,•
> *Pro ile açın*

Dokunuşta Paywall açın. Bkz. **§0** — bu, 403'ten farklı bir mekanizmadır.

### Tazelik

Veri her gece **01:30**'da yenilenir; `guncelleme` alanı son hesaplama zamanıdır.
Gün içinde değişmez — istemcide agresif cache'lemek güvenlidir.

---

## 8. Free teaser bildirimi

Ücretsiz üyeye **haftada bir** (Pazartesi) gelen `type=INFO` bildirimi:

> **Bu hafta kaçırdıklarınız** — 2 kayıtlı filtrenize uygun 23 yeni ihale yayımlandı.
> Pro ile hepsini görün.

Hiçbir derin bağlantı alanı dolu değildir (push data'sında `teaser: "1"`) → **Paywall
açın**. Sayılar gerçektir; kullanıcı Pro olduğunda karşılığını görmelidir.

---

## 9. Veri olgunluğu — arşiv TAMAMLANDI, ama boş değerler hâlâ normal

**2026-08-13 itibarıyla geriye dönük doldurma bitti:** 1.043.455 ihalenin **%100'ü**
detaylı (2019/2020/2021 dahil), 1.407.379 sözleşme, Pro sinyal kolonları %99,99 dolu.
Yani `okas_ana_kod`, `istekli_sayisi`, şikâyet bayrakları ve `seri_anahtar` artık
arşivin tamamında mevcut.

**Buna rağmen `null` ve boş diziler hâlâ geçerli yanıtlardır** — sebepleri yapısal,
geçici değil:

| Durum | Sebep | Doğru davranış |
|---|---|---|
| `ortalama_indirim: null` + `indirim_guven: "yetersiz"` | Sonuç İlanı imzadan aylar sonra yayımlanır → güncel yılda kapsam çok düşük | "Veri yetersiz" yazın |
| `benchmark` → `yeterli_veri: false` | O benzerlik kademesinde yeterli sonuçlanmış iş yok | Dağılımı hiç göstermeyin |
| `istekli_sayisi: null` | Alan **değerlendirme sonrası** dolar; açık ihalede daima boş | "—" gösterin, `0` DEĞİL |
| İhalelerin ~%19'unda OKAS yok | EKAP'ta öyle | `okas_bucket: ""` → "Sınıflandırılmamış" |
| Şikâyet bayrakları `null` | Üç değerli: `null` = "bilinmiyor", `false` = "hayır" | İkisini ayırt edin |

⚠️ `indirim_orani` kapsamı **%37,6** (529.798/1.407.379) ve bu **tavana yakındır** —
EKAP tüm sözleşmeler için Sonuç İlanı yayımlamıyor. Daha da artmasını beklemeyin.

**Sabit sayılara göre test yazmayın**; `null` ve boş diziyi her uçta geçerli kabul edin.

---

## 10. Kritik alan sözlüğü

| Alan | Dikkat |
|---|---|
| `indirim_orani` | **Oran**, yüzde değil. `0.2140` = %21,4. Negatif olabilir (bedel maliyetin üstünde). |
| `*_ornek_sayisi` | Yanındaki ortalamanın kaç kayıttan hesaplandığı. **0 ise ortalamayı hiç göstermeyin.** |
| `guven` | `yuksek`/`orta`/`dusuk`. Hem örneklem büyüklüğünü hem düzenliliği özetler. |
| `kapsam.seviye` | Benchmark hangi benzerlik kademesinden cevaplandı — kullanıcıya gösterin. |
| `kilitli` | `true` → maskeli göster (403 DEĞİL). |
| `hhi` | Yoğunlaşma; 1'e yakın = tek firma hâkimiyeti. `yaklasik` ise "≈". |
| `beklenen_ilan_tarihi` | **Tahmin.** `guven` ile birlikte sunulmalı. |
| `fiyat_disi_unsur_var` | `true` → en düşük fiyat tek başına kazandırmaz; belirgin uyarı. |
| `itiraz_orani` | **Her zaman `null`** — EKAP itirazen şikâyet bilgisini üçüncü taraf çağrılarında paylaşmıyor. Ekranda göstermeyin ya da "veri yok" yazın. `itiraz_veri_yok_nedeni` gerekçeyi taşır. |
| `yillara_gore` | Para karşılaştırmaları **her zaman** buradan; tek medyan enflasyonda yanıltır. |
| `indirim_guven` | `yuksek`/`orta`/`dusuk`/**`yetersiz`**. `yetersiz` ise değer `null`'dur → "veri yok" yazın, `0` göstermeyin. |
| `okas_bucket: ""` | **Sınıflandırılmamış** — gerçek veri (sözleşmelerin ~%15'i). Listeden düşürmeyin, toplamlar tutmaz. |
| `ozet.firma_sayisi` | **Tekil** sayı — iş gruplarından toplanamaz (aynı firma birden çok grupta olabilir). |
