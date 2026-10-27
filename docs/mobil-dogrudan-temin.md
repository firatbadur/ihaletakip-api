# Doğrudan Temin — Mobil Entegrasyon Notu

Uygulamaya yeni bir bölüm: **doğrudan temin (DT) alımları**. Listeleme, detay, doküman
indirme, kayıtlı filtre ve (Pro) anında bildirim.

DT'ler normal ihaleden farklıdır: bir kısmı yayımlandıktan **birkaç saat sonra** kapanır.
Bu yüzden liste yalnızca **şu an teklif verilebilir** alımları gösterir ve alarm bildirimi
günlük özet değil, **anlık**tır (yeni ilanlar mesai içinde 5 dakikada bir yoklanıyor).

> **Durum:** Üretimde **açık** (2026-10-27). Açılıştan sonraki ilk ~40 dakikada liste
> kademeli dolar: önce o günün ilanları, ardından önceki günlerin hâlâ açık olanları
> (tam tarama turda bir sayfa ilerler).
>
> **Hacim (ölçüldü, Salı 2026-10-27):** günde ~900 yeni ilan; aynı anda açık **≥ 1.500**
> alım. Liste ucu sayfalıdır — sonsuz kaydırma ile `page` artırarak çekin, tek
> istekte hepsini almaya çalışmayın (`page_size` en çok 100).

Kök: `https://ihale-takip.envisoft.com.tr/api/v1` · Tüm yanıtlar `{success, message, data}`
zarfındadır. Alan adları **snake_case Türkçe**dir (ihale uçlarındaki EKAP camelCase değil).

---

## 1. Liste — `GET /ekap/dogrudan-temin/`

Giriş gerekmez (kayıtlı filtre parametresi hariç).

| Parametre | Açıklama |
|---|---|
| `q` | Alım adı / idare adı (Türkçe-i güvenli, "ışık" = "ISIK") |
| `il_id` | İl id'leri, virgülle — **`/ekap/cities/`'teki `ekap_il_id`**, ihale filtresiyle aynı seçici kullanılabilir |
| `tur` | 1 Mal · 2 Yapım · 3 Hizmet · 4 Danışmanlık (virgülle) |
| `madde_kod` | 4734/22 maddesi (virgülle) — seçenekler `/ekap/dogrudan-temin/filtreler/` |
| `idare_detsis` | DETSIS düğümleri (virgülle), alt birimler dahil — mevcut "İdare Seç" ekranı aynen kullanılır |
| `son_teklif_min`, `son_teklif_max` | ISO damga ya da `YYYY-MM-DD` (gün verilirse gün sonu dahil) |
| `dokuman_var` | `true` → yalnızca dokümanı olanlar |
| `dtn` | DTN listesi (virgülle) — **bildirim derin bağlantısı** (§5) |
| `kayitli_filtre` | DT kayıtlı filtresi id'si — **giriş gerekir** (§4) |
| `order` | `son_teklif` (vars.: en acil önce, tarihsizler sonda) · `yeni` (en son yayımlanan önce) |
| `page`, `page_size` | sayfa (1'den), boyut (vars. 20, en çok 100) |

Yanıt:

```json
{
  "list": [
    {
      "dtn": "26DT2112816",
      "ad": "8 Kalem İlaç Alımı",
      "idare_adi": "BURSA İL SAĞLIK MÜDÜRLÜĞÜ BURSA GÜRSU CÜNEYT YILDIZ DEVLET HASTANESİ BAŞHEKİMLİĞİ",
      "idare_id": "",
      "il_adi": "BURSA",
      "il_id": 265,
      "tur": 1,
      "tur_adi": "Mal",
      "madde_kod": 104,
      "madde": "22-d* (Parasal Limit Kapsamında)",
      "durum": 202,
      "durum_metin": "Doğrudan Temin Duyurusu Yayımlanmış",
      "son_teklif": "2026-10-27T16:00:00+03:00",
      "kalan_dakika": 2440,
      "ilan_tarihi": "2026-10-25",
      "ilanli": true,
      "dokuman_var": true,
      "dokuman_url": "https://…/api/v1/ekap/dogrudan-temin/26DT2112816/dokuman/"
    }
  ],
  "totalCount": 412,
  "page": 1
}
```

- ⚠️ **`kalan_dakika` sunucu saatindendir** — geri sayımı bununla başlatın, cihaz saatiyle
  hesaplamayın (saati yanlış cihazlar). Liste her açılışta tazelenmeli: kayıt kapanınca
  listeden düşer.
- `son_teklif` / `kalan_dakika` **`null` olabilir** (EKAP "Tarih Belirtilmemiş" diyor) —
  uydurma bir tarih göstermeyin, "Tarih belirtilmemiş" yazın.
- `idare_id` boş olabilir (mobil kaynakta idare kimliği yok, adla eşleştiriliyor).

## 2. Detay — `GET /ekap/dogrudan-temin/{dtn}/`

⚠️⚠️ **Bu uç ilk açılışta EKAP'a gider** (ihale detayından farklı: ihale detayı önceden
toplanmış veriden anında döner). Aynı alım 30 dk içinde tekrar açılırsa — başka bir
kullanıcı açmış olsa bile — önbellekten anında döner. İlk açılış tipik olarak birkaç
saniye, kötü durumda (EKAP doğrulama istediğinde) 15–20 saniye sürebilir.
**Önerilen ekran akışı:**
1. Listedeki satırda zaten olan alanlarla (`ad`, `idare_adi`, `il_adi`, `son_teklif`,
   `kalan_dakika`, `tur_adi`, `madde`, `dokuman_url`) detay ekranını **hemen** açın.
2. Detay ucunu arka planda çağırın; yalnızca "Ayrıntılar" bölümünde (duyuru metni,
   iletişim, idare hiyerarşisi, birim fiyat cetveli) yükleniyor göstergesi gösterin.
3. Zaman aşımını en az **30 sn** tutun; `detay: null` + `mesaj` gelirse bölümde mesajı
   gösterin, ekranı hataya düşürmeyin.

Bildirimden (`dtn_liste` tek kayıt) gelindiğinde listede satır yoktur → ekranı detay
ucunun cevabıyla açın (liste alanları cevabın içinde de var).

Liste alanları **+**:

```json
{
  "detay": {
    "kapsam": "4734 Kapsamında",
    "ilan_sekli": "Doğrudan Temin İlanı",
    "kismi_teklif": "Verilemez",
    "kisim_sayisi": null,
    "istisna_dayanagi": "",
    "mevzuat_dayanagi": "",
    "iptal_nedeni": "",
    "iptal_tarihi": "",
    "idare": {"en_ust": "SAĞLIK BAKANLIĞI", "ust": "BAKAN YARDIMCILIKLARI",
              "ad": "BURSA İL SAĞLIK MÜDÜRLÜĞÜ …", "il": "BURSA"},
    "ilanlar": [
      {"tur": "dogrudan_temin", "tarih": "25.10.2026", "html": "<center><b>8 KALEM İLAÇ ALIMI</b>…"}
    ],
    "sozlesmeler": []
  },
  "birim_fiyat_cetveli_url": "https://ekap.kik.gov.tr/EKAP/YeniIhaleArama.aspx?qs=1&birimFiyatCetvelIndir=1&dogrudanTeminId=…",
  "mesaj": ""
}
```

- `detay` EKAP'tan **o an** alınır (30 dk önbellek). EKAP yanıt vermezse `detay: null` ve
  `mesaj` dolu gelir; liste alanları yine gelir → ekran açılmalı, ayrıntı bölümünde
  `mesaj` gösterilmeli. Hata ekranına düşmeyin.
- `ilanlar[].html` duyuru metnidir (idare adresi, telefon, e-posta, teklifin verileceği
  yer, son tarih). WebView'de gösterin; HTML olduğu gibi gelir (Türkçe karakterler
  çözülmüş).
- `birim_fiyat_cetveli_url` **EKAP'ın kendi adresidir**, dosya bizim sunucumuzdan geçmez:
  tarayıcıda / WebView'de açın (bir .docx iner). `null` olabilir.

## 3. Doküman — `GET /ekap/dogrudan-temin/{dtn}/dokuman/`

Yalnızca `dokuman_var: true` iken düğme gösterin; URL listede `dokuman_url` olarak hazır gelir.

- ⚠️ **Tek dosyadır** (ihale dokümanındaki liste ekranı yok) ve **biçimi serbesttir**:
  idarenin yüklediği dosya ne ise o (RAR, ZIP, PDF, DOCX… — ölçülen ilk örnek bir RAR).
  Dosya adı `Content-Disposition`'da, tür `Content-Type`'tadır. Önizleme yapmayın;
  dosyayı kaydedip "Paylaş / Dosyalarda aç" ile verin.
- Yanıtlar:

| Durum | Anlamı | Ne yapılmalı |
|---|---|---|
| 200 | dosya akıyor | kaydet / paylaş |
| 404 | "EKAP'ta yayımlanmış doküman yok" — hata DEĞİL | mesajı göster |
| 503 | kota doldu / EKAP doğrulama istedi / EKAP yanıtsız | `message`'ı göster, "tekrar dene" |
| 502 | beklenmeyen hata | genel hata |

## 4. Kayıtlı filtre — `/dogrudan-temin/kayitli-filtreler/`

⚠️ Bu **ihale kayıtlı filtrelerinden (`/saved-filters/`) ayrı** bir uçtur. DT filtrelerini
`saved-filters`'a yazmayın: alan adları farklıdır ve ihale aramasında yok sayılırlardı.

| Uç | Açıklama |
|---|---|
| `GET /dogrudan-temin/kayitli-filtreler/` | kullanıcının DT filtreleri |
| `POST /dogrudan-temin/kayitli-filtreler/` | kaydet |
| `GET/PUT/PATCH/DELETE /dogrudan-temin/kayitli-filtreler/{id}/` | getir / değiştir / sil |

```json
{ "ad": "Diyarbakır mazot", "filters": {"q": "mazot", "il_id": [270], "tur": [1]}, "alarm": true }
```

- `filters` anahtarları **yalnızca**: `q`, `il_id`, `tur`, `madde_kod`, `idare_detsis`,
  `son_teklif_min`, `son_teklif_max`, `dokuman_var`. ⚠️ Tanınmayan anahtar **400** döner
  (yok saymak filtreyi sessizce genişletirdi) — bir yazım hatası bu yüzden görünür olur.
- Filtreyi listede açmak: `GET /ekap/dogrudan-temin/?kayitli_filtre={id}` (giriş gerekir;
  anonim istek **400**). Ek parametrelerle birleştirilebilir (`&order=yeni`).

### Free / Pro

| İşlem | Free | Pro |
|---|---|---|
| Listeleme, detay, doküman, filtreleme | ✅ | ✅ |
| Filtre kaydetme (`alarm: false`) | ✅ sınırsız | ✅ |
| `alarm: true` ile oluşturma / güncelleme | ❌ **403 `premium_required`** | ✅ |
| Alarmlı filtrede ad/filtre değiştirme (alarma dokunmadan) | ✅ | ✅ |

403'te `errors.code == "premium_required"` → mevcut paywall akışı. Pro'dan düşen üyenin
alarmlı filtresi silinmez ama bildirim üretmez.

## 5. Bildirim (Pro alarmı)

Filtreye uyan **yeni** bir DT yayımlanınca — yeni ilanlar mesai içinde 5 dk'da bir
yoklanıyor — kullanıcı başına **tek** bildirim gider (birden çok filtreye uyan alım bir kez
sayılır). İki DT bildirimi arasında en az 15 dk; sessiz saatte (22–07) gönderilmez,
sabah ilk turda gelir.

### Uygulama-içi (`GET /api/v1/notifications/`)

```json
{
  "id": 812,
  "type": "tender",
  "title": "Doğrudan Temin: 3 Yeni Alım",
  "body": "Filtrelerinize uyan 3 doğrudan temin yayımlandı; en erken bugün 14:30'da kapanıyor.",
  "dtn_liste": "26DT2112805,26DT2112816,26DT2112913",
  "filter_id": null, "filtre_idler": "", "tender_ikn": null
}
```

### Push (FCM `data`, her değer string)

```json
{ "type": "tender", "dtnListe": "26DT2112805,26DT2112816,26DT2112913" }
```

### Tıklanınca

`dtn_liste` / `dtnListe` doluysa → **DT listesi**: `GET /ekap/dogrudan-temin/?dtn=<dtn_liste>`.
Tek DTN varsa doğrudan detay (`/ekap/dogrudan-temin/{dtn}/`) açılabilir.

Derin bağlantı önceliği (yeni halka ikinci sırada):
`conversation_id` > **`dtn_liste`** > `filter_id` > `filtre_idler` > `authority_detsis` >
`idare_detsis_liste` > `contractor_id` > `okas_kodlar` > `tender_ikn`/`tender_id`.

⚠️ **`mapNotification` sabit bir alan listesidir** (`firestoreApi.js`): `dtn_liste` oraya
eklenmezse alan uygulamaya **hiç ulaşmaz**, hata da vermez (2026-09-28'de `pencere_bas`
ile canlı yaşandı — push tıklaması çalışıp uygulama-içi liste yanlış ekranı açıyordu).
⚠️ Arama parametrelerini kuran yardımcı bir izin listesiyse (`toEkapQuery` gibi) `dtn`
ve `kayitli_filtre` oraya da eklenmeli; aksi hâlde istek **filtresiz** gider ve tüm açık
alımlar listelenir.
⚠️ Bildirimdeki sayı = `dtn_liste` uzunluğu. Bildirim açıldığında bazıları **kapanmış**
olabilir (liste yalnızca açıkları döndürür) → liste kısa gelirse "N alımdan M'si hâlâ
açık" gibi bir not uygundur; bu hata değildir.

## 6. Filtre seçenekleri — `GET /ekap/dogrudan-temin/filtreler/`

```json
{
  "turler":   [{"kod": 1, "ad": "Mal"}, {"kod": 2, "ad": "Yapım"}, …],
  "maddeler": [{"kod": 104, "ad": "22-d* (Parasal Limit Kapsamında)"}, …],
  "siralamalar": [{"kod": "son_teklif", "ad": "Son teklif tarihi (en yakın)"},
                  {"kod": "yeni", "ad": "En yeni ilan"}]
}
```

Sabit veridir (EKAP'a gitmez); önbelleğe alınabilir.

## 7. Boş ve hata durumları

| Durum | Gösterim |
|---|---|
| Liste boş (`totalCount: 0`) | "Şu an açık doğrudan temin yok" (filtre çok darsa da olur) |
| Hafta sonu / gece | Az kayıt normaldir (EKAP'a hafta sonu az ilan düşüyor) |
| Detayda `detay: null` | Liste alanlarıyla ekranı aç, ayrıntıda `mesaj` |
| Doküman 404 | "Bu alım için EKAP'ta doküman yok" (bilgi, hata değil) |

## 8. Metinler

| Durum | Başlık | Gövde |
|---|---|---|
| 1 alım | Doğrudan Temin: Yeni Alım | Filtrenize uyan bir doğrudan temin yayımlandı; en erken bugün 14:30'da kapanıyor. |
| N alım | Doğrudan Temin: N Yeni Alım | Filtrelerinize uyan N doğrudan temin yayımlandı; en erken 27.10 16:00'da kapanıyor. |
| Free alarm denemesi (403) | — | Doğrudan temin alarmları Pro üyelere özeldir. Filtrenizi kaydedebilirsiniz; yeni alımlar için anında bildirim almak için Pro'ya geçin. |
