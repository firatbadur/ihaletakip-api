# Web'den Pro satışı (iyzico) — API yanıtı (6. not #15–#19)

> **Okuyucu:** web portalı ekibi · **Tarih:** 2026-10-28 · **Durum:** API'de uygulandı,
> sandbox anahtarları ve fiyat girilmedi → `WEB_SATIS_ENABLED=False` (satış kapalı).

## #15 Kaynağa duyarlı Pro hakkı ✅
- `subscription.ProGrant` (`store` | `iyzico` | `admin`). `subscription_tier` /
  `subscription_expires_at` artık **etkin değer**; tek yazarı `recompute_pro`.
- RevenueCat senkronu yalnızca `store` satırını yazar → web'den ödeyen / admin'den Pro
  verilen kullanıcı mobil `verify` ile **artık Free'ye düşmez**.
- `UserSerializer` → yeni salt okunur **`subscription_source`**: `store` · `iyzico` ·
  `admin` · `null` (Pro değil). Login / `GET auth/profile/` yanıtında gelir.
- Etkin bitiş zincirlenir: Pro'yken alınan ikinci yıl, ilk yılın sonundan başlar ve
  `subscription_expires_at` ikinci yılın sonunu gösterir.
- Deneme hatırlatıcısı yalnızca mağaza hakkının bitişine bakar.
- Mevcut Pro'lar göç edildi (`0002`): RevenueCat izi olan → `store`, olmayan → `admin`.

## #16 Fatura profili ✅ — `GET/PUT /api/v1/auth/billing-profile/`
- `GET` → profil ya da `data: null`. `PUT` tam profili yazar.
- `bireysel`: `full_name` (ad + soyad, en az iki kelime) + `identity_number` (TCKN,
  sağlama algoritması). `kurumsal`: `company_name` + `tax_office` + `tax_number`
  (10 hane), `full_name` = yetkili. Kurumsal profilde TCKN **tutulmaz** (gönderilse de boşaltılır).
- `phone` `+90XXXXXXXXXX`'e normallenir (`0532…`, `532…`, `+90 532…` kabul).
- Hatalar: **400** + `errors.<alan>` (liste).

## #17 Ödeme uçları ✅ — `/api/v1/payments/`
| Uç | Kimlik | Not |
|---|---|---|
| `GET plan/` | yok | `{code, name, amount:"2400.00"\|null, vat_rate, currency, months, available}` — **`available=false` ise satın al düğmesini göstermeyin** |
| `POST checkout/` | JWT | gövde `{consents:{on_bilgilendirme, mesafeli_satis, cayma_istisnasi}, consent_version}` → `{payment_page_url}` |
| `POST checkout/complete/` | **yalnız `X-IT-Portal-Key`** | gövde `{token}` → `{status, message, pro_until?}` |
| `GET /` | JWT | `[{id, created_at, amount, currency, status, pro_from, pro_until, card_last4, invoice_available, cancel_requested}]` — `pending` listelenmez |
| `GET <id>/invoice/` | JWT, sahibi | PDF; hazır değilse 404 `invoice_not_ready` |
| `POST <id>/cancel-request/` | JWT, sahibi | `{cancel_requested_at, pro_until, refund:false}` |

Hata kodları (`errors.code`): `consents_required` (400), `billing_profile_required` (400),
`sales_closed` (503), `payment_provider_error` (502), `not_cancellable` (400),
`invoice_not_ready` (404), hız sınırı → 429 `throttled`.

- `checkout/`'a **`X-IT-Client-IP`** gönderin (portal anahtarıyla) — iyzico `buyer.ip` oradan.
- `complete/`: `status=pending` gelirse "Ödemeniz doğrulanıyor" gösterin; mutabakat
  15 dk içinde kapatır, kullanıcı `/hesabim`'da sonucu görür. `failure` → `message`'ı
  olduğu gibi gösterebilirsiniz. Aynı token'ı tekrar göndermek güvenlidir (idempotent).
- Hız sınırı: `checkout/` kullanıcı başına 10/saat, `complete/` IP başına 30/dk.
- `consent_version` onaylattığınız metnin sürümü; işlem kaydına yazılır.

## #18 Celery ✅ (kısmen)
- Mutabakat `*/15`: 15 dk'dan eski `pending` sorulur. iyzico karar vermediyse token
  ömrü (60 dk) dolana kadar `failure` **yazılmaz**.
- Süre hatırlatma (her gün 10:40): web kaynaklı etkin bitişe 30/7/1 gün kala + bitince
  e-posta, "Süreyi uzat" → `/pro/satin-al`. Mağaza aboneliği aktifse gitmez.
- ⚠️ **e-Arşiv entegratörü YOK** (muhasebeci seçecek). O zamana kadar fatura muhasebede
  kesilip admin'den ödemeye yüklenir; ilk yüklemede "Fatura hazır" e-postası gider ve
  `invoice/` ucu PDF'i verir.

## #19 E-postalar / fesih / iade ✅
- Ödeme alındı · Fatura hazır · Süre bitiyor/bitti · Fesih alındı · İade yapıldı.
- ⚠️ "Ödeme alındı" ekinde ön bilgilendirme + mesafeli satış için **işlem kaydı** (HTML)
  var: taraflar, hizmet, bedel, dönem, onayların zamanı/IP/sürümü. **Hukukçu onaylı tam
  metin DEĞİLDİR** — metin onaylanınca sürümüyle birlikte API'ye verilmeli (şablon:
  `subscription/templates/sozlesme/islem_kaydi.html`). PDF yerine HTML eki (API'de PDF
  kütüphanesi yok).
- İade yok; istisnai iade admin'den ("Web ödemeleri" → **İADE ET** eylemi): iyzico
  iadesi + hak iptali + e-posta.

## Açılış için gerekenler (API `.env.prod`)
`IYZICO_API_KEY`, `IYZICO_SECRET_KEY`, `IYZICO_BASE_URL`, `IYZICO_CALLBACK_URL`,
`PRO_YEARLY_PRICE`, `PRO_VAT_RATE`, `SATICI_BILGI` ve en son `WEB_SATIS_ENABLED=True`.
⚠️ IYZWSv2 imzası ve yanıt `signature` biçimi **sandbox'ta henüz denenmedi**.
