# E-posta ile Kayıt + Şifre Sıfırlama — Mobil Entegrasyon Notu

Google/Apple'a ek olarak **e-posta + şifre** ile kayıt ve giriş açıldı. Kayıt ve şifre
sıfırlama, e-postaya gönderilen **6 haneli kod** ile doğrulanır (link yok, deep link
gerekmez). Kodlar `noreply@ihaletakip.com` adresinden gelir.

> **Mevcut sürüm etkilenmez**: bugünkü uygulama bu uçların hiçbirini çağırmıyor;
> Google/Apple girişi aynen çalışır. Eski `POST /auth/register/` (doğrulamasız kayıt)
> **kapatıldı** → 400 `verification_required`. Uygulama onu kullanmıyordu.

Taban adres: `https://ihale-takip.envisoft.com.tr/api/v1` · Bu uçların hepsi **kimliksiz**
(`Authorization` gönderilmez) ve global zarfla döner: `{success, message, data, errors?}`.

---

## 1. Kayıt (2 adım)

### 1a. Kod iste — `POST /auth/email/register/start/`

```json
{ "email": "ahmet@firma.com.tr", "password": "Guclu.Sifre42", "display_name": "Ahmet Yılmaz" }
```

`display_name` isteğe bağlıdır (boşsa e-postanın `@` öncesinden türetilir). Ad/soyad
sonradan tanışma sihirbazında (`PATCH /auth/profile/`) her zamanki gibi alınır.

**200:**
```json
{
  "success": true,
  "message": "Doğrulama kodu e-posta adresinize gönderildi.",
  "data": { "email": "ahmet@firma.com.tr", "gecerlilik_sn": 600, "tekrar_gonderim_sn": 60 }
}
```

⚠️ `data.email` **küçük harfe indirilmiş** adrestir. Sonraki adımda bunu gönderin.
Hesap bu adımda **açılmaz**.

### 1b. Kodu doğrula — `POST /auth/email/register/verify/`

```json
{ "email": "ahmet@firma.com.tr", "code": "482913" }
```

**201**: `login` / Google girişiyle **birebir aynı** yanıt (`access`, `refresh`, `user`,
`created: true`). Mevcut oturum açma kodunuzu (token kaydet → `onboarding_status`
`pending` ise tanışma sihirbazı) olduğu gibi kullanın. AppsFlyer'da `created: true`
olduğu için **kayıt olayı** (`af_complete_registration`) atılmalıdır.

### Kodu yeniden gönder

Ayrı uç yok: **1a aynı gövdeyle tekrar çağrılır** (şifre ekranın belleğinde durur).
Yeni kod öncekini geçersiz kılar. "Kodu tekrar gönder" düğmesini `tekrar_gonderim_sn`
(60 sn) boyunca geri sayımla kilitleyin.

---

## 2. Giriş — `POST /auth/login/` (mevcut uç)

```json
{ "email": "ahmet@firma.com.tr", "password": "Guclu.Sifre42" }
```

200 → token çifti (`created: false`). Yanlış şifre ya da bu adreste şifre tanımlı değilse
(hesap Google/Apple ile açılmış) → **401** `"Geçersiz kimlik bilgileri."`. İkisi
bilerek ayırt edilmez; ekranda "Şifremi unuttum" bağlantısı gösterin.
Hesap kapatılmışsa → 403.

---

## 3. Şifre sıfırlama (3 adım)

### 3a. Kod iste — `POST /auth/password/reset/start/`

```json
{ "email": "ahmet@firma.com.tr" }
```

⚠️ **Yanıt her zaman 200 ve aynıdır**: adres kayıtlı olsun olmasın, hangi adreslerin
kayıtlı olduğu sızmasın diye. Her durumda kod ekranına geçin. Mesaj:
*"Bu e-posta adresiyle kayıtlı bir hesap varsa şifre sıfırlama kodu gönderildi."*

Yeniden gönderim için aynı uç tekrar çağrılır. Bekleme süresi dolmadan yapılan çağrıda
kod gönderilmez ama yanıt yine 200 döner, bu yüzden geri sayımı **istemci** tutmalı.

💡 Google/Apple ile kayıtlı kullanıcı bu akışla şifre belirleyip e-postayla da girebilir.

### 3b. Kodu doğrula — `POST /auth/password/reset/verify/`

```json
{ "email": "ahmet@firma.com.tr", "code": "482913" }
```

**200:** `{"reset_token": "MTIz…", "gecerlilik_sn": 900}`. Kod burada tüketilir.
`reset_token` 15 dakika geçerlidir; yalnızca bellekte tutun, diske yazmayın.

### 3c. Yeni şifre — `POST /auth/password/reset/confirm/`

```json
{ "reset_token": "MTIz…", "new_password": "Yeni.Sifre2027" }
```

**200** → token çifti (`created: false`), yani kullanıcı **giriş yapmış olur**.
⚠️ Hesabın **diğer cihazlardaki oturumları kapatılır** (refresh token'ları geçersizleşir).
O cihazlar bir sonraki token yenilemesinde girişe düşer.

---

## 4. Şifre kuralları

Sunucu doğrular, mesajlar Türkçe gelir: en az **8 karakter**, yalnız rakam olamaz,
çok yaygın bir şifre olamaz (`password`, `12345678` …), e-postaya ya da ada çok benzer
olamaz. İhlalde **400** döner ve mesajlar alan adıyla gelir:

```json
{ "success": false, "message": "Bu parola çok kısa. En az 8 karakter içermek zorunda.",
  "data": null,
  "errors": { "password": ["Bu parola çok kısa. En az 8 karakter içermek zorunda."] } }
```

Alan adı kayıtta `password`, sıfırlamada `new_password`. İstemcide de min 8 kontrolü
yapın ki gereksiz istek atılmasın; asıl karar sunucunundur.

---

## 5. Hata kodları (`errors.code`)

Alan hatası dışındaki tüm hatalar `errors = {code, detail, ...}` taşır. `message` her
zaman kullanıcıya gösterilebilir Türkçe metindir.

| HTTP | `errors.code` | Nerede | Ne yapılmalı |
|---|---|---|---|
| 400 | `email_exists` | kayıt 1a/1b | "Bu e-postayla hesap var" → Giriş / Şifremi unuttum'a yönlendir |
| 400 | `code_invalid` | 1b, 3b | Kod hatalı; `errors.kalan_deneme` (int) göster |
| 400 | `code_expired` | 1b, 3b | Süre doldu / kod yok → "Yeni kod gönder" |
| 400 | `code_attempts` | 1b, 3b | 5 hatalı deneme → kod kilitlendi, yeni kod iste |
| 400 | `reset_token_invalid` | 3c | 15 dk geçti / token kullanıldı → akışı baştan başlat |
| 400 | `verification_required` | eski `/auth/register/` | (uygulama kullanmıyor) |
| 429 | `code_cooldown` | 1a | `errors.retry_after` sn bekle (geri sayımı buna göre kur) |
| 429 | `code_limit` | 1a | Saatte en çok 5 kod → `retry_after` sn |
| 429 | `throttled` | hepsi + login | IP sınırı (aşağıda); `Retry-After` başlığı var |
| 503 | `send_failed` | 1a, 3a | E-posta altyapısı arızası → "Birkaç dakika sonra deneyin" |

`retry_after` **tamsayı saniye**dir; ayrıca `Retry-After` HTTP başlığı da gönderilir.

---

## 6. Sınırlar (sunucuda, her zaman uygulanır)

- **Kod**: 6 hane, **10 dk** geçerli, tek kullanımlık, **5 hatalı denemede** kilitlenir.
- **E-posta başına**: yeni kod için **60 sn** bekleme, saatte en çok **5** kod.
- **IP başına** (`throttled`): kod gönderme 10 dk'da 10 / günde 40; kod doğrulama
  10 dk'da 30; **giriş denemesi 10 dk'da 30**.

⚠️ Bu uçlarda 429 **gerçekten döner** (diğer uçlardaki gözlem modunun aksine). Mobilde
429 için özel bir ele alma yoksa en azından `message`'ı gösterin; genel "bir hata oluştu"
ekranına düşürmeyin.

---

## 7. Önerilen ekran akışı

```
Giriş ekranı ──"E-posta ile devam et"──▶ [E-posta + Şifre]  ──Giriş──▶ login
     │                                         │
     │                                   "Hesap oluştur"
     │                                         ▼
     │                           [Ad (ops.) + E-posta + Şifre] ──▶ 1a
     │                                         ▼
     │                           [6 haneli kod · 60 sn geri sayım] ──▶ 1b ──▶ oturum
     │
     └──"Şifremi unuttum"──▶ [E-posta] ─▶ 3a ─▶ [Kod] ─▶ 3b ─▶ [Yeni şifre] ─▶ 3c ─▶ oturum
```

- Kod alanında `textContentType="oneTimeCode"` (iOS) ve `autoComplete="sms-otp"`
  (Android) kullanın. Konu satırında kod yazdığı için iOS Mail'den otomatik doldurma
  önerisi gelebilir. `keyboardType="number-pad"`, `maxLength={6}`, 6. hanede otomatik
  gönderin.
- E-posta alanı: `autoCapitalize="none"`, `keyboardType="email-address"`.
- Kod ekranında "Kod gelmedi mi? Spam klasörünü kontrol edin" notu + geri sayımlı
  "Tekrar gönder" + "E-postayı değiştir" (önceki ekrana dön).

---

## 8. Destek

Admin → **E-posta Kodları**: her adres için son gönderim, geçerlilik, hatalı deneme.
Kod görünmez. Kilitlenen bir kullanıcının satırını **silmek** bekleme ve saatlik
sınırı sıfırlar.
