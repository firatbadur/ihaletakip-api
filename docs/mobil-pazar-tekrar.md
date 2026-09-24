# Pazar Panosu (sektör ekseni) + Beklenen İhaleler — mobil notu

**Kime:** IhaleTakip React Native uygulamasını geliştiren mobil ekip.
**Tarih:** 2026-09-24 · **Durum:** üretimde.

> **Kısa özet: bu sürüm için mobilde YAPILMASI ZORUNLU bir değişiklik YOK.**
> İki ekran da mevcut kodla doğru çalışır. Aşağıdakiler isteğe bağlı iyileştirmeler
> ve dikkat edilmesi gereken davranış değişiklikleridir.

---

## 1. Pazar panosu artık sektör ekseninde

`GET /ekap/market/` önceden OKAS iş gruplarını döndürüyordu. Artık **onboarding
sihirbazındaki sektörlerin aynısını** döndürüyor (`/ekap/sektorler/` ile aynı kapalı
liste).

**Neden:** OKAS kodu sözleşmelerin ancak %78,7'sinde doluydu — panonun 4. en büyük
kalemi "Sınıflandırılmamış"tı (18.635 sözleşme) ve listenin başı *"BI. ve BII. Grubu
işlerin dışındaki bina işleri"* gibi bürokratik etiketlerdi. Sektörde doluluk %98,3
ve adlar kullanıcının onboarding'de zaten seçtiği adlar.

### Ne değişmedi (kasıtlı)

| | |
|---|---|
| `is_gruplari[].okas_bucket` | **Alan adı aynı kaldı.** Artık sektör kodu taşır (`gida_catering`). Siz bunu zaten opak anahtar olarak `/ekap/market/{anahtar}/` yoluna koyuyorsunuz — davranış birebir aynı. |
| `is_gruplari[].ad` | Artık `"Gıda ve Yemek Hizmeti"` gibi okunur adlar. |
| `okas_bucket: ''` | Hâlâ "Sınıflandırılmamış" ve hâlâ yol parametresi olamaz. `isUnclassifiedBucket` kontrolünüz aynen geçerli. |
| `/ekap/market/4523/` | **Çalışmaya devam ediyor.** Eksen koddan çözülüyor, yani önbelleğinizdeki ve paylaşılmış linklerdeki eski OKAS kodları kırılmadı. |
| "Bu gruptaki ihaleleri gör" düğmesi | `okas_kod` olarak sektör kodu gönderiyorsunuz; backend rakam olmayan değeri sektör filtresine yönlendiriyor. Sonuç boş dönmez. |

### Yeni alanlar (isteğe bağlı)

```jsonc
{
  "boyut": "sektor",          // "sektor" | "okas"
  "is_gruplari": [
    { "kod": "gida_catering", // okas_bucket ile aynı değer, açık adlandırma
      "boyut": "sektor",
      "okas_bucket": "gida_catering",
      "ad": "Gıda ve Yemek Hizmeti", ... }
  ]
}
```

Yeni query param: `?boyut=okas` → eski OKAS eksenini döndürür. İstemez ve
göndermezseniz sektör gelir.

**Öneri (zorunlu değil):** ileride `okas_bucket` yerine `kod` okumaya geçin; `boyut`
alanı hangi taksonomide olduğunuzu söyler. `okas_bucket` kaldırılmayacak.

---

## 2. Beklenen ihaleler — tahminler artık dürüst

`GET /ekap/recurring/` uydurma veri üretiyordu. Sizin varsayılan parametrelerinizle
(`beklenen_gun=90`, `guven=yuksek,orta`) uç yalnızca **2 satır** döndürüyordu ve
ikisi de uydurmaydı. Düzeltme sonrası aynı sorgu **951 satır** döndürüyor.

### ⚠️ Davranış değişikliği: `beklenen_ilan_tarihi` artık `null` olabilir

Periyodu "düzensiz" çıkan seride tespit edilebilir bir tekrar aralığı yoktur ve
**tarih uydurulmaz**. Bu seriler yine listelenir (idarenin o işi tekrar tekrar
aldığı gerçek bir bilgidir), yalnızca tarihleri yoktur.

**Sizin kodunuz bunu zaten doğru ele alıyor** — `RecurringCard` `'Bilinmiyor'`
yazıyor. Değişiklik gerekmez. ⚠️ Ama `beklenen_gun` filtresi gönderdiğinizde bu
seriler zaten listeye girmez (tarihsiz kayıt tarih aralığına düşmez), yani takvim
ekranında görünmezler. İstenen davranış budur.

### ⚠️ Sıralama düzeldi

`order=beklenen` eskiden düz artan sıralıyordu ve aktiflik penceresi geriye
uzandığı için **listenin ilk sayfası en bayat tahminlerdi** (üretimde 2023-10-14 ile
başlıyordu). Artık: **önce yaklaşanlar** → sonra **gecikenler** → en son tahmin
edilemeyenler.

Ayrıca sıralamaya `pk` tie-break eklendi. Bu, `RecurringTenders/index.js` içinde
belgelediğiniz *"aynı seri iki sayfada birden düşüyor / two children with the same
key"* sorununun **kaynağını** giderir. İstemcideki tekilleştirmeyi kaldırmak
zorunda değilsiniz (zararsız bir emniyet kemeri), ama artık gerekli değil.

### Yeni alanlar

| alan | anlamı |
|---|---|
| `donem_sayisi` | **Kaç kez tekrarladı.** `ihale_sayisi`'ndan farklıdır: kısımlı bir alımın lotları ve iptal sonrası yeniden ihale **tek dönem** sayılır. Kullanıcıya "5 kez tekrarladı" demek için bunu kullanın, `ihale_sayisi`'nı değil. |
| `gecikme_gun` | Beklenen tarih geçtiyse kaç gün geçtiği, yoksa `null`. Bir arıza değil bilgidir: *"bu iş normalde Mart'ta çıkardı, 40 gün gecikti"*. |
| `sektor` / `sektor_adi` | Pazar panosu ve onboarding ile aynı taksonomi. |

Yeni filtre: `?sektor=gida_catering,ilac`

### ⚠️ `guven` rozetinin anlamı sıkılaştı

Eskiden `yuksek`, üye sayısına bakıyordu ve tek bir gözlemden üretilebiliyordu
(aynı gün yayımlanan lotlar tek aralığa düşüyor, sapma 0 çıkıyordu). Artık ölçüt
**dönem aralığı sayısıdır**: `yuksek` = en az 3 aralık (4 dönem) **ve**
sapma/medyan ≤ 0,15.

Rozet artık daha az sıklıkta `yuksek` diyor ama dediğinde **doğru diyor**. Geriye
dönük ölçüm (bir yıl önceki veriyle tahmin üretip gerçekte ne olduğuna bakarak):

| | eski | yeni |
|---|---|---|
| yüksek güvende ±30 gün isabet | %29,2 | **%39,8** |
| yüksek güvende ±90 gün isabet | %40,0 | **%53,8** |
| medyan mutlak hata | 42 gün | **30 gün** |

⚠️ **Bunu kullanıcıya "kesin tarih" gibi sunmayın.** En iyi hâlde ±30 günde 10
tahminden 4'ü tutuyor. `guven` rozeti ve `periyot_gun ± sapma_gun` gösterimi
zorunludur — mevcut kartınız bunu zaten doğru yapıyor.

⚠️ Yüksek güvenli tahminlerin **%42'sinde ertesi yıl o seriden hiç ihale çıkmıyor**
(seri bitmiş ya da gruplama kaçırmış olabiliyor). Yani "beklenen" bir söz değil,
bir olasılıktır; metinde "bekleniyor" yerine "tahmin" dili daha dürüst olur.
