# Mobil Entegrasyon — Yükleniciler (Firma Kaydı)

Bu doküman mobil uygulamanın **sözleşme imzalamış yüklenici firmaları** (arama, firma
geçmişi, ihale-yüklenici bağı) uçtan uca uygulaması için gereken API'leri ve davranışları
anlatır.

> Taban URL: `https://ihale-takip.envisoft.com.tr/api/v1/`
> Kaynak: view'lardaki `@extend_schema`; `docs/openapi.yaml` ve
> `docs/postman_collection.json` her zaman günceldir (Postman'e import edip deneyebilirsiniz).

---

## 0. Ortak kurallar

- **Kimlik gerekmez.** Bu uçlar diğer `ekap/` uçları gibi **herkese açıktır** (`AllowAny`).
  Token göndermek zararsızdır. İleride Pro'ya kilitlenirse `403` + `errors.code =
  "premium_required"` dönecektir — mevcut premium işleme mantığınızı bu uçlar için de
  hazırda tutun (bkz. `mobil-bildirimler.md` §0).
- **Yanıt zarfı** — tüm yanıtlar:
  ```json
  { "success": true, "message": "", "data": { } }
  ```
- **Sayfalı liste zarfı** — `data` içinde:
  ```json
  { "list": [ ... ], "totalCount": 1234, "page": 1 }
  ```
  `page` (1'den başlar) ve `page_size` (varsayılan 20, **azami 100**) query param'dır.
- **Para alanları STRING'dir** (`"2834670.00"`). Hassasiyet kaybı olmasın diye Decimal
  olarak serileştirilir. Görüntülerken `Number()`/`parseFloat` ile bozmayın; string'i
  doğrudan biçimlendirin ya da decimal-güvenli bir kütüphane kullanın.
- **Tarihler ISO-8601 + timezone** (`"2025-03-10T21:00:00+00:00"`). UTC gelir, cihaz
  saatine çevirin.
- **Türkçe arama güvenlidir**: `?q=sümerliler`, `?q=SUMERLILER`, `?q=SÜMERLİLER` aynı
  sonucu verir. İstemcide normalize etmeye gerek yok.
- **Alan adları snake_case**'dir (ihale uçlarındaki camelCase'den farklı). Sebep: EKAP'ta
  yüklenici nesnesi yoktur, yansıtılacak bir EKAP şekli yoktur.

---

## 1. ⚠️ Önce sınırlar — UI tasarlamadan okuyun

EKAP yalnızca **imzalanmış sözleşmeleri** yayımlar. Bunun doğrudan sonuçları:

| İstenen | Durum |
|---|---|
| Firmanın **kazandığı** işler, bedelleri, tarihleri | ✅ var |
| Firmanın **kaybettiği** ihaleler / verdiği teklifler | ❌ **yok, hiç gelmeyecek** |
| **Kazanma oranı** (kazandığı / katıldığı) | ❌ **hesaplanamaz** — API'de böyle bir alan yok, eklemeyin |
| Vergi no / VKN / TCKN | ❌ EKAP vermiyor |
| Kısım (lot) bazında tutar | ❌ EKAP bozuk gönderiyor, bilerek dönülmüyor (yalnızca kısım adları var) |

**"Hangi ihaleye ne teklif verdi" sorusu yalnızca kazandığı işler için yanıtlanabilir.**
Ekranda "katıldığı ihaleler" gibi bir başlık kullanmayın; doğrusu **"aldığı işler"**.

Ayrıca firma kimliği **ünvan** üzerinden kurulur (VKN olmadığı için). Aynı ünvanlı iki
gerçek firma birleşebilir, ünvan değiştiren firma ikiye ayrılabilir. Şeffaflık için firma
detayında `aliaslar` döner — hangi yazımların o firmada birleştiğini gösterir.

---

## 2. Firma arama / listeleme

**`GET /ekap/contractors/`**

| Param | Tip | Açıklama |
|---|---|---|
| `q` | string | Ünvan araması (Türkçe-i güvenli) |
| `kind` | csv | `firma`, `sahis`, `ortak_girisim` (virgülle çoklu) |
| `il_id` | csv | İl id listesi (ihale uçlarındaki `il_id` ile aynı) |
| `min_sozlesme` | int | En az bu kadar sözleşmesi olanlar |
| `order` | enum | `sozlesme_sayisi` (varsayılan) · `toplam_bedel` · `son_sozlesme` · `ad` |
| `siralamaTipi` | enum | `desc` (varsayılan) · `asc` |
| `page`, `page_size` | int | Sayfalama (page_size azami 100) |

```
GET /ekap/contractors/?q=decoline
```
```json
{
  "success": true,
  "message": "",
  "data": {
    "list": [
      {
        "id": 11,
        "ad": "DECOLINE MEDİKAL MOBİLYA TEKSTİL İTHALAT İHRACAT SANAYİ TİCARET LİMİTED ŞİRKETİ",
        "kind": "firma",
        "kind_aciklama": "Firma",
        "il_adi": "SAMSUN",
        "sozlesme_sayisi": 12,
        "ihale_sayisi": 9,
        "toplam_sozlesme_bedeli": "41250000.00",
        "son_sozlesme_tarihi": "2025-03-09T21:00:00+00:00",
        "ortalama_indirim_orani": "0.2010",
        "indirim_orani_ornek_sayisi": 4,
        "uye_sayisi": 0,
        "ortak_girisim_sayisi": 0
      }
    ],
    "totalCount": 1,
    "page": 1
  }
}
```

**Liste satırında gösterilmesi önerilenler:** `ad`, `il_adi`, `sozlesme_sayisi`,
`toplam_sozlesme_bedeli`. `kind_aciklama` rozet olarak (Firma / Şahıs / Ortak Girişim).

---

## 3. Firma detayı

**`GET /ekap/contractors/<id>/`** — `id` liste yanıtındaki tam sayıdır.

```json
{
  "success": true,
  "message": "",
  "data": {
    "id": 12,
    "ad": "HORTOĞLU İNŞAAT TAAHHÜT TURİZM TİCARET TAŞIMACILIK SANAYİ İTHALAT VE İHRACAT LİMİTED ŞİRKETİ",
    "kanonik_anahtar": "hortoglu insaat taahhut turizm ticaret tasimacilik sanayi ithalat ve ihracat ltdsti",
    "kind": "firma",
    "kind_aciklama": "Firma",
    "tuzel_tip": "ltdsti",
    "uyruk": "Türkiye",
    "adres": "A. HİSAR MAH. 4501 SOK. RAMIZ BEY IŞ HANI NO: 13 MANAVGAT/ANTALYA",
    "il_adi": "ANTALYA",
    "il_id": 252,
    "istatistik": {
      "sozlesme_sayisi": 1,
      "ihale_sayisi": 1,
      "idare_sayisi": 1,
      "toplam_sozlesme_bedeli": "2834670.00",
      "ilk_sozlesme_tarihi": "2025-03-10T21:00:00+00:00",
      "son_sozlesme_tarihi": "2025-03-10T21:00:00+00:00",
      "ortalama_indirim_orani": "0.3868",
      "indirim_orani_ornek_sayisi": 1
    },
    "dagilim": {
      "ihale_tipi": [{ "ihale_tip": 1, "ad": "Mal Alımı", "adet": 1, "toplam_bedel": "2834670.00" }],
      "il":        [{ "il_id": 252, "ad": "ANTALYA", "adet": 1, "toplam_bedel": "2834670.00" }],
      "yil":       [{ "yil": 2025, "adet": 1, "toplam_bedel": "2834670.00" }],
      "idare":     [{ "idare_id": "28484", "ad": "ANTALYA İL SAĞLIK MÜDÜRLÜĞÜ", "adet": 1, "toplam_bedel": "2834670.00" }]
    },
    "aliaslar": ["HORTOĞLU İNŞAAT TAAHHÜT ... LİMİTED ŞİRKETİ"],
    "ortak_girisimler": [],
    "uyeler": [],
    "uyeleri_cozumlendi": true
  }
}
```

- `dagilim.*` listeleri **adete göre azalan** sıralıdır; `idare` en çok **10** satır döner.
- `dagilim.il[].ad` ve `dagilim.ihale_tipi[].ad` hazır Türkçe etiketlerdir, eşleme yapmayın.
- Firma bulunamazsa **HTTP 404**: `{"success": false, "message": "Yüklenici bulunamadı.", "data": null}`.

### Ortak girişim (İş Ortaklığı / Konsorsiyum)

`kind = "ortak_girisim"` olan kayıt bir **ortaklıktır**; üyeleri `uyeler` dizisindedir.
Tersi de doğrudur: normal bir firmanın girdiği ortaklıklar `ortak_girisimler` dizisinde gelir.

```json
"uyeler": [
  { "id": 88, "ad": "ALFA İNŞAAT LİMİTED ŞİRKETİ", "pilot": true, "sozlesme_sayisi": 14 },
  { "id": 92, "ad": "BETA YAPI ANONİM ŞİRKETİ",    "pilot": false, "sozlesme_sayisi": 6 }
],
"ortak_girisimler": [
  { "id": 77, "ad": "ALFA - BETA İş Ortaklığı", "sozlesme_sayisi": 2, "pilot": true }
]
```

`uyeleri_cozumlendi: false` → ortaklık ünvanı güvenle üyelerine ayrıştırılamadı; `uyeler`
**boş gelir**. Bu bir hata değil, "tahmin etmektense boş bırak" kararıdır. UI'da üye
bölümünü gizleyin, hata göstermeyin.

---

## 4. Firmanın sözleşme geçmişi (aldığı işler)

**`GET /ekap/contractors/<id>/contracts/`** — en yenisi başta, sayfalı.

Filtreler: `il_id` (csv), `idare_id` (csv), `ihale_tip` (csv), `yil` (int),
`order` (`sozlesme_tarihi` | `sozlesme_bedeli`), `siralamaTipi`, `page`, `page_size`.

```json
{
  "id": 32,
  "ekap_sozlesme_id": "13495829",
  "sozlesme_tarihi": "2025-03-10T21:00:00+00:00",
  "sozlesme_bedeli": "2834670.00",
  "en_dusuk_teklif": "2834670.00",
  "en_yuksek_teklif": "7784228.25",
  "yaklasik_maliyet": "4622638.02",
  "yaklasik_maliyet_kaynak": "sonuc_ilani",
  "ihale_yaklasik_maliyet": "65596282.36",
  "indirim_orani": "0.3868",
  "rekabet_araligi": "1.7461",
  "teklif_sayisi": 13,
  "gecerli_teklif_sayisi": 12,
  "dokuman_indiren_sayisi": 34,
  "en_dusuk_teklifi_veren_mi": true,
  "fesih": "Yok",
  "tasfiye_transfer": "Yok",
  "ihale": {
    "ekap_id": "4659b02605e8f03d...",
    "ikn": "2024/1362677",
    "ihale_adi": "2025 YILI TEKSTİL MALZEMELERİ ALIMI (12 AYLIK)",
    "idare_adi": "ANTALYA İL SAĞLIK MÜDÜRLÜĞÜ",
    "idare_id": "28484",
    "ihale_il_adi": "ANTALYA",
    "ihale_tip": 1,
    "ihale_tipi_aciklama": "Goods",
    "ihale_tarihi": "2024-11-19T11:30:00+00:00"
  },
  "kisimlar": [
    { "ekap_kisim_id": "36261112", "kisim_adi": "PAZEN DESENLİ KUMAŞ" },
    { "ekap_kisim_id": "36261113", "kisim_adi": "PİKE" }
  ]
}
```

`ihale.ekap_id` ile mevcut **ihale detay ekranınıza** yönlendirin
(`GET /ekap/tenders/<ekap_id>/`). **İKN kullanmayın** — İKN `/` içerir, yol parametresi
olarak çalışmaz.

---

## 5. İhalenin yüklenicileri (ihale detay ekranına "Sonuç" bölümü)

**`GET /ekap/tenders/<ekap_id>/contracts/`** — mevcut `announcements/` ucuyla aynı desende.
Sayfalama yok, `data.list` doğrudan gelir (kısımlı ihalede birden çok sözleşme olur).

Sözleşme alanları §4 ile aynıdır, ek olarak:

```json
"yuklenici_ham_ad": "SALİM KÜÇÜK",
"yuklenici": {
  "id": 17,
  "ad": "SALİM KÜÇÜK",
  "kind": "sahis",
  "kind_aciklama": "Şahıs",
  "sozlesme_sayisi": 1,
  "uyeler": []
}
```

- **`yuklenici` `null` OLABİLİR** (satır henüz firmaya çözülmemişse). Bu durumda
  `yuklenici_ham_ad`'ı gösterin, firma detayına link vermeyin. Kod bunu tolere etmeli.
- İhale bulunamazsa **HTTP 200** + `{"list": []}` döner (404 değil).
- Sözleşme yoksa (ihale henüz sonuçlanmamış) yine `{"list": []}` → "Henüz sonuçlanmadı"
  gösterin.

---

## 6. İhale aramasında yüklenici filtresi

Mevcut **`GET /ekap/tenders/`** ucuna iki yeni parametre eklendi:

| Param | Açıklama |
|---|---|
| `yuklenici_id` | Firma id (csv). Kesin eşleşme — firma detayından gelirken bunu kullanın. |
| `yuklenici` | Serbest metin (Türkçe-i güvenli). Kullanıcı arama kutusu için. |
| `ortakliklari_dahil_et` | Varsayılan `true`. Firmanın **ortak girişim üyesi olarak** aldığı işler de gelsin mi. `false` ile kapatılır. |

```
GET /ekap/tenders/?yuklenici_id=12
GET /ekap/tenders/?yuklenici=hortoğlu
GET /ekap/tenders/?yuklenici_id=12&ortakliklari_dahil_et=false
```

Yanıt biçimi mevcut ihale listesiyle **aynıdır** (camelCase EKAP alanları) — mevcut
mapper'larınız değişmeden çalışır. Bu parametreler `SavedFilter.filters` gövdesinde de
kullanılabilir (kayıtlı filtre → "şu firmanın yeni işleri").

---

## 7. Kritik alan sözlüğü — yanlış okunması kolay olanlar

| Alan | Dikkat |
|---|---|
| `sozlesme_sayisi` vs `ihale_sayisi` | **Aynı şey değil.** Kısımlı ihalede bir firma 3 kısım alırsa **3 sözleşme / 1 ihale** olur. "Kaç iş aldı" için `ihale_sayisi`, "kaç sözleşme imzaladı" için `sozlesme_sayisi` gösterin. Sözleşme sayısını "ihale" diye etiketlemeyin. |
| `yaklasik_maliyet` | **`null` olabilir.** Yalnızca Sonuç İlanı yayımlanmış sözleşmelerde bilinir. `yaklasik_maliyet_kaynak == "sonuc_ilani"` ise değer güvenilirdir; `null` ise **"veri yok"** gösterin, 0 veya "-" ile karıştırmayın. |
| `ihale_yaklasik_maliyet` | İhalenin **tamamının** yaklaşık maliyeti. `yaklasik_maliyet` ise yalnızca **bu sözleşmeye esas kısımların** maliyetidir. Kısımlı ihalede ikisi çok farklıdır — indirim oranı hesabında `yaklasik_maliyet` kullanılır. |
| `indirim_orani` | `(yaklaşık maliyet − sözleşme bedeli) / yaklaşık maliyet`. `0.3868` = **%38,7 indirim**. Yüzde göstermek için 100 ile çarpın. Negatif olabilir (bedel maliyetin üstünde). |
| `ortalama_indirim_orani` | **Asla tek başına göstermeyin** — yanında `indirim_orani_ornek_sayisi` olmalı. Yaklaşık maliyet kapsamı kısmi olduğu için ortalama, firmanın tüm işlerini temsil etmez. Örn: "%20 ortalama indirim (4 sözleşmede)". `ornek_sayisi = 0` ise ortalamayı hiç göstermeyin. |
| `rekabet_araligi` | `(en yüksek − en düşük) / en düşük`. Teklif sahasının genişliği. `1.7461` = en yüksek teklif, en düşüğün ~2,75 katı. |
| `en_dusuk_teklifi_veren_mi` | Neredeyse hep `true`. **`false` bir anomalidir**: en düşük teklifi veren elenmiş demektir — rozet/uyarı olarak değerlidir. `null` ise veri eksiktir. |
| `gecerli_teklif_sayisi` | `teklif_sayisi`'ndan küçük olabilir (elenen teklifler). Rekabet yoğunluğu için bunu kullanın. |
| `fesih` / `tasfiye_transfer` | EKAP'ın ham metni (`"Yok"` / açıklama). Boşsa `null`. |
| `kisimlar` | Yalnızca `ekap_kisim_id` + `kisim_adi`. **Tutar yoktur** ve eklenmeyecektir (EKAP bozuk ölçekte gönderiyor). |

---

## 8. Önerilen ekran akışı

```
İhale Detay
  └─ "Sonuç / Yüklenici" bölümü        GET /ekap/tenders/<ekap_id>/contracts/
       └─ yüklenici kartına dokun  ──┐
                                     │
Firma Arama                          │   GET /ekap/contractors/?q=
  └─ sonuç satırına dokun  ──────────┤
                                     ▼
                              Firma Detayı        GET /ekap/contractors/<id>/
                                ├─ istatistik + dağılım grafikleri
                                ├─ "Aldığı İşler"  GET /ekap/contractors/<id>/contracts/
                                │     └─ satıra dokun → İhale Detay (ihale.ekap_id)
                                └─ ortak girişim üyeleri → başka Firma Detayı
```

Ek fikir: ihale arama ekranında `yuklenici` filtresi → "şu firmanın işleri" listesi;
kullanıcı bunu **kayıtlı filtre** olarak saklayıp o firma yeni iş aldığında bildirim
alabilir (mevcut kayıtlı filtre alarmı altyapısıyla, bkz. `mobil-bildirimler.md`).

---

## 9. Veri olgunluğu (geliştirme sırasında bilin)

Yüklenici verisi EKAP arşivinden **geriye dönük olarak doldurulmaktadır**. Bu yazının
yazıldığı sırada arşiv 5 yıldan 10 yıla genişletiliyordu ve backfill sürüyordu.

Pratik sonuçları:

- Firma sayısı ve sözleşme sayıları **günler içinde artmaya devam eder** — sabit sayılara
  göre test yazmayın.
- Bir ihalenin `contracts/` ucu bugün boş dönüp yarın dolabilir.
- `yaklasik_maliyet` kapsamı zamanla artar (Sonuç İlanları sözleşme imzalandıktan *sonra*
  yayımlanır, backend bunları 2 günde bir tazeler).
- Yeni sonuçlanan ihalelerin yüklenicisi, detay senkronu sırasında **anında** işlenir;
  ayrı bir gecikme yoktur.

Boş/eksik durumları normal karşılayın: `yuklenici: null`, `yaklasik_maliyet: null`,
`dagilim` içindeki boş diziler ve `uyeler: []` hepsi geçerli yanıtlardır.
