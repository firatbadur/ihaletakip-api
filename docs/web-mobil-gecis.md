# Web → mobil geçiş kodu (QR ile doğrudan Pro ödeme ekranı)

Web'de girişli kullanıcı Pro'ya tıklar, telefonla QR'ı okutur; uygulama **aynı hesapla,
giriş ekranı görmeden**, tıklanan özelliğin anlatıldığı ödeme ekranında açılır. Satın alma
bitince web sayfası kendiliğinden "Pro'nuz açıldı" der.

Bu belge backend sözleşmesidir (web + mobil). Uçların tam şeması `docs/openapi.yaml` /
Postman → `auth` etiketi. Tüm yanıtlar her zamanki zarftadır (`{success, message, data}`),
hatalar `errors.code` taşır.

## Akış

```
Web (girişli) ─ POST /auth/handoff/ {baglam} ─► {id, kod, kisa_kod, gecerlilik_sn}
   QR = https://ihaletakip.com/q/<kod>     + ekranda kisa_kod
   web GET /auth/handoff/<id>/ ile yoklar
        │
Telefon /q/<kod> ─► web: POST /auth/handoff/okutuldu/ {kod} (portal anahtarı)
        │             ─► {gecerli, baglam} ─► 302 OneLink
        │                deep_link_value=web_pro & sub1=<baglam> & sub2=<kod> (gecerli ise)
        ▼
Uygulama: POST /auth/handoff/redeem/ {kod} | {kisa_kod}
   ─► access/refresh/user + baglam + maskeli_eposta
   ─► session kaydet → Purchases.logIn(user.id) → Paywall {source: baglam, from: "web"}
   ─► satın alma → POST /subscription/verify/  (web yoklaması is_premium=true görür)
```

## Uçlar

| Uç | Kim | Kimlik | Açıklama |
|---|---|---|---|
| `POST /api/v1/auth/handoff/` | web | JWT | Kod üretir. Gövde `{baglam}` (isteğe bağlı). **201** `{id, kod, kisa_kod, baglam, gecerlilik, gecerlilik_sn}` |
| `GET /api/v1/auth/handoff/<id>/` | web | JWT (sahibi) | `{id, durum, baglam, gecerlilik, is_premium}`; başkasının kodu → 404 |
| `DELETE /api/v1/auth/handoff/<id>/` | web | JWT (sahibi) | İptal, idempotent **204** |
| `POST /api/v1/auth/handoff/okutuldu/` | web `/q` rotası | `X-IT-Portal-Key` | Gövde `{kod}` → her zaman 200 `{gecerli, baglam}`; anahtar yoksa **403** `portal_required` |
| `POST /api/v1/auth/handoff/redeem/` | mobil | yok | Gövde `{kod}` **ya da** `{kisa_kod}` → `login` yanıtı + `baglam` + `maskeli_eposta` |

`durum` değerleri: `bekliyor` → `okutuldu` → `kullanildi`; ayrıca `iptal`, `suresi_doldu`.

⚠️ Plandan sapma: "okutuldu" ucu `id` değil **`kod`** alır — `/q/<kod>` rotası yalnızca
kodu bilir.

## Kurallar

- **Kod bir giriş anahtarıdır.** Uzun kod 43 karakter (URL güvenli), kısa kod 7 karakter,
  alfabe `23456789ABCDEFGHJKMNPQRSTUVWXYZ` (0/O/1/I/L yok). Backend yalnızca HMAC
  özetlerini saklar; web QR'ı **önbelleğe almamalı, loglamamalı**.
- **Tek kullanımlık, 15 dk** (`gecerlilik_sn`). Süre dolunca web yeni kod ister (`POST`),
  sayfa kapanınca/yenilenince eskisini `DELETE` eder. Kullanıcı başına aynı anda en çok
  **5** canlı kod; fazlası (en eskiler) kendiliğinden iptal edilir.
- **Kısa kod girişi**: büyük/küçük harf, tire ve boşluk serbest (`k7m-4qxa` = `K7M4QXA`).
  Web gösterirken bölebilir.
- **Bağlam**: `[a-z0-9_-]{0,40}` (web/mobil `PRO_CONTEXTS` anahtarı). Biçim dışıysa boş
  sayılır — mobil boş bağlamda genel ödeme ekranını açar.
- **Süresi dolmuş / kullanılmış kod okutulursa** `okutuldu` `gecerli:false` döner (bağlam
  yine gelir) → web kodu OneLink'e eklemeden genel bağlantıya yönlendirir, kullanıcı yine
  uygulamaya gider.

## Hatalar

| `errors.code` | HTTP | Anlam / istemci davranışı |
|---|---|---|
| `handoff_invalid` | 400 | Kod yok, süresi dolmuş, kullanılmış ya da iptal. Mobil: "Web'deki Pro sayfasından yeni kod alın." |
| `handoff_busy` | 429 | Kısa kod geçici olarak kapalı (küresel hatalı deneme sigortası, 10 dk). QR yolu çalışır. |
| — | 429 | IP sınırı (`Retry-After`): bozdurma 10 dk'da 20 / günde 100; oluşturma kullanıcı başına 10 dk'da 30 / günde 300 |
| `portal_required` | 403 | `okutuldu` ucu portal anahtarı olmadan çağrıldı |
| — | 400 | `redeem`'e ikisi birden ya da hiçbiri gönderildi |

## Mobil notları

- `redeem` ucu kimlik doğrulaması **yapmaz**: istekte eski/geçersiz bir `Authorization`
  başlığı olsa da 401 dönmez. Yine de başlıksız çağırmak doğrusu.
- Uygulamada başka bir hesap açıksa: `redeem` başarılı olunca **önceki oturum kapatılıp**
  yenisi kaydedilmeli (mevcut çıkış akışı + `saveSession`), `Purchases.logIn(user.id)`
  satın almadan ÖNCE çağrılmalı — aksi hâlde satın alma yanlış RC kimliğine yazılır.
- `maskeli_eposta` (`a***@gmail.com`) "bu hesapla devam ediliyor" satırı içindir; Apple
  "e-postamı gizle" kullanıcılarında `privaterelay.appleid.com` görünür.

## Girişsiz satın alma (RevenueCat `TRANSFER`)

Girişsiz web kullanıcısı için kod yoktur; ödeme ekranı girişsiz açılır ve "Satın al"da
tek dokunuş Apple/Google girişi yapılır. Giriş satın almadan önce olduğu için normalde
satın alma doğru kimliğe yazılır. Yine de girişten **önce** satın alınırsa RC satın almayı
anonim kimlikten hesaba `TRANSFER` olayıyla aktarır; backend artık bu olayı
`transferred_to` içindeki sayısal kimlikle eşleştirir ve satın almayı **kaybeden** hesabı da
RC'den yeniden senkronlar (Pro'su düşsün).
