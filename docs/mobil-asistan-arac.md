# Mobil Entegrasyon — İhale Asistanı (araç kullanan sürüm)

Bu doküman, İhale Asistanı'nın **araç kullanan (tool calling)** sürümüne geçişte mobil
tarafta ne değiştiğini, neyin **değişmediğini** ve ne yapılması gerektiğini anlatır.

> Taban URL: `https://ihale-takip.envisoft.com.tr/api/v1/`
> Kaynak: `assistant/` app'i; `docs/openapi.yaml` ve `docs/postman_collection.json`
> her zaman günceldir.

---

## 0. Bir cümlede ne oldu

Asistan eskiden mesajı **anahtar kelimelere** göre dallara ayırıyor ve LLM'e yalnızca
prompt'a önceden konmuş bağlamı veriyordu; veritabanına erişemiyordu. Artık modele
**8 araç** verildi (ihale arama, ihale detayı, OKAS arama, idare raporu, firma arama /
profili / işleri, kullanıcının kendi kayıtları) ve model hangi aracı çağıracağına kendisi
karar veriyor. "Şu idare kimlere iş vermiş?", "otomasyon ihalelerini getir", "bu firma en
son ne iş almış?" gibi sorular artık gerçek veriyle yanıtlanıyor.

---

## 1. ⚠️ ÖNCE BUNLARI OKUYUN

### 1.1 Yanıt süresi arttı — 5-10 sn değil, 10-60 sn

Model birden çok **tur** atabiliyor (araç çağır → sonucu oku → gerekirse tekrar çağır →
yaz). Sunucu bütçe zinciri:

```
model turu ~40 sn  <  wall 180 sn  <  soft_time_limit 240 sn  <  hard limit 300 sn
```

**İstemci polling zaman aşımı 240 sn olmalıdır.** 150 sn'de pes eden bir istemcide
kullanıcı "zaman aşımı" görür, ama görev sunucuda koşmaya devam eder ve yanıt sessizce
veritabanına yazılır — kullanıcı ancak sohbeti yeniden açınca görür. Sessiz veri kaybı
değil ama **sessiz kafa karışıklığı**.

⚠️ Doküman analizinin (`analyzeDocument`) zaman aşımını **büyütmeyin**; o ayrı bir
bütçeye sahip. Asistan için ayrı bir sabit kullanın.

### 1.2 Markdown YOK — renderer eklemeyin

Model'e sistem prompt'unda `**`, `#`, `` ` ``, tablo ve `1.`/`-` liste kullanması
**yasaklandı**; madde gerekiyorsa `•`, başlık gerekiyorsa "Metin:" kullanıyor. Sebep:
mesaj balonu düz `<Text>` basıyor ve markdown ham karakter olarak görünüyordu.

Bu bir ürün kararıdır. Markdown renderer eklemeyin — eklerseniz prompt'taki yasak
gereksiz bir kısıtlamaya dönüşür ve iki taraf çelişir. Zengin içerik **blok** olarak
gelecek (§4).

### 1.3 Kartlar artık araç sonuçlarından geliyor

Model kart gösterilecek ihaleyi uyduramaz: kartlar yalnızca bir aracın **gerçekten
döndürdüğü** İKN'lerden üretilir (sunucuda "kart havuzu" deseni). Mobil tarafta
değişiklik gerekmez — ama bir kartın alanı boşsa bu veri eksikliğidir, hata değildir.

---

## 2. Sözleşme: ne DEĞİŞMEDİ

Bu bölüm bilerek uzun — geriye uyumluluk garantileri burada.

| Şey | Durum |
|---|---|
| `POST /assistant/chat/` gövdesi ve `202 {task_id, conversation_id}` | **Aynı** |
| `GET /ai/tasks/{task_id}/` durum akışı ve `analysis` alanı | **Aynı** |
| `analysis` = serileştirilmiş ChatMessage (`id, conversation, role, content, payload, created_at`) | **Aynı** |
| `payload.tender_cards` ve kart alanları | **Aynı** |
| Sohbet oturumu uçları (`conversations/`, `conversations/{id}/`) | **Aynı** |
| Pro kapısı: sohbet `403 premium_required` | **Aynı** |

**Yani mevcut uygulama, tek satır değişiklik yapılmadan da çalışır** — yalnızca §1.1'deki
zaman aşımı riskiyle. Değişiklikler iyileştirmedir, zorunluluk değil (biri hariç).

---

## 3. Akış

```
POST /assistant/chat/
  { "message": "otomasyon ihalelerini getir",
    "conversation": 71,          // ilk mesajda gönderilmez, yanıttan alınır
    "tender": "2026/1473120" }   // opsiyonel: ihale odaklı YENİ sohbet açar
  → 202 { "task_id": "...", "conversation_id": 71 }

GET /ai/tasks/{task_id}/   (2 sn aralıkla, EN FAZLA 240 sn)
  → { "status": "pending" | "processing" }            // devam
  → { "status": "completed", "analysis": {...}, "usage": {...} }
  → { "status": "failed" }                            // HTTP 500
```

`analysis` doğrudan mesaj listesine eklenebilir.

---

## 4. `payload` şeması

### 4.1 Bugün gelen

```json
{
  "kind": "text",
  "tender_cards": [
    {
      "ikn": "2026/1473120",
      "ekap_id": "4c153041b8fe…",
      "ihale_adi": "100 Projenin ÇED ve PTD Hazırlanması",
      "idare_adi": "Toplu Konut İdaresi Başkanlığı",
      "il": "ANKARA",
      "ihale_tarihi": "03.09.2026 11:00",
      "ihale_tip": 3
    }
  ]
}
```

- `ihale_tip`: 1 Mal Alımı · 2 Yapım · 3 Hizmet · 4 Danışmanlık. **Boş gelebilir.**
- `ihale_tarihi` **string**'dir, ISO değil (EKAP'ın ham biçimi). Parse etmeye çalışmayın,
  olduğu gibi gösterin.
- Karta dokunma → `TenderDetail`, `ihaleId = card.ekap_id`.
  ⚠️ İKN'yi yol parametresi yapmayın, içinde `/` var.

### 4.2 Yakında eklenecek: `blocks`

`payload`'a **geriye uyumlu** bir `blocks` dizisi eklenecek. `tender_cards` **kalacak**
(eski mesajlar ve eski uygulama sürümleri bozulmasın diye).

```json
{
  "kind": "text",
  "tender_cards": [ ... ],
  "blocks": [
    {"type": "notice", "seviye": "uyari", "metin": "Bu filtre, değeri bilinmeyen ihaleleri de eler."},
    {"type": "notice", "seviye": "kilit", "metin": "İdare raporu Pro üyelere özeldir."},
    {"type": "action", "action_id": "3f2a…", "tur": "ihale_kaydet",
     "ozet": "Bu ihaleyi kayıtlılarınıza ekleyeyim mi?",
     "onay_metni": "Kaydet", "ret_metni": "Gerek yok",
     "durum": "bekliyor", "expires_at": "2026-09-01T12:00:00Z"}
  ]
}
```

**Şimdi yapılması gereken tek şey**: `blocks`'u dolaşan küçük bir dispatcher ve
**bilinmeyen `type`'ı SESSİZCE ATLAMAK**.

```js
const RENDERERS = { notice: NoticeBlock, /* action: ActionCard — Faz 2 */ };
(payload.blocks || []).map((b, i) => {
  const R = RENDERERS[b.type];
  return R ? <R key={i} block={b} /> : null;   // bilinmeyen tip → atla, ÇÖKME
});
```

Bu kural sözleşmenin kalbi: sunucu, mobil sürüm güncellenmeden yeni blok tipi
yayınlayabilmeli. Eski uygulama onu görmez, ama çökmez de.

`blocks` geldiğinde kartlar **hem** `tender_cards`'ta **hem** `blocks` içinde olacak;
`blocks` varsa ona öncelik verin, yoksa `tender_cards`'a düşün.

---

## 5. Yapılacaklar

### Zorunlu
1. **Polling zaman aşımını 240 sn'ye çıkarın** (yalnızca asistan için; doküman analizini
   ayrı bırakın). Bkz. §1.1.

### Önerilen
2. **`blocks` dispatcher'ı** — bilinmeyen tipi atlayan 10 satırlık yapı (§4.2). Şimdi
   eklenirse Faz 2'de sunucu tarafı tek başına devreye alınabilir.
3. **Bekleme göstergesi** — araç döngüsü 30-60 sn sürebiliyor; üç nokta tek başına
   "takıldı mı?" hissi veriyor. Süreye bağlı metin (6/20/45 sn) yeterli.
   ⚠️ Sunucu **ara durum bildirmiyor**: Celery `PROGRESS` state'i `AnalyzeStatusView`
   tarafından tanınmaz ve **`failed` olarak okunur**. Gerçek ilerleme çubuğu şu an
   mümkün değil; metinler dürüst-genel olmalı ("Veritabanında arıyorum…").

### Yapmayın
4. Markdown renderer eklemeyin (§1.2).
5. `POLL_TIMEOUT` sabitini genel olarak büyütmeyin.
6. Karta dokunmada `ikn`'yi yol parametresi olarak kullanmayın.

> **Not:** 1 ve 3 numaralı maddeler `IhaleTakip` deposunda `b4b711e` commit'iyle
> uygulanmıştır; ekip isterse geri alıp kendi tercihiyle yeniden yapabilir.

---

## 6. Hata durumları

| Durum | Ne zaman | Mobil davranışı |
|---|---|---|
| `403` + `errors.code = "premium_required"` | Sohbet Pro'ya özel | Paywall aç (mevcut interceptor zaten yapıyor) |
| `400` "Önce firma profilinizi oluşturun." | Profil yok | Onboarding'e yönlendir |
| `GET tasks` → `500` / `status: "failed"` | Görev çöktü | Kırmızı hata balonu, "tekrar deneyin" |
| `422` | Model isteği reddetti ya da AI servisi hata verdi | Sunucu mesajını göster |
| Zaman aşımı (240 sn) | Nadir | "Beklenenden uzun sürdü" + sohbeti yeniden açmayı öner (yanıt DB'ye yazılmış olabilir) |

⚠️ Sunucu, süre aşımında artık **kullanıcıya bir mesaj kaydediyor** ("Bu soruyu
yanıtlamak beklenenden uzun sürdü…"). Yani zaman aşımı sonrası sohbet boş kalmaz.

---

## 7. Faz 2 önizlemesi — onay kartı (eylemler)

Asistan kaydetme / alarm / filtre gibi işleri **kendisi yapmayacak**; bir **öneri**
üretecek ve kullanıcı onaylayacak. Bu bilinçli bir karardır: okuma hatası düzeltilebilir,
yazma hatası kullanıcının verisini kirletir.

Akış: `blocks` içinde `type: "action"` kartı gelir → kullanıcı butona basar → mobil şu
uçlardan birini çağırır:

```
POST /assistant/actions/{action_id}/execute/    → 200 { ... } | 403 | 410
POST /assistant/actions/{action_id}/dismiss/    → 200
```

- **`403 premium_required`** burada da çıkabilir (alarm kurma Pro'dur) → Paywall. Eylem
  `bekliyor` durumunda kalır, kullanıcı Pro alıp tekrar basabilir.
- **`410 Gone`** → öneri bayatlamış (7 gün ya da ihale tarihi geçmiş). Kartı pasifleştir,
  "Bu öneri güncelliğini yitirdi, tekrar sorun" yaz.
- **İyimser UI kullanmayın** — yazma işlemidir; yanıt gelene kadar buton spinner'a dönsün.
- Çift dokunuş güvenlidir (sunucu idempotent), ama yine de butonu kilitleyin.

Bu uçlar **henüz yayında değildir**; şema Faz 2 çıkınca bu dokümanda güncellenecektir.

---

## 8. Test senaryoları

| # | Mesaj | Beklenen |
|---|---|---|
| 1 | "otomasyon ihalelerini getir" | Metin + ihale kartları; süre 10-40 sn |
| 2 | "2026/1473120 numaralı ihaleyi incelemek istiyorum" | İhale özeti + tek kart |
| 3 | "Toplu Konut İdaresi kimlere iş vermiş?" | Yüklenici listesi, yoğunlaşma yorumu; kart olmayabilir |
| 4 | "ONLİNE ÇEVRE en son ne iş almış?" | Firma özeti + son sözleşmeler |
| 5 | "Kaydettiğim ihaleler neler?" | Kullanıcının kayıtlı ihaleleri |
| 6 | "Geçici teminat nedir?" | Araç çağrısı olmadan düz cevap; 5-10 sn |

Her yanıtta kontrol edin: **markdown karakteri yok**, uygulamada var olmayan bir ekran/
buton adı geçmiyor, "kazanma oranı" gibi hesaplanamayan bir metrik uydurulmamış.
