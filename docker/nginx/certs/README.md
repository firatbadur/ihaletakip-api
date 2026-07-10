# nginx TLS sertifikaları (Cloudflare Origin Certificate)

Bu klasöre **Cloudflare Origin Certificate** dosyaları konur. Dosyalar
`.gitignore`'dadır → **commit edilmez** (private key gizli kalmalı). Sunucuda
elle oluşturulur.

## Nasıl oluşturulur

1. Cloudflare paneli → ilgili domain → **SSL/TLS → Origin Server →
   Create Certificate**.
2. "Let Cloudflare generate a private key and a CSR" seçili bırak, RSA, süre
   **15 yıl**. Create.
3. Çıkan iki bloğu sunucuda bu klasöre kaydet:
   - **Origin Certificate** (`-----BEGIN CERTIFICATE-----`) → `cf-origin.pem`
   - **Private Key** (`-----BEGIN PRIVATE KEY-----`) → `cf-origin.key`

```bash
# sunucuda, repo kökünde
mkdir -p docker/nginx/certs
nano docker/nginx/certs/cf-origin.pem   # sertifikayı yapıştır
nano docker/nginx/certs/cf-origin.key   # private key'i yapıştır
chmod 600 docker/nginx/certs/cf-origin.key
```

4. Cloudflare paneli → **SSL/TLS → Overview → SSL modu = Full (strict)**.

## (Opsiyonel) Authenticated Origin Pulls

Yalnızca Cloudflare'in origin'e bağlanabilmesini garanti etmek için (mTLS):

1. Cloudflare origin-pull CA'sını indir:
   https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/
   → `cloudflare-origin-pull-ca.pem` olarak bu klasöre koy.
2. `docker/nginx/default.conf` içindeki `ssl_client_certificate` +
   `ssl_verify_client on` satırlarını aç.
3. Cloudflare paneli → SSL/TLS → Origin Server → **Authenticated Origin Pulls**'u aç.

## Beklenen dosyalar

| Dosya | Zorunlu | Açıklama |
|-------|---------|----------|
| `cf-origin.pem` | ✅ | Cloudflare Origin sertifikası |
| `cf-origin.key` | ✅ | Private key (chmod 600) |
| `cloudflare-origin-pull-ca.pem` | ⛔ opsiyonel | Authenticated Origin Pulls için |
