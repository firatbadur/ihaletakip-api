# İhale listesi sıralaması — mobil ekip notu

**Tarih:** 22 Eylül 2026 · **Uç:** `GET /api/v1/ekap/tenders/` · **Durum:** üretimde, doğrulandı

Mobil uygulamadaki "İlan Tarihi (Yeni)" / "İhale Tarihi (Yeni)" sıralamalarının hatalı
çalıştığı bildirildi. Sebep **tamamen backend'deydi**; üç ayrı hata bulundu ve düzeltildi.

> **Mobil tarafta zorunlu değişiklik YOK.** Uygulama şu an doğru parametreleri gönderiyor
> (`order=ilan_tarihi&siralamaTipi=desc`), sıralama sunucu tarafında düzeldiği için mevcut
> sürüm **güncelleme olmadan** doğru çalışır. Aşağıdaki "Önerilen" bölümü isteğe bağlıdır.

---

## 1. Ne bozuktu?

### a) "İlan Tarihi (Yeni)" ilan tarihi **boş** kayıtları gösteriyordu

PostgreSQL'de `ORDER BY ilan_tarihi DESC` varsayılan olarak **NULL'ları başa** koyar
(`NULLS FIRST`). `ilan_tarihi` kolonunun **%48,2'si boştu** (506.902 / 1.051.946) — EKAP bu
tarihi liste yanıtında hiç vermiyor, yalnızca ihale detayından geliyor.

Sonuç: kullanıcı "İlan Tarihi (Yeni)" seçtiğinde ilk **yarım milyon** kayıt tarihsiz
ihalelerdi. Listede alakasız, eski görünen ihaleler çıkıyordu — bildirilen şikâyet buydu.

**Düzeltme:** `NULLS LAST` → tarihi boş kayıtlar artık **her iki yönde de listenin sonunda**.

### b) Sıralama eşitliklerinde sayfalama kayıt tekrarlıyor/kaybediyordu

Aynı tarih damgasını yüzlerce ihale paylaşıyor: `ilan_tarihi` **saat taşımaz** (gün başı
damgası) ve tek bir günü 897 ihale paylaşıyor; `ihale_tarihi` tarafında tek saatte 420 ihale
var. İkincil sıralama anahtarı olmadığı için bu grupların iç sırası sorgu planına kalıyordu
ve **sayfa 2, sayfa 1'deki kaydı tekrar gösterebiliyordu** (sonsuz scroll'da görülür).

**Düzeltme:** her iki sıralamaya deterministik ikincil anahtar (`id`) eklendi.
Doğrulandı: 4 sıralama kombinasyonunda 8 sayfa × 25 kayıt = 200 kayıt, **0 tekrar**.

### c) Parametre yazımında tolerans yoktu

Backend ham string karşılaştırıyordu; ufak bir sapma **hata vermeden** başka bir sıralama
veriyordu:

| Gönderilen | Eski davranış | Yeni davranış |
|---|---|---|
| `order=ilanTarihi` (camelCase) | sessizce `ihale_tarihi` | `ilan_tarihi` ✅ |
| `siralamaTipi=ASC` (büyük harf) | sessizce `desc` | `asc` ✅ |

Mobil uygulama bu iki hatayı tetiklemiyordu (`api.js` → `ORDER_MAP` doğru çeviriyor), ama
tolerans yine de eklendi.

---

## 2. Parametre sözleşmesi (güncel)

```
GET /api/v1/ekap/tenders/?order=<alan>&siralamaTipi=<yön>&page=1&page_size=20
```

**`order`** — varsayılan `ihale_tarihi`
- `ihale_tarihi` · `ilan_tarihi` (önerilen yazım)
- camelCase da kabul edilir: `ihaleTarihi` · `ilanTarihi`
- büyük/küçük harf ve alt çizgi önemsizdir; **tanınmayan değer** varsayılana düşer

**`siralamaTipi`** — varsayılan `desc`
- `asc` (eş anlamlı: `ascending`, `artan`) · `desc`
- büyük/küçük harf önemsizdir; `asc` dışındaki her değer azalan sayılır

**Boş tarihli kayıtlar:** `order=ilan_tarihi` ile, ilan tarihi olmayan ihaleler `asc` ve
`desc` yönlerinin **ikisinde de sonda** döner (NULL bir tarih değildir; "en eski" de değil).

---

## 3. Yeni yanıt alanı: `ilanTarihi`

Liste yanıtındaki her kayda `ilanTarihi` eklendi. Önceden sıralama anahtarı yanıtta hiç
dönmüyordu — istemci yalnızca `ihaleTarihSaat` görebildiği için **sıralamanın doğru olup
olmadığını ekranda ayırt edemiyordu**.

```json
{
  "ikn": "2026/1701745",
  "ihaleAdi": "…",
  "ihaleTarihSaat": "13.10.2026 14:30",
  "ilanTarihi": "2026-09-22T00:00:00+00:00",
  "…": "…"
}
```

- **Format ISO 8601** (`ihaleTarihSaat` ise EKAP'ın `GG.AA.YYYY SS:dd` formatında kalır —
  bu alanın formatı değişmedi).
- **`null` olabilir** → "tarih bilinmiyor" demektir, sıfır/eski değil. Ekranda `—` gösterin.
- Ek sorgu maliyeti yok (alan zaten sıralama için çekiliyordu).

---

## 4. Önerilen (isteğe bağlı) mobil değişiklikler

Zorunlu değil; sıralama bunlar olmadan da doğru çalışıyor.

**a) Alanı mapper'a ekleyin** — `src/screens/TenderList/helpers.js` → `mapTenderItem`
şu an `ilanTarihi`yi almıyor:

```js
return {
  // …mevcut alanlar
  date: item.ihaleTarihSaat || '',
  publishDate: item.ilanTarihi || null,   // YENİ — ISO, null olabilir
};
```

**b) Sıralama ilan tarihine göre yapıldığında kartta ilan tarihini gösterin.** Kullanıcı
"İlan Tarihi (Yeni)" seçtiğinde kartta hâlâ **ihale tarihi** görüyor; sıralamanın çalıştığını
göremediği için "bozuk" izlenimi doğuyor. `SORT_OPTIONS[sortIndex].key` `ilanTarihi` ile
başlıyorsa kartta `publishDate` göstermek bu izlenimi ortadan kaldırır.

**c) `publishDate === null` durumunu ele alın** — "İlan tarihi yok" ya da `—`.

---

## 5. Doğrulama (isterseniz kendiniz çalıştırabilirsiniz)

```bash
# En yeni ilan tarihli ihaleler — bugünün ilanları dönmeli
curl -s "https://ihale-takip.envisoft.com.tr/api/v1/ekap/tenders/?order=ilan_tarihi&siralamaTipi=desc&page_size=5" \
  | python3 -m json.tool | grep -E 'ikn|ilanTarihi'

# Sayfalama bütünlüğü — tekrar eden İKN olmamalı
for p in 1 2 3 4; do
  curl -s "https://ihale-takip.envisoft.com.tr/api/v1/ekap/tenders/?order=ilan_tarihi&siralamaTipi=desc&page=$p&page_size=25"
done | grep -o '"ikn": "[^"]*"' | sort | uniq -d
```

Üretimde ölçülen yanıt süreleri: **0,29 – 0,76 sn** (sıralama artık indeksten karşılanıyor;
öncesinde bu sorgu 1 milyon satırı tarayıp sıralıyordu).

---

## 6. Veri onarımı (tamamlandı — mobil tarafı etkilemez)

Boş `ilan_tarihi` değerlerinin sebebi **Temmuz 2026'daki ilk arşiv doldurmasıydı**: o dönemde
liste senkronu, detaydan gelen ilan tarihini boş değerle eziyordu. Kök neden Ağustos 2026'da
giderildi — **yeni gelen ihalelerde bu sorun yok** (son 14 günde eklenen 2.518 ihalenin
%0'ında boş; `ihale_tarihi` ise hiçbir kayıtta boş değil).

Geçmiş kayıtlar `detail_raw` arşivinden onarıldı: **506.831 kayıt düzeltildi**, boş kalan
yalnızca **71** (1.051.946 ihalede). İlan tarihine göre sıralama artık arşivin tamamını
kapsıyor. API sözleşmesi değişmedi. `ihale_tarihi` hiçbir kayıtta boş değildi.

> ⚠️ **Doğruluk notu:** onarılan kayıtların ~%66'sında yazılan tarih gerçek **İhale İlanı**
> tarihidir. Kalan ~%34'ünde ihalenin İhale İlanı hiç yayımlanmamış (ilansız usuller) ve
> arşivdeki **en erken ilan** tarihi yazılır — bu genelde Sonuç İlanı tarihidir. Aynı kural
> yeni gelen ihalelerde de işliyor, yani arşiv ile yeni kayıtlar tutarlı. `ilanTarihi`
> alanını "ihalenin duyurulma tarihi" olarak sunarken bu yaklaşıklığı hesaba katın.

---

## Özet

| | |
|---|---|
| Mobilde zorunlu değişiklik | **Yok** — mevcut sürüm düzelmiş sıralamayı alır |
| Yeni alan | `ilanTarihi` (ISO, `null` olabilir) |
| Bozulan sözleşme | **Yok** — mevcut alanların adı/formatı değişmedi |
| Backend commit | `bb0f6d2` + `ekap/tests/test_tender_sirala.py` (12 test) |
