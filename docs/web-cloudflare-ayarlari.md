# Web portalı — Cloudflare ayarları (ihaletakip.com)

> **Okuyucu:** web portalı ekibi (`ihaletakip-web`) · **Hazırlayan:** `ihaletakip-api` ·
> **Tarih:** 2026-11-05 · **Durum:** uygulandı ve test edildi (yapay zekâ botları hariç;
> o kısım yapılıyor). Bağlam: `docs/web-guvenlik-sayfa-siniri.md` #26 madde 1–2.

## Özet

`ihaletakip.com` zone'u **Pro** plana geçirildi ve şu korumalar açıldı:

| Ayar | Ne yapar | Durum |
|---|---|---|
| Hız kuralı: **Sayfa tarama sınırı** | Aynı IP dakikada 40'tan fazla sayfa açarsa "robot değilim" ekranı | ✅ test edildi |
| Hız kuralı: **İndirme sınırı** | Aynı IP dakikada 15'ten fazla `/belge` isterse aynı ekran | ✅ test edildi |
| **Super Bot Fight Mode** | Otomasyon araçlarına (curl, HeadlessChrome, script) challenge | ✅ test edildi |
| **Cloudflare Managed Ruleset** | Bilinen saldırı kalıplarını keser | ✅ açık |
| Custom rule: **Sunucu istekleri — bot korumasından muaf** | API sunucusunun `/api/revalidate` çağrısı bot korumasına takılmaz | ✅ test edildi |
| Yapay zekâ botları | Arama/asistan botları ve büyük sağlayıcılar açık, veri satıcıları kapalı | ⏳ yapılıyor |

Mobil uygulama bu ayarlardan etkilenmez: API ayrı bir zone'da (`ihale-takip.envisoft.com.tr`).
Web'in API'ye giden sunucu istekleri de Cloudflare'i atladığı için (`host-gateway`) etkilenmez.

## 1. Hız kuralları (Security → Security rules → Rate limiting rules)

**Sayfa tarama sınırı**

```
(starts_with(http.request.uri.path, "/ihale")
 or starts_with(http.request.uri.path, "/dogrudan-temin")
 or starts_with(http.request.uri.path, "/belge"))
and not cf.client.bot
and not (http.request.uri.query contains "_rsc=")
```

- Sayaç: IP başına, **40 istek / 1 dakika** → **Managed Challenge**.
- `/ihale` hem `/ihaleler`'i hem `/ihale/<ikn>`'i kapsar.
- `_rsc=` istekleri sayılmaz. Next.js'in sayfa içi gezinme ve önceden yükleme istekleri
  sayılırsa normal kullanıcı da kurala takılırdı; böylece yalnızca tam sayfa açılışları
  sayılıyor.
- `cf.client.bot`: Google, Bing gibi doğrulanmış botlar sayılmaz (SEO etkilenmez).

**İndirme sınırı**

```
starts_with(http.request.uri.path, "/belge") and not cf.client.bot
```

- **15 istek / 1 dakika** → **Managed Challenge**.

Pro'da challenge için süre seçilmiyor. Kutuyu geçen ziyaretçinin sayacı sıfırlanır, tekrar
hızlanırsa kutu yine çıkar. Kutu tarayıcı dışında (curl, script) **HTTP 429** +
`cf-mitigated: challenge` olarak görünür.

**Test (2026-11-05):** `/ihaleler`'e art arda 50 istek → ilk 40'ı `200`, sonrası `429`
challenge. `/belge`'ye 22 istek → ilk 15'i web'e ulaştı, sonrası `429` challenge.

## 2. Super Bot Fight Mode (Security → Settings → Bot traffic)

| Ayar | Değer |
|---|---|
| Definitely automated | Managed Challenge |
| Verified bots | Allow |
| Static resource protection | Off |
| JavaScript detections | On |

**Test:** dışarıdan `curl`, `python-requests` ve `HeadlessChrome` kimlikli istekler `403` +
`cf-mitigated: challenge` aldı.

⚠️ **Web ekibini ilgilendiren nokta:** `ihaletakip.com`'a **sunucudan** (tarayıcı olmadan)
gelen her istek artık challenge'a takılır. Bugün bilinen tek örnek API'nin blog yenileme
çağrısı ve onun için istisna var (§3). Web'e başka bir sunucu-sunucu çağrısı eklerseniz
(webhook, ödeme ya da giriş geri dönüşü, izleme servisi, cron), Cloudflare'de onun için de
istisna gerekir. Bize yazın; ekleyelim.

Tarayıcıdan gelen form ve yönlendirmeler etkilenmez: Apple/Google girişinin geri dönüşü,
`/q/<kod>` QR geçişi, Server Action'lar.

## 3. İstisna: API'nin blog yenilemesi (Security rules → Custom rules)

```
http.request.uri.path eq "/api/revalidate" and ip.src eq 173.249.43.236
```

- Aksiyon: **Skip → All Super Bot Fight Mode rules**. Listede en üstte.
- Neden: API, blog yazısı yayımlanınca `https://ihaletakip.com/api/revalidate`'i çağırıyor.
  Bu çağrı Cloudflare'den geçiyor ve "kesin otomasyon" sayılıyor. İstisna olmasa blog
  yayını web'e **sessizce** yansımazdı; API bu hatayı bilerek yutuyor.
- `173.249.43.236` API sunucusunun Cloudflare'e görünen IPv4 adresi (ölçüldü). Sunucu
  değişirse bu kural güncellenmeli.

**Test:** Bot modu açıkken sunucudan `/api/revalidate` → web'in kendi cevabı (`401`, test
isteğinde gizli anahtar yoktu), challenge yok. Aynı sunucudan `/ihaleler` → challenge
(istisna yalnızca bu yola açık).

## 4. WAF Managed Rules (Security → Settings)

- **Cloudflare Managed Ruleset:** açık. Varsayılan ayarlarla bilinen saldırı kalıplarını
  keser; yanlış alarm oranı düşük.
- **OWASP Core Ruleset:** bilerek **kapalı**. Arama kutusuna ve formlara yazılan bazı
  metinleri saldırı sanıp gerçek kullanıcıyı engelleyebilir. Açılırsa en düşük
  hassasiyetle ve Security Events izlenerek açılmalı.

## 5. Yapay zekâ botları (AI Crawl Control) — yapılıyor

Ürün kararı: ihaletakip.com yapay zekâ cevaplarında kaynak gösterilebilsin (GEO).

| Açık | Kapalı |
|---|---|
| OAI-SearchBot, ChatGPT-User, GPTBot (OpenAI) | Bytespider (ByteDance) |
| Claude-SearchBot, Claude-User, ClaudeBot (Anthropic) | CCBot (Common Crawl) |
| PerplexityBot, Perplexity-User | Diffbot, omgili ve diğer veri satıcıları |
| Google-Extended, Applebot-Extended | Amazonbot |
| Googlebot, BingBot, Applebot (arama motorları) | |

- `robots.txt` Cloudflare tarafından yönetiliyor ("Bot Preference Sync"). Web'deki kendi
  `robots.txt`'nizin başına Cloudflare'in bölümü ekleniyor. Bu ayarlar bitince
  `robots.txt`'te yukarıdaki açık botlar `Disallow` listesinden çıkacak.
- ⚠️ Web'in kendi `robots.txt`'i bu botları ayrıca `Disallow` ediyorsa Cloudflare
  ayarıyla çelişir. Kontrol edip varsa kaldırın.

## Sizden istenenler

1. Web'e sunucu-sunucu çağrısı eklediğinizde bize haber verin; Cloudflare istisnası
   gerekir (§2).
2. Kendi `robots.txt`'nizde yapay zekâ botlarına `Disallow` varsa kaldırın (§5).
3. Hız kuralları `/firma…` ve `/kurul…` sayfalarını henüz kapsamıyor. Bu sayfaların gerçek
   yollarını iletin; kurala ekleyelim.
4. Yerelde ya da test ortamında `ihaletakip.com`'a Cloudflare üzerinden otomatik test
   (Playwright, curl) koşacaksanız bot moduna takılır. Testleri doğrudan sunucuya ya da
   localhost'a yönlendirin.
