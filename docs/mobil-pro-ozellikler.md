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
  (temel arama herkese açıktır, bkz. §5)

`http.js` interceptor'ı bunu zaten işliyor; ekstra iş yok.

### B) `200` + `kilitli: true` → maskeli göster, dokununca Paywall

**YENİ.** `benchmark` ucu Free kullanıcıya da `200` döner; **örneklem sayıları görünür,
değerler `null`** gelir:

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

---

## 1. Fiyat analizi — `GET /ekap/tenders/{ekap_id}/benchmark/`

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

## 2. İdare profili — `GET /ekap/authorities/profile/`

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

## 3. Tekrar eden ihaleler

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

## 4. Rakip takibi — `/favorite-contractors/`

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

## 5. Gelişmiş filtreler — `GET /ekap/tenders/`

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

## 6. Free teaser bildirimi

Ücretsiz üyeye **haftada bir** (Pazartesi) gelen `type=INFO` bildirimi:

> **Bu hafta kaçırdıklarınız** — 2 kayıtlı filtrenize uygun 23 yeni ihale yayımlandı.
> Pro ile hepsini görün.

Hiçbir derin bağlantı alanı dolu değildir (push data'sında `teaser: "1"`) → **Paywall
açın**. Sayılar gerçektir; kullanıcı Pro olduğunda karşılığını görmelidir.

---

## 7. ⚠️ Veri olgunluğu — özellikler kademeli dolacak

Yeni kolonların çoğu (`okas_ana_kod`, `istekli_sayisi`, şikâyet bayrakları,
`seri_anahtar`) **arşive geriye dönük olarak** dolduruluyor. Bu yazının yazıldığı sırada:

- Yalnızca yeni senkronlanan ve tazelenen ihalelerde dolular.
- Arşivin geri kalanı gece çalışan bir doldurma görevini bekliyor; o da yüklenici
  süpürmesinin bitmesini bekliyor.

**Pratik sonuçları:**

- `benchmark` bugün `yeterli_veri: false` dönebilir, birkaç hafta sonra dolu döner.
- `GET /ekap/recurring/` başlangıçta **boş liste** dönebilir — hata değil.
- İdare profilinin `dagilim.okas` bölümü OKAS kolonu dolana kadar boş gelir.
- İndirim oranı kapsamı da artıyor (ölçüm: %24,5 → beklenen ~%55).

Bu yüzden **boş/eksik durumları normal karşılayın**: `null` değerler, boş diziler ve
`yeterli_veri: false` geçerli yanıtlardır. Sabit sayılara göre test yazmayın.

---

## 8. Kritik alan sözlüğü

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
| `yillara_gore` | Para karşılaştırmaları **her zaman** buradan; tek medyan enflasyonda yanıltır. |
