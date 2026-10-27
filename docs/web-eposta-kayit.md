# E-posta ile Kayıt + Şifre Sıfırlama — Web Portalı (ihaletakip.com) Notu

Uçlar, sözleşme ve hata kodları mobil ile **aynıdır**. Tam istek/yanıt örnekleri,
şifre kuralları, sınırlar ve hata tablosu → **`docs/mobil-eposta-kayit.md`**.
Bu not yalnızca web'e (Next.js BFF) özgü noktaları anlatır.

> **Durum (2026-10-27): üretimde canlı.** Gönderim Amazon SES ile. ⚠️ SES hesabının
> üretim onayı gelene kadar kodlar yalnızca SES'te doğrulanmış adreslere ulaşır;
> geliştirme sırasında test adresinizi SES'te doğrulatın. Onay gelince istemci
> tarafında hiçbir şey değişmez.

| Adım | Uç (`API_URL` + …) | Başarı |
|---|---|---|
| Kayıt — kod iste | `POST auth/email/register/start/` `{email, password, display_name?}` | 200 `{email, gecerlilik_sn, tekrar_gonderim_sn}` |
| Kayıt — doğrula | `POST auth/email/register/verify/` `{email, code}` | **201** token çifti (`created: true`) |
| Giriş | `POST auth/login/` `{email, password}` | 200 token çifti |
| Şifre — kod iste | `POST auth/password/reset/start/` `{email}` | 200 (**her zaman**, kayıtlı olmasa da) |
| Şifre — doğrula | `POST auth/password/reset/verify/` `{email, code}` | 200 `{reset_token, gecerlilik_sn: 900}` |
| Şifre — kaydet | `POST auth/password/reset/confirm/` `{reset_token, new_password}` | 200 token çifti |

---

## 1. Server Action'lar

Token dönen üç uç (`register/verify`, `login`, `password/reset/confirm`) mevcut
`startSession()` ile birebir uyumlu: aynı `TokenPairResponse`, aynı çerez yazımı, aynı
`onboarding_status` yönlendirmesi. Öneri (`src/features/auth/actions.ts`):

```ts
export const loginWithEmail = (email: string, password: string, next: string) =>
  startSession("auth/login/", { email, password }, next);

export const verifyEmailRegistration = (email: string, code: string, next: string) =>
  startSession("auth/email/register/verify/", { email, code }, next);

export const confirmPasswordReset = (resetToken: string, newPassword: string, next: string) =>
  startSession("auth/password/reset/confirm/", { reset_token: resetToken, new_password: newPassword }, next);
```

Kod isteyen uçlar (`register/start`, `password/reset/start`) ve `password/reset/verify`
token döndürmez → `apiFetch` + `ok(data)`.

⚠️ **Hepsine `client: await requestClient()` verin** (portal anahtarı + `X-IT-Client-IP`).
Bu uçların IP sınırı **her zaman uygulanır**. Anahtar gitmezse tüm web kullanıcıları
konteynerin tek IP'sini paylaşır ve 10 dakikada 10 kod isteğinden sonra **herkes** 429
alır. ⚠️ Bu yüzden API'de `PORTAL_KEY` boşken (bugünkü üretim durumu) web e-posta
akışını **canlıya açmayın**.

⚠️ `startSession` 401'i "Giriş doğrulanamadı" diye çeviriyor. E-posta girişinde 401
"e-posta ya da şifre hatalı" demektir, bu yüzden `loginWithEmail` için mesajı
`"E-posta ya da şifre hatalı."` yapın (API'nin `message`'ı da `"Geçersiz kimlik
bilgileri."`).

## 2. Hata ayrımı — `errors.code`'a bakın

Bu uçların 400 yanıtları iki türlüdür:

- **Alan hatası**: `errors = {"password": ["…"]}` ya da `{"new_password": [...]}` ya da
  `{"email": [...]}` → mevcut `ValidationError.fieldErrors` doğru çalışır.
- **Akış hatası**: `errors = {"code": "code_invalid", "detail": "…", "kalan_deneme": 3}`.

⚠️ `toFieldErrors` ikinci türdeki `code`/`detail` string'lerini de "alan hatası" sanıp
`fieldErrors.code = ["code_invalid"]` üretir. Formda bunları göstermeyin:
`error.errors?.code` doluysa akış hatasıdır. `message`'ı genel hata olarak gösterin ve
koda göre davranın (`email_exists` → giriş/şifremi unuttum bağlantısı,
`code_expired`/`code_attempts` → "yeni kod gönder", `reset_token_invalid` → akışı baştan
başlat).

429'da gövde `errors.retry_after` (tamsayı sn) **ve** `Retry-After` başlığı taşır. Kod
ekranındaki geri sayım için `retry_after`'ı kullanın (`code_cooldown`, `code_limit`).

## 3. Durum taşıma (adımlar arası)

- Kayıt 1. adımdan 2. adıma `email` taşınır. Bunun için yanıttaki **küçük harfli**
  `data.email` kullanılmalı. URL'e koymak serbest (`/kayit/dogrula?e=…`), ama **şifre
  asla** URL'e ya da çereze yazılmaz. "Tekrar gönder" `register/start`'ı yeniden ister,
  bu da şifrenin form state'inde (istemci belleğinde) kalmasını gerektirir. Sayfa
  yenilenince şifre kaybolur: o durumda "Tekrar gönder" kullanıcıyı 1. adıma döndürmeli.
- `reset_token`'ı httpOnly, kısa ömürlü (15 dk) bir çereze ya da form state'ine koyun;
  URL'e koymayın (geçmişte/logda kalır).

## 4. Güvenlik/ürün notları

- `password/reset/start` her zaman aynı 200'ü döner. Web de "bu e-posta kayıtlı değil"
  dememeli; aynı ekrana geçmeli.
- Şifre sıfırlama başarıyla bitince API kullanıcının **diğer oturumlarını** kapatır,
  web dahil: başka tarayıcıdaki refresh token kara listeye girer.
- Google/Apple hesabı olan kullanıcı "Şifremi unuttum" ile şifre belirleyip e-postayla
  da girebilir (aynı hesap).
- Kod alanı: `inputMode="numeric"`, `autoComplete="one-time-code"`, `maxLength={6}`,
  `pattern="\d{6}"`.
- Önerilen sayfalar: `/giris` (e-posta sekmesi), `/kayit`, `/kayit/dogrula`,
  `/sifremi-unuttum`, `/sifremi-unuttum/kod`, `/sifremi-unuttum/yeni`. Hepsi `noindex`.
