# İhale Asistanı — cevap geri bildirimi (👍/👎) ve 2026-11-04 değişiklikleri

**Durum:** backend hazır ve üretimde (2026-11-04). Mobilde **tek zorunlu iş yok**:
yapılmazsa uygulama bugünkü gibi çalışır. Yalnızca 👍/👎 düğmeleri yeni bir mobil
geliştirme ister; diğer maddeler bilgi içindir.

## Neden

Üretimdeki 445 asistan mesajı okundu. Cevapların %11,2'si "bulamadım / göremiyorum"
diyordu. Bunları bulmak için sohbetleri tek tek okumak gerekti, çünkü kullanıcının
"bu cevap işe yaramadı" diyebileceği bir yer yoktu. Geri bildirim düğmesi bu boşluğu
kapatır: olumsuz cevaplar admin panelinde ve `asistan_rapor` komutunda doğrudan listelenir.

---

## 1. Geri bildirim düğmeleri (mobil iş)

### Uç

`POST /api/v1/assistant/messages/<id>/feedback/` · JWT gerekli

```json
{ "deger": -1, "aciklama": "Kazanan firmayı bulamadı" }
```

| alan | tip | anlam |
|---|---|---|
| `deger` | `1` · `-1` · `0` | 👍 · 👎 · puanı geri al |
| `aciklama` | string, isteğe bağlı, ≤500 | Kullanıcının kısa notu. `deger=0` gelince silinir. |

Yanıt (global zarf içinde):

```json
{ "success": true, "message": "", "data": { "id": 1234, "geri_bildirim": -1 } }
```

| durum | ne zaman |
|---|---|
| `200` | yazıldı. Aynı mesaja tekrar çağrı son değeri yazar (idempotent). |
| `400` | `deger` 1/-1/0 dışında |
| `404` | mesaj yok, başkasının, ya da **kullanıcı mesajı** (yalnızca asistan cevabı puanlanır) |

### Seçili durumu geri yükleme

Mesaj nesnesine **`geri_bildirim`** alanı eklendi (`1`, `-1` ya da `null`). Şu uçlarda gelir:

- `GET /assistant/conversations/<id>/` (mesaj listesi)
- `GET /assistant/messages/`
- `GET /ai/tasks/<task_id>` → sohbet cevabı (`analysis` içindeki mesaj)

Alan **additive**: eski sürümler yok sayar.

### Önerilen arayüz

- Yalnızca `role === 'assistant'` balonlarında, balonun altında iki küçük simge
  (`thumb-up-outline` / `thumb-down-outline`). Seçili olan dolu simgeyle gösterilir.
- 👎'ye basınca isteğe bağlı kısa bir not alanı açılabilir ("Ne eksikti?"). Not boş
  geçilebilir — düğme tek dokunuşla da çalışmalı, yoksa kimse basmaz.
- Seçili olana tekrar basmak puanı geri alır → `deger: 0`.
- İstek arka planda gitsin, hata olursa sessizce eski hâline dönsün (bu bir ölçüm
  aracı; kullanıcıya hata mesajı göstermeye değmez).
- ⚠️ `id`'si **sunucudan gelmiş** mesajlarda göster. Gönderim sırasında istemcinin
  oluşturduğu geçici mesajlarda (iyimser ekleme) id yoktur; o mesaja basılırsa 404 döner.
- Uzun basma menüsü (`MessageActions.js`: kopyala / paylaş / sesli oku) yerine
  balonun altına koymak önerilir: menüde kimse aramaz.

API istemcisi (`src/api/assistant.js`):

```js
export const sendMessageFeedback = (messageId, deger, aciklama = '') =>
  apiPost(`/assistant/messages/${messageId}/feedback/`, {deger, aciklama});
```

---

## 2. Bilgi: değişen davranışlar (mobil değişiklik GEREKMEZ)

### Onay kartında alarm metni

Asistanın önerdiği alarm artık **"ihale sonuçlanınca" bildirimini varsayılan açık**
kuruyor. Eskiden bu bildirim kapalı kuruluyordu ve asistan kullanıcıya "sonuçlanınca
haber alırsın" diyordu; bu doğru değildi. Kartın `ozet` metni artık neyin açık olduğunu
yazıyor:

> “Afyonkarahisar Emirdağ 104 Adet İşyeri…” için alarm kurayım mı? (ihale günü, sonuçlanınca bildirimi)

Model isterse "KİK kurul kararı" bildirimini de açabiliyor (`kurul_karar`). Kart yine
`type: "action"`, `tur: "alarm_kur"` — `ActionCard.js` değişmeden basar.

### Bir cevapta en çok 3 onay kartı

Sınır eskiden de 3'tü, ama aşıldığında asistan "11 belediye için kart çıkardım" diyor,
kullanıcı 3 kart görüyordu. Artık model sınırı biliyor ve "kalanlar için devam
edelim" diyor. `payload.blocks` içinde en çok 3 `action` bloğu gelir — bu değişmedi.

### Doğrudan temin cevapları kart DEĞİL, metin

Asistan artık doğrudan temin alımlarını arayabiliyor (eskiden "doğrudan temin
ilansızdır, sistemde yok" diyordu). Sonuçlar **metin içinde DTN numarasıyla** gelir
(ör. `26DT2104455`). `tender_cards` ihale kartıdır ve DT için kullanılmaz.

İleride istenirse: metindeki DTN'leri dokunulabilir yapıp `DirectProcurementDetail`
ekranına (`dtn` parametresi) açmak mümkün. Bugün backend ayrı bir DT kart bloğu
üretmiyor; istenirse `payload.blocks`'a `{type: "dt_card", dtn, ad, son_teklif}`
biçiminde eklenebilir — önce mobil ekiple biçim konuşulmalı.

### Daha çok ihale kartı görülebilir

- "Bu işi kim aldı" cevapları kazanan firmayı artık doğru veriyor.
- "Geçen yıl yapıldı mı" cevaplarında geçen yılın ihalesi de kart olarak gelebilir.
- Yeni sohbette, kullanıcının son 48 saatte konuştuğu ihaleler asistanın aklında
  kalıyor ("az önceki ihale", "onu kaydet").

Kart biçimi aynı (`tender_cards`).

---

## 3. Uygulama içi yönlendirmeler (asistan artık bunları söylüyor)

Asistan, kullanıcıya şu ekranları **aşağıdaki adlarla** tarif ediyor. Ekran ya da menü
adı değişirse backend'e haber verin (`assistant/prompts.py` `_URUN_VE_EKAP`), yoksa
asistan var olmayan bir yeri tarif eder:

| asistanın söylediği | mobildeki karşılığı |
|---|---|
| alt menüde "Doğrudan" sekmesi | `DirectProcurementList` (sekme etiketi "Doğrudan") |
| yan menü → "KİK Kurul Kararları" | `KurulKararlari` |
| Asistan ekranının üstündeki firma adı (kalem simgesi) | `Onboarding {mode:'company'}` |
| Asistan ekranındaki saat simgesi (geçmiş sohbetler) | `AssistantHistory` |
| İhale detayı → İhale Bilgileri sekmesi (idareyi favoriye ekleme) | `IhaleBilgileriTab` |
| "Kayıtlı İdareler" listesinde arama kutusu **yok** | `SavedAuthorities` |
| yan menü → "Yardım & Destek" | `Support` |
| giriş ekranında "Şifremi unuttum" | `Login` → `EmailAuthFlow` |
| bilgisayardan: ihaletakip.com, aynı hesap | web portalı |

⚠️ Bir kullanıcı "Kayıtlı İdareler'de idare ara yok" diye yakındı ve haklıydı; asistan
orayı tarif ediyordu. Kayıtlı İdareler ekranına arama/ekleme düğmesi koymak bu
şikâyeti ürün tarafında çözer — öneri olarak not düşüldü.

---

## 4. Açık kalan konu: doküman soruları

Kullanıcılar asistana sık sık doküman içeriğini soruyor: kaç adet, iş kalemleri,
ödeme kaç günde, iş süresi, kısmi teklif. Asistan yalnızca **daha önce yapılmış**
şartname analizini okuyabiliyor; analiz ise dosyanın **mobilden** gönderilmesini
bekliyor (`POST /ai/analyze`, `file_base64`).

Önerilen çözüm (mobil + backend, henüz yapılmadı): asistan cevabında
`{type: "dokuman_ac", ekap_id, analiz_turu}` bloğu → dokununca `DocumentExplorer`
açılır ve seçilen dosyanın analizi başlatılır. Analiz bittikten sonra kullanıcı
aynı soruyu sorduğunda asistan cevabı analizden verir. Biçim konuşulmadan backend
bu bloğu üretmeyecek.

Analiz şablonlarına bu sorular için bölümler zaten eklendi (kalemler ve miktarlar,
kısmi teklif, ödeme ve avans) — yeni yapılan analizlerde görünür.
