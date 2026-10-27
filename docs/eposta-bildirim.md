# E-posta Bildirimleri (Günlük Bülten) — Mobil + Web Entegrasyon Notu

Kullanıcılar artık günün bildirimlerini **e-postayla da** alıyor. Tercih **varsayılan
açık**; profilden kapatılıp açılabiliyor. Push ve uygulama-içi bildirimler **değişmedi**.

> **Mevcut sürümler etkilenmez**: yeni alan `email_notifications` profil yanıtına
> eklendi (additive). Eski uygulama onu yok sayar; kullanıcı e-postaları alır ve
> e-postadaki bağlantıdan çıkabilir.

---

## 1. Tercih — `email_notifications`

`GET /api/v1/auth/profile/` (ve login / register / sosyal giriş yanıtındaki `user`):

```json
{ "id": 42, "email": "ahmet@firma.com.tr", "...": "...", "email_notifications": true }
```

Değiştirmek — `PATCH /api/v1/auth/profile/` (JWT):

```json
{ "email_notifications": false }
```

Yanıt güncel `user` nesnesidir. Ayrı bir uç yok. Önerilen yer: **Profil → Bildirim
tercihleri** ekranında bir anahtar:

> **E-posta bildirimleri** — Günün bildirimlerini her sabah tek bir e-postada al.
> *(alt metin: `user.email` adresine gönderilir)*

- ⚠️ Tercihi `preferences` JSON'una **yazmayın**. Ayrı bir alandır, çünkü
  `PATCH /auth/preferences/` JSON'un tamamını değiştirir ve tercih orada sessizce
  silinebilirdi.
- Anahtarı yalnızca `user.email` doluysa gösterin (Apple "e-postamı gizle" adresleri
  doludur, çalışır).
- Kullanıcı e-postadaki "çık" bağlantısını kullanırsa alan sunucuda `false` olur.
  Ekranı açarken profili tazeleyin.

## 2. ⚠️ `email` artık profilden DEĞİŞTİRİLEMEZ

E-posta giriş kimliği ve bülten adresi olduğu için `PATCH /auth/profile/` gövdesindeki
`email` **yok sayılır** (hata dönmez, değer değişmez). Bugün hiçbir istemci onu
göndermiyordu. Doğrulamalı e-posta değişikliği gerekirse ayrı bir akış olarak eklenir.

## 3. E-postanın içeriği

- **Ne zaman**: her sabah **10:00** (TR), kullanıcı başına **en çok bir** e-posta.
  Bildirimi yoksa e-posta gitmez.
- **Ne anlatır**: son 24 saatte (önceki gün 10:00 → bugün 10:00) üretilen bildirimlerin
  tamamı, push'la aynı içerik:

  | Bildirim | E-postada |
  |---|---|
  | Kayıtlı filtre özeti (08:00) | gövde metni + eşleşen ilk 8 ihale (ad, idare, il, tür, ihale tarihi) |
  | Takip edilen idare özeti (08:30) | aynı, idare kümesinden |
  | İhale Asistanı önerisi (09:00, Pro) | önerilen ihaleler |
  | İhale alarmı (09:00, Pro) | alarm metni + "İhaleyi aç" |
  | Takip edilen firma (12:00, Pro) | metin + "Firmayı aç" (ertesi sabahın e-postasına girer) |

  İhale listesi uygulamanın açtığı listeyle **aynı sorgudan** kurulur. Sayı push'taki
  sayıdır; listede yalnızca ilk 8 ihale gösterilir.
- **Konu**: tek bildirim varsa bildirimin başlığı, birden çoksa
  `27 Ekim özetiniz: 3 yeni gelişme · İhale Takip`.
- **Gönderen**: `İhale Takip <noreply@ihaletakip.com>`.

### Bağlantılar (web portalı)

Taban `https://ihaletakip.com` (`EPOSTA_LINK_TABANI`), hepsi
`?utm_source=eposta&utm_medium=bulten` taşır:

| Bağlantı | Adres |
|---|---|
| İhale | `/ihale/<yıl>-<sayı>` (İKN'deki `/` → `-`, web `slug.ts` ile aynı) |
| Filtre/idare/asistan özeti "Tümünü gör" | `/bildirimler` |
| Firma | `/yuklenici/<contractor_id>` |
| Bildirim tercihleri | `/hesabim` |

**Web için:** bu dört yol bugün `ihaletakip-web`'de var, değiştirilirse e-posta da
güncellenmeli. `/hesabim`'da e-posta bildirimleri anahtarı (`email_notifications`)
bulunmalı. `/bildirimler` girişli sayfa olduğu için girişsiz kullanıcı önce girişe
yönlenmeli ve sonra geri dönmeli.

**Mobil için:** AppsFlyer OneLink / universal link bu adresleri yakalarsa uygulama
açılabilir. Bugün `applinks:ihaletakip.onelink.me` var, `ihaletakip.com` yok.
`ihaletakip.com` eklenirse `/ihale/<yıl>-<sayı>` → ihale detayı eşlemesi yeterlidir
(İKN = `-`'yı ilk konumda `/` yap). Eklenmezse bağlantılar tarayıcıda web'de açılır.

## 4. Abonelikten çıkma (istemci işi yok)

E-postanın altında **"E-posta bildirimlerinden çık"** bağlantısı ve `List-Unsubscribe`
başlığı var (Gmail/Outlook'un kendi "abonelikten çık" düğmesi). Sunucuda
`/eposta/abonelik/?t=<imzalı>` sayfası açılır, onay düğmesiyle çıkarır ve aynı sayfadan
geri açılabilir. Giriş gerektirmez, istemcide bir şey yapmak gerekmez.
