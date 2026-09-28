# Birleşik Bildirim — Mobil Entegrasyon Notu

Kayıtlı filtre ve favori idare bildirimleri artık **kullanıcı başına tek** satır + tek
push üretiyor. Bildirime basınca o kullanıcının **tüm** filtrelerine (ya da tüm favori
idarelerine) uyan ihalelerin **birleşik** listesi açılıyor.

Öncesi: 8 filtresi eşleşen kullanıcı 8 ayrı bildirim alıyordu (ölçüm 2026-09-23: tek
kullanıcı bir günde 18 filtre bildirimi, 6 filtre aynı gün 2 kez).

> **Durum:** Backend hazır ve dağıtıldı, ama `NOTIF_BIRLESIK_BILDIRIM` **kapalı** →
> bildirimler bugünkü gibi abonelik-başına gitmeye devam ediyor. Bayrak, mağaza sürümü
> yayıldıktan sonra açılacak. Yeni alanlar o ana kadar boş gelir; uygulama bugünden
> hazır olabilir.

---

## 1. Bildirim yükü

### Uygulama-içi (`GET /api/v1/notifications/`)

```json
{
  "id": 501,
  "type": "tender",
  "title": "Size Uygun İhaleler",
  "body": "Kayıtlı filtrelerinize uygun son 24 saatte 47 ihale yayımlandı.",

  "filtre_idler": "62,129,132",
  "idare_detsis_liste": "",

  "pencere_bas": "2026-09-27T08:00:00+03:00",
  "pencere_bit": "2026-09-28T08:00:00+03:00",

  "filter_id": null,
  "authority_detsis": null
}
```

İdare özetinde tersi: `idare_detsis_liste` dolu (`"24308110,19254760"`), `filtre_idler` boş.

⚠️ **`filter_id` ve `authority_detsis` bilerek `null`.** Birleşik bir bildirimde
doldurulsalardı, eski uygulama sürümü *birleşimin sayısını* gösterip *tek aboneliğin
listesini* açardı — yani "bildirimdeki sayı ekrandaki listeyle tutmuyor" hatası geri
gelirdi. Yeni alanları tanımayan sürümler bu sayede bildirim ekranına düşer.

### Push (FCM `data`)

```json
{
  "type": "tender",
  "filtreIdler": "62,129,132",
  "pencereBas": "2026-09-27T08:00:00+03:00",
  "pencereBit": "2026-09-28T08:00:00+03:00"
}
```

İdare özetinde `idareDetsisListe`. FCM her değeri **string** yollar.

---

## 2. Tıklamada ne yapılacak

Yönlendirme zincirine iki yeni halka, **`filterId` / `authorityDetsis`'ten ÖNCE**:

```
teaser > conversation > filtreIdler > idareDetsisListe > filterId > authorityDetsis > ...
```

⚠️ Sıra önemli: geçiş sürecinde iki alan birden gelirse eski halka kazanır ve yanlış
liste açılır.

### Filtre özeti

```
GET /api/v1/ekap/tenders/
      ?kayitli_filtreler=62,129,132
      &created_at_min=2026-09-27T08:00:00+03:00
      &created_at_max=2026-09-28T08:00:00+03:00
```

### İdare özeti

```
GET /api/v1/ekap/tenders/
      ?favori_idareler=24308110,19254760
      &created_at_min=...&created_at_max=...
```

Yanıt **normal ihale listesi**: `{list, totalCount, page}`. Sayfalama, sıralama ve
`totalCount` aynı sözleşmeyle çalışır — özel bir şey yok.

⚠️⚠️ **Başka hiçbir parametre gönderilmeyecek.** Özellikle `ihale_durum` ve
`teklif_verilebilir` **eklenmemeli**: "katılıma açık + teklifi geçmemiş" ölçütünü sunucu
uyguluyor. İstemci de uygulamaya kalkarsa ölçüt iki yerde yaşar, zamanla ayrışır ve
bildirimdeki sayı ekrandaki listeden sapar.

⚠️ `created_at_min/max` **tam ISO damgadır**, gün anahtarı değil → güne yuvarlanmamalı
(`dayStart`/`dayEnd` gibi bir dönüşümden geçirilmemeli). Yuvarlamak 24 saatlik pencereyi
~48 saate açar. Backend'in verdiği dizge aynen geçirilir.

⚠️ Pencere gelmezse istek yine çalışır; backend son 24 saate düşer.

---

## 3. Dikkat edilecek üç nokta

**a) Alan eşleyicisi.** Bildirim satırını camelCase'e çeviren eşleyici sabit bir alan
listesiyse, yeni alanlar oraya **eklenmeden** uygulamaya hiç ulaşmaz — ve hata da vermez.
Bu tam olarak 2026-09-28'de yaşandı: `pencere_bas`/`pencere_bit` eşlenmediği için
uygulama-içi tıklama pencereyi kaybediyor, liste yanlış günü açıyordu. Push tıklaması
doğru çalıştığı için haftalarca görünmedi.

**b) Sorgu kurucusu izin listesiyse.** `kayitli_filtreler` / `favori_idareler` oraya
eklenmezse yönlendirme doğru çalışır, **istek yanlış gider** ve liste filtrenin tüm
geçmişini açar. Sessiz bir hata: ekranda "çok fazla sonuç" görünür, sebebi belli olmaz.

**c) "Bu filtreyi kaydet".** Bildirimden açılan listede pencere ve id listesi geçici
parametrelerdir; kaydedilebilir filtreye girmemeleri gerekir. Girerse kullanıcı 24 saatlik
bir pencereyi kalıcı filtre olarak kaydeder, `kayitli_filtreler` ise kendine referans veren
anlamsız bir filtre doğurur.

---

## 4. Uçların davranışı

| Durum | Yanıt |
|---|---|
| Giriş yapılmamış + `kayitli_filtreler` | **400** (`success:false`) |
| Başkasına ait / silinmiş filtre id'si | id sessizce düşer |
| Hiçbir id çözülemedi | `totalCount: 0` + `data.uyari` — **tüm ihaleler dönmez** |
| Filtrenin JSON'unda Pro kriteri var, kullanıcı Free | **403**, `errors.code = "premium_required"` |
| En çok filtre | 50 |

⚠️ 400 seçimi bilinçli: **401 dönülmüyor**, çünkü uygulamanın yenileme akışı 401'de
oturumu temizleyebiliyor — bir parametre hatası kullanıcıyı çıkış yaptırmamalı.
`favori_idareler` için giriş gerekmez (DETSİS kodları herkese açık).

---

## 5. Metinler

| | Başlık | Gövde |
|---|---|---|
| Filtre özeti | `Size Uygun İhaleler` | `Kayıtlı filtrelerinize uygun son 24 saatte 47 ihale yayımlandı.` |
| İdare özeti | `Takip Ettiğiniz İdareler` | `Takip ettiğiniz idareler son 24 saatte 12 ihale yayımladı.` |

Filtre ya da idare **adı geçmiyor** — ürün kararı.

Sayı, pencerede eşleşen ihalelerin **tekilleştirilmiş** toplamıdır: iki filtreye uyan
ihale bir sayılır. "Sana yeni olanlar" değil, o pencerenin toplamı — ekranda açılan liste
birebir bu sayıyı gösterir.

---

## 6. Pencere neden "kayıt tarihi"

Pencere `ilan_tarihi` (EKAP yayım damgası) değil **`created_at`** (ihalenin sistemimize
girdiği an) üzerinden kuruluyor.

Sebep ölçüldü: EKAP hafta sonu yayın yapmıyor (26-27 Eylül'de yayım tarihi taşıyan sıfır
ihale), ama kayıtlar her gün gece yarısını hemen geçince düşüyor — 28 Eylül Pazartesi:
177 kayıt, tamamı 02:13'ten önce. Yayım gününe bakan pencere pazar ve pazartesi sabahları
**yapısal olarak boş** kalıyordu; kullanıcı "sabah 8 oldu bildirim gelmedi" diye bildirdi.

Bu yüzden gövdedeki ifade de "dün" değil **"son 24 saatte"**: pencere takvim gününe
oturmuyor.
