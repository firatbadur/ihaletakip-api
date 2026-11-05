# Web portalı — Cloudflare ayarları (ihaletakip.com)

> **Okuyucu:** web portalı ekibi (`ihaletakip-web`) · **Hazırlayan:** `ihaletakip-api` ·
> **Tarih:** 2026-11-05 (8. not yanıtıyla güncellendi) · **Durum:** §1–§4 uygulandı ve test
> edildi; §1 yeni yollar, §5 yapay zekâ botları, §6 ASN kuralı ve Turnstile ön onayı uygulandı. Bağlam: `docs/web-guvenlik-sayfa-siniri.md`
> #26 madde 1–2, `docs/web-portal-istekleri-8-yanit.md`.

## Özet

`ihaletakip.com` zone'u **Pro** plana geçirildi ve şu korumalar açıldı:

| Ayar | Ne yapar | Durum |
|---|---|---|
| Hız kuralı: **Sayfa tarama sınırı** | Aynı IP dakikada 40'tan fazla sayfa açarsa "robot değilim" ekranı | ✅ test edildi |
| Hız kuralı: **İndirme sınırı** | Aynı IP dakikada 15'ten fazla `/belge` isterse aynı ekran | ✅ test edildi |
| **Super Bot Fight Mode** | Otomasyon araçlarına (curl, HeadlessChrome, script) challenge | ✅ test edildi |
| **Cloudflare Managed Ruleset** | Bilinen saldırı kalıplarını keser | ✅ açık |
| Custom rule: **Sunucu istekleri — bot korumasından muaf** | API sunucusunun `/api/revalidate` çağrısı bot korumasına takılmaz | ✅ test edildi |
| Yapay zekâ botları | Arama/asistan botları açık; model eğitim botları ve veri satıcıları kapalı | ✅ uygulandı |
| Custom rule: **Veri merkezi ağları** | Bulut/barındırma ASN'lerinden gelene challenge | ✅ test edildi |
| Origin kilidi | Sunucunun 443'ü yalnızca Cloudflare'e açık | ✅ (§7) |

Mobil uygulama bu ayarlardan etkilenmez: API ayrı bir zone'da (`ihale-takip.envisoft.com.tr`).
Web'in API'ye giden sunucu istekleri de Cloudflare'i atladığı için (`host-gateway`) etkilenmez.

## 1. Hız kuralları (Security → Security rules → Rate limiting rules)

**Sayfa tarama sınırı**

```
(starts_with(http.request.uri.path, "/ihale")
 or starts_with(http.request.uri.path, "/dogrudan-temin")
 or starts_with(http.request.uri.path, "/belge")
 or starts_with(http.request.uri.path, "/yuklenici")
 or starts_with(http.request.uri.path, "/idare")
 or starts_with(http.request.uri.path, "/kurul-kararlari")
 or starts_with(http.request.uri.path, "/bildirimler"))
and not cf.client.bot
and not (http.request.uri.query contains "_rsc=")
```

- Sayaç: IP başına, **40 istek / 1 dakika** → **Managed Challenge**.
- `/ihale` hem `/ihaleler`'i hem `/ihale/<ikn>`'i kapsar. `/yuklenici` firma listesi ve
  sayfasını, `/idare` idareler ve idare sayfasını kapsar (8. not #30). Pro planda en fazla iki
  hız kuralı var ve ikisi dolu: yeni yollar bu kurala eklenir, yeni kural açılamaz.
- Test (8. not #30): `/idareler`'e 45 istek → 39 × 200, sonrası `429` challenge.
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

## 5. Yapay zekâ botları (AI Crawl Control)

Ürün kararı (2026-10-28, 2026-11-05'te yeniden onaylandı): ihaletakip.com yapay zekâ
**arama ve asistan** cevaplarında kaynak gösterilebilsin (GEO). **Model eğitimine** ve veri
satıcılarına içerik verilmez.

| Açık (arama, asistan, kaynak gösterme) | Kapalı (model eğitimi, veri satıcıları) |
|---|---|
| Googlebot, Bingbot, Applebot | GPTBot, ClaudeBot, anthropic-ai |
| OAI-SearchBot, ChatGPT-User | Google-Extended, Applebot-Extended |
| Claude-SearchBot, Claude-User | CCBot, Bytespider, meta-externalagent |
| PerplexityBot, Perplexity-User | Amazonbot, Diffbot, omgili |

- ⚠️ Bu notun ilk sürümü GPTBot, ClaudeBot, Google-Extended ve Applebot-Extended'i "açık"
  yazıyordu. O tablo yanlıştı, düzeltildi.
- `robots.txt` Cloudflare tarafından yönetiliyor ("Bot Preference Sync"). Web'in kendi
  `robots.txt`'inin başına Cloudflare'in bölümü ekleniyor. İki bölüm aynı listeyi
  söylemeli: aynı bot için iki grup birleşir ve sonuç bota göre değişir.
- Panel ayarı (Security → Settings → AI Crawler and Agent access policies): Search = Allow,
  Training = Disallow, Agents = Allow, Bot Preference Sync açık. Canlı `robots.txt`'te
  eğitim botları `Disallow: /` olarak görünüyor (2026-11-05).
- ⚠️ Web'in kendi bölümündeki eğitim botu grubu (`Allow: /` + kısmi yasak) Cloudflare'in
  `Disallow: /`'u ile birleşir ve izin kazanır. O grup kaldırılmalı
  (`docs/web-portal-istekleri-8-yanit.md` #31).

## 6. Veri merkezi ağları (Security rules → Custom rules)

```
(not cf.client.bot)
and not starts_with(http.request.uri.path, "/_next/")
and not (http.request.uri.path eq "/api/revalidate")
and ip.src.asnum in {16509 14618 396982 8075 24940 16276 14061 51167 63949 20473 31898 12876 45102 132203}
```

- Aksiyon: **Managed Challenge**. Sırada §3 istisnasının **altında** (2. kural).
- Kapsam: AWS, Google Cloud, Azure, Hetzner, OVH, DigitalOcean, Contabo, Linode, Vultr,
  Oracle, Scaleway, Alibaba, Tencent.
- Google'ın ana ağı (15169) bilerek yok: çeviri ve önizleme vekilleri oradan gelir.
- API sunucusu da Contabo'da (AS51167). `/api/revalidate` istisnası bu yüzden ifadenin
  içinde.
- Test: aynı Chrome TLS parmak izli istemci ev bağlantısından 200, Contabo'dan challenge
  aldı. Contabo'dan `/api/revalidate` challenge'a takılmadı.
- ✅ **Turnstile → `web-widget` → ön onay açık, mod Managed.** Sitedeki "robot değilim" kutusunu
  (API 428) çözen kullanıcı `cf_clearance` alır ve Cloudflare'in challenge'ını bir süre
  yeniden görmez.

## 7. Origin kilidi (sunucu güvenlik duvarı)

- Sunucunun 443'ü (ve aynı sunucudaki 80) yalnızca Cloudflare'in IPv4 aralıklarına açık.
  Diğer her kaynak `DOCKER-USER` zincirinde düşürülür. Doğrudan IP'ye
  `Host: ihaletakip.com` ile gelen istek bağlantı kuramaz. Böylece bu notun kuralları
  atlanamaz.
- Web'in API'ye giden istekleri (`host-gateway`, Docker köprüsü) ve sunucu içi sağlık
  kontrolleri etkilenmez.
- ⚠️ Docker açılışta bu zinciri boşaltıyor. Kilit 2026-11-05'ten beri Docker'ın her
  açılışında systemd ile yeniden kuruluyor. Ondan önce, 2026-10-19 yeniden başlatmasından
  sonra bir hafta açık kalmıştı.
- Kalan açık: başka bir Cloudflare hesabından sunucu IP'sine yönlendirme. Bunun çözümü
  Authenticated Origin Pulls; ayrı bir adım olarak planlandı.

## Sizden istenenler

1. Web'e sunucu-sunucu çağrısı eklediğinizde bize haber verin; Cloudflare istisnası
   gerekir (§2).
2. `robots.txt`'nizdeki yapay zekâ listesi §5'teki kapalı listeyle aynı kalsın. ~~Disallow'u
   kaldırın~~: bu istek yanlıştı, geri alındı.
3. ~~Firma ve kurul sayfalarının yollarını iletin~~: iletildi (8. not #30), kurala ekleniyor.
   Yeni bir liste sayfası açarsanız yolunu bildirin.
4. Yerelde ya da test ortamında `ihaletakip.com`'a Cloudflare üzerinden otomatik test
   (Playwright, curl) koşacaksanız bot moduna takılır. Testleri doğrudan sunucuya ya da
   localhost'a yönlendirin.
