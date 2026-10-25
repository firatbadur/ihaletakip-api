# KİK Kurul Kararları — Mobil Entegrasyon Notu

Kamu İhale Kurulu'nun **uyuşmazlık kararları** (itirazen şikâyet başvuruları üzerine
verilen kararlar) artık uygulamada görüntülenebilir ve bir ihale için **alarm**
kurulabilir: kurul o ihale hakkında karar verince kullanıcıya bildirim gider.

Kurul çoğunlukla Çarşamba toplanır, kararlar Perşembe 00:00'da yayımlanır (ama gün
düzensiz olabiliyor). Veri her sabah 06:40'ta kontrol edilir; alarm bildirimi en erken
07:00'den sonra gider.

> **Durum:** Backend hazır; toplayıcı **kapalı** (`EKAP_MOBIL_KURUL_ENABLED=False`).
> Açıldığında son 12 toplantı geriye dönük doldurulacak. O ana kadar listeler boş döner.

Kök: `https://ihale-takip.envisoft.com.tr/api/v1` · Alan adları **snake_case Türkçe**.
Görüntüleme uçları **herkese açık** (giriş gerekmez); alarm **Pro**.

---

## 1. İhale detayında kararlar

`GET /ekap/tenders/{ekap_id}/` yanıtına **`kurulKararlari`** dizisi eklendi (additive;
karar yoksa `[]`). Ayrı uç da var: `GET /ekap/tenders/{ekap_id}/kurul-kararlari/`.

```json
"kurulKararlari": [
  {
    "karar_no": "2026/UH.II-2688",
    "toplanti_tarihi": "2026-10-21T10:00:00+03:00",
    "kurul": "2026 / 41",
    "sira_no": 36,
    "karar_tarihi": "2026-10-21T11:35:30.293000+03:00",
    "yayin_tarihi": "2026-10-22T00:00:00+03:00",
    "karar_tur_kod": 102,
    "karar_aciklama": "İtirazen şikayet başvurusunun reddine",
    "karsi_oy": "OYBIRLIGI",
    "ikn": "2026/1519213",
    "ihale_adi": "PERSONEL HİZMETİ ALIMI",
    "idare_adi": "ANAMUR SULAMA BİRLİĞİ BAŞKANLIĞI",
    "basvuru_sahibi": "Nlc İnşaat Turizm Otomotiv Danışmanlık Tic. Ve San. Ltd. Şti.",
    "ekap_id": "…"
  }
]
```

- Aynı ihale için **birden çok karar** olabilir (farklı başvuru sahipleri — ölçüldü:
  aynı toplantıda bir ihaleye 4 karar). Hepsini listeleyin.
- `karar_aciklama` kararın sonucudur ("…reddine", "Düzeltici işlem belirlenmesine",
  "İhalenin iptaline" …). Rozet için kullanılabilir. `karar_tur_kod` ham EKAP kodudur;
  anlamını uydurmayın, metni gösterin.
- `karsi_oy`: `OYBIRLIGI` / `OYCOKLUGU`.
- Karar PDF'i **yok** (EKAP mobil API vermiyor).

## 2. Karar listesi — `GET /ekap/kurul-kararlari/`

| Parametre | Açıklama |
|---|---|
| `ikn` | İKN (`2026/1676818`) |
| `q` | ihale adı / idare / başvuru sahibi / karar no (Türkçe-i güvenli) |
| `toplanti` | toplantı günü `YYYY-MM-DD` |
| `page`, `page_size` | sayfa, boyut (vars. 20, en çok 100) |

Yanıt `{list, totalCount, page}`; satır şekli §1 ile aynı. `ekap_id` doluysa ihale
bizdedir → `/ekap/tenders/{ekap_id}/` açılabilir; `null` ise ihale kartı gösterilmez.

`GET /ekap/kurul-kararlari/toplantilar/` → `[{toplanti_tarihi, kurul, karar_sayisi}]`
(yeniden eskiye) — "toplantı seç" ekranı için.

## 3. Alarm — mevcut ihale alarmına yeni seçenek

Ayrı bir alarm yok: **ihale alarmına** `kurul_karar` anahtarı eklendi.

| Uç | Değişiklik |
|---|---|
| `POST /alarms/` | gövdede `"kurul_karar": true` kabul eder |
| `GET /alarms/{tender_id}/` | yanıtta `kurul_karar` döner |

```json
{ "tender_id": "…", "tender_ikn": "2026/1519213", "tender_title": "Personel Hizmeti",
  "reminder_day": true, "document_change": false, "completed": true, "kurul_karar": true }
```

- ⚠️ **Varsayılan kapalı** — mevcut alarmların hepsi de kapalı başlar. Alarm ekranına
  "Kurul kararı" anahtarı ekleyin; kullanıcı açar.
- Alarm kurmak zaten **Pro**'dur (Free → 403 `premium_required`, mevcut davranış).
- ⚠️ `POST /alarms/` yanıtı yalnızca **gönderilen** alanları yansıtır (mevcut davranış);
  kaydedilen durumu okumak için `GET /alarms/{tender_id}/` kullanın.

## 4. Bildirim

### Uygulama-içi

```json
{
  "type": "alarm",
  "title": "Personel Hizmeti",
  "body": "KİK uyuşmazlık kararı: İtirazen şikayet başvurusunun reddine (2026/UH.II-2688)",
  "tender_id": "…", "tender_ikn": "2026/1519213",
  "karar_no": "2026/UH.II-2688"
}
```

### Push (FCM `data`, her değer string)

```json
{ "type": "alarm", "tenderId": "…", "tenderIkn": "2026/1519213", "kararNo": "2026/UH.II-2688" }
```

### Tıklanınca

Mevcut alarm bildirimi gibi **ihale detayı** açılır (`tender_id`). `karar_no` doluysa
detayda kurul kararları bölümüne kaydırıp o kararı vurgulayın. Derin bağlantı önceliği
değişmez (`karar_no` bir hedef değil, `tender_id` hedefinin parametresidir).

⚠️ **`mapNotification` sabit alan listesine `karar_no` eklenmeli** — eklenmezse alan
uygulamaya hiç ulaşmaz (hata da vermez); bildirim yine ihaleyi açar ama karar vurgulanmaz.

### Metinler

| Durum | Başlık | Gövde |
|---|---|---|
| 1 karar | ihale adı | KİK uyuşmazlık kararı: {karar} ({karar_no}) |
| N karar | ihale adı | KİK N uyuşmazlık kararı verdi. İlki: {karar} ({karar_no}) |

Aynı karar ikinci kez bildirilmez. Kurul o ihale hakkında yeni bir karar verirse ayrıca
bildirilir.

## 5. Free / Pro

| İşlem | Free | Pro |
|---|---|---|
| Kararları görüntüleme (liste, ihale detayı) | ✅ | ✅ |
| Kurul kararı alarmı | ❌ (alarm kurmak Pro) | ✅ |
