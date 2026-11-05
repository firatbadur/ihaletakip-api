# Web portalı 8. not (#27–#32) — API yanıtı

> **Okuyucu:** web portalı ekibi (`ihaletakip-web`) · **Hazırlayan:** `ihaletakip-api` ·
> **Tarih:** 2026-11-05 · **İlgili:** `portal-istekleri-8.md`, `docs/web-cloudflare-ayarlari.md`

| # | Konu | Durum | Web'den iş |
|---|---|---|---|
| #27 | Dolum kovasında İKN araması ayrı bütçe | ✅ yapıldı | ✅ 429 ele alımı yapıldı |
| #28 | Hesap başına farklı IP / UA | ✅ yapıldı | yok |
| #29 | Cloudflare dışından gelen trafik | ✅ yanıt + düzeltme | yok |
| #30 | Hız kuralına girişli liste yolları | ✅ yapıldı, test edildi | yok |
| #31 | AI Crawl Control: eğitim botları kapalı | ✅ uygulandı | ✅ `robots.ts` düzeltildi (`2f557dd`), canlıda doğrulandı |
| #32 | Veri merkezi ASN'si + Turnstile ön onayı | ✅ yapıldı | yok |

**8. not kapandı (2026-11-05).** #30–#32 Cloudflare panelinde uygulandı; web'in iki işi
(#27, #31) yapıldı ve canlıda doğrulandı.

---

## #27 Dolum kovasında İKN araması ayrı bütçe ✅

**Ne değişti**

- `GET /ekap/tenders/?ikn_yil=…&ikn_sayi=…` artık kendi kapsamında (`ikn_arama`).
  Dolum isteği olduğunda liste dolum kovasını (200/dk) **hiç tüketmez**.
- İKN araması **sorgudan sonra, sonuca göre** sayılır (dakikada):

  | sonuç | dakika sınırı |
  |---|---|
  | bulunan (`totalCount > 0`) | 300 |
  | boş (`totalCount = 0`) | 60 |

  Böylece İKN sayan bir betik yalnızca boş aramalar bütçesini doldurur. Var olan İKN'yi
  açan kullanıcı ve arama motoru bundan etkilenmez.
- DB'yi korumak için ayrıca İKN dolumuna dakikada 1.500'lük bir emniyet tavanı var.
  Normal trafikte bu tavana erişilmez.
- Girişli ya da `X-IT-Client-IP` taşıyan İKN araması dolum değildir. O istek kişinin kendi
  kovasında sayılır (oranlar ihale aramasıyla aynı), boş İKN bütçesine girmez.
- Liste ve detay dolum kovaları değişmedi (200/dk ve 300/dk).

**Web'den beklenen**

Bütçe aşılınca İKN araması **429** döner. Gövdede `errors.code = "throttled"` bulunur,
`Retry-After` başlığında saniye gelir. Bu yanıt "ihale yok" demek değildir:

- `/ihale/<yıl>-<sayı>` sayfası bu durumda **404 göstermemeli**. Geçici hata göstermeli
  (503 / "biraz sonra tekrar deneyin").
- Bu yanıt önbelleğe alınmamalı.

404 gösterilirse arama motoru var olan bir ihaleyi "yok" diye kaydedebilir.

**Web yanıtı (2026-11-05):**

- İhale sayfası zaten doğru davranıyordu: hata önbelleğe girmiyor, sayfa "Çok fazla istek
  gönderildi" ve "Tekrar dene" gösteriyor.
- `/belge` bu hatayı düz 500'e çeviriyordu; düzeltildi, artık 429 ve anlaşılır bir mesaj
  dönüyor.

**Bilinen sınır (dürüst not)**

Var olan İKN'leri sayan bir betik **detay** dolum kovasını (300/dk) yine tüketebilir. Bu
sınırlı bir saldırıdır: ~1 milyon ihale bu hızla ~55 saat sürer ve sizin önbelleğiniz
dolduğu için aynı sayfa ikinci kez gelmez. Dağıtık (dönen IP) kazıyıcıya karşı asıl önlem
Cloudflare tarafındadır: IP başına hız kuralı ve #32'deki veri merkezi kuralı.

**Görünürlük**

`/admin/guvenlik/` sayfasında yeni bir kart var: "Web önbellek dolumu — son 60 dakika".
Türler şunlar: ihale listesi, İKN bulunan, İKN boş, firma listesi, detay, reddedilen.
Her tür için 60 dakikalık toplam, son dakika, zirve, dakika sınırı ve dakikalık grafik
gösteriliyor.

## #28 Hesap başına farklı IP / UA ✅

- Girişli her istekte son 1 saatte görülen farklı IP ve tarayıcı sayısı tutuluyor. Kaynak
  portalda `X-IT-Client-IP` / `X-IT-Client-UA`, mobilde gerçek IP ve UA.
- Güvenlik Paneli'nde "1 sa IP/UA" kolonu eklendi. 10 ve üstü IP kırmızı görünür.
- Şüphe puanına etkisi:
  - 1 saatte 10+ farklı IP → +2
  - 1 saatte 5+ farklı tarayıcı → +1
- ⚠️ İsteğiniz doğrultusunda bu sinyal **otomatik askıya sayılmaz**, yalnızca panelde
  işarettir. Mobil ağda IP sık değişiyor.
- Eşikler ayarla değiştirilebilir (`GUVENLIK_PAYLASIM_IP`, `GUVENLIK_PAYLASIM_UA`).

## #29 API'ye Cloudflare dışından gelen trafik var mı? ✅

**Yanıt: hayır.** Origin'in 443'ü 2026-09-16'dan beri yalnızca Cloudflare'in 15 IPv4
aralığına açık. Diğer her kaynak `DOCKER-USER` zincirinde düşürülüyor (bugün itibarıyla
20.927 paket). Notunuzdaki "443 herkese açık görünüyor" gözlemi bu kilitten önceki duruma
aitti. Doğrudan IP'ye `Host: ihaletakip.com` ile bağlanma denemesi bağlantı kurmadan düşer.

Cloudflare dışından gelip etkilenebilecek şeyleri tek tek kontrol ettik:

| kaynak | yol | kilitten etkilenir mi |
|---|---|---|
| mobil uygulama | `ihale-takip.envisoft.com.tr` (Cloudflare proxy'li) | hayır |
| RevenueCat, SES/SNS webhook'ları | API zone'u, Cloudflare üzerinden | hayır |
| `ihaletakip-social` blog botu | API zone'u, Cloudflare üzerinden | hayır |
| web konteyneri → API | `host-gateway` (Docker köprüsü, `eth0` değil) | hayır |
| sağlık kontrolü | sunucu içinden `localhost` | hayır |

**Bu kontrolde bulduğumuz ve kapattığımız açık:** Docker açılışta `DOCKER-USER` zincirini
boşaltıyor. Sunucu 2026-10-19'da yeniden başladı:

- 443 kilidi bir hafta sonra, haftalık tazeleme cron'uyla geri geldi. O hafta origin
  Cloudflare'i atlayarak erişilebilirdi.
- 80 kilidi bugüne kadar geri gelmemişti. 80 aynı sunucudaki başka bir projenin portu,
  ihaletakip'i ilgilendirmez.

Bugün yapılanlar:

- Yeni bir systemd birimi kilidi Docker'ın **her açılışında** iki port için yeniden kuruyor.
- Cloudflare aralık listesinin son doğrulanmış kopyası saklanıyor. Açılışta ağ hazır değilse
  kilit bu listeyle kuruluyor.
- 80 için de haftalık tazeleme eklendi.
- Kontrol edildi: üç site de Cloudflare üzerinden 200 dönüyor, doğrudan bağlantı düşüyor.

**Authenticated Origin Pulls ✅ (aynı gün, zorunlu):**

- Kilit yalnızca "kaynak bir Cloudflare IP'si mi" diye bakıyordu. Başka bir Cloudflare
  hesabında sunucu IP'mize yönlendirilmiş bir alan adı bu kontrolü geçerdi.
- Artık iki zone'umuz (ihaletakip.com, envisoft.com.tr) origin'e bağlanırken kendi CA'mızla
  imzalı istemci sertifikasını sunuyor.
- nginx geçerli sertifika taşımayan isteğe 403 dönüyor. Docker ağından gelen iç trafik
  (sizin API çağrılarınız) bu kuraldan muaf.
- Önce gözlem aşamasında çalıştı: Cloudflare'den gelen her istek doğrulandı. Ardından
  zorunlu kılındı.
- Sınama: sunucu IP'sine sertifikasız istek 403, bizim sertifikamızla 200. Cloudflare
  üzerinden web, API ve önbelleğe girmeyen bir arama 200.
- Sizden bir şey gerekmiyor.

## #30 Hız kuralına girişli liste yolları ✅

- "Sayfa tarama sınırı" kuralına `/yuklenici`, `/idare`, `/kurul-kararlari` ve `/bildirimler`
  önekleri eklendi.
- Pro planda en fazla iki hız kuralı açılabiliyor ve ikisi de dolu. Bu yüzden yeni kural
  açılmadı, mevcut kural genişletildi; sınır yine IP başına dakikada 40 sayfa. Yeni bir
  liste sayfası eklerseniz yolunu bildirin, aynı kurala ekleriz.
- **Test (2026-11-05):** Chrome TLS parmak izli istemciyle `/idareler`'e art arda 45 istek
  atıldı. İlk 39'u 200 döndü, sonrakiler `429` + `cf-mitigated: challenge` aldı.

## #31 AI Crawl Control: eğitim botları kapalı ✅ (web'den bir düzeltme gerekiyor)

Kararınız doğru; bizim notumuzdaki tablo (`web-cloudflare-ayarlari.md` §5) yanlıştı ve
düzeltildi.

**Cloudflare ayarı (uygulandı):**

| Kategori | Ayar |
|---|---|
| Search | Allow |
| Training | Disallow |
| Agents | Allow |
| Bot Preference Sync | açık |

**Canlı `robots.txt` (2026-11-05):** Cloudflare bölümü şunları içeriyor:

- Genel satır `Content-Signal: search=yes,ai-train=no,use=reference` ve `Allow: /`.
- Şu eğitim botlarının her biri için `Disallow: /`: GPTBot, ClaudeBot, anthropic-ai,
  Google-Extended, Applebot-Extended, CCBot, Bytespider, meta-externalagent, Amazonbot,
  Diffbot, omgili. Cloudflare bunlara ek olarak cohere-ai, MistralAI-Training, GoogleOther
  gibi başka eğitim botlarını da kapatıyor.
- Arama ve asistan botları (OAI-SearchBot, ChatGPT-User, Claude-SearchBot, Claude-User,
  PerplexityBot, Perplexity-User) engellenmiyor.

**⚠️ Sizden istenen: kendi eğitim botu grubunuzu kaldırın.**

Sizin bölümünüzde aynı 11 bot için ayrı bir grup var. O grup `Allow: /` ile başlıyor ve
yalnızca `/ihale/`, `/ihaleler` ve özel yolları yasaklıyor. Cloudflare'in grubu ise aynı
botlara `Disallow: /` diyor.

Bir bot için iki grup olunca kurallar birleşiyor. Google'ın kuralına göre eşit uzunluktaki
`Allow: /` ile `Disallow: /` çakışmasında izin kazanıyor. Sonuçta bu botlar anasayfayı ve
blogu eğitim için okuyabiliyor. Notunuzda uyardığınız çelişki tam olarak bu.

Eğitim botlarını artık Cloudflare'in bölümü tamamen kapatıyor. Sizin
`User-Agent: GPTBot … omgili` grubunuz `src/app/robots.ts`'ten kaldırılmalı. Genel
`User-Agent: *` bölümünüz aynen kalabilir.

**Web yanıtı (2026-11-05):** grup `src/app/robots.ts`'ten kaldırıldı (`2f557dd`).
Lokalde `robots.txt` artık yalnız genel `User-Agent: *` bölümünü üretiyor. **Canlı kontrol (2026-11-05, yayından sonra):**

- Cloudflare bölümünde 11 eğitim botunun her biri `Disallow: /` ve `ai-train=no` taşıyor.
- Web bölümünde yalnızca `User-Agent: *` grubu ve `Sitemap` satırı var.
- Çakışma kalmadı.

Not: Cloudflare'in "Disallow" ayarı yalnızca `robots.txt`'e yazar, isteği kesmez. Kuralı
tanımayan botları (ör. Bytespider) Super Bot Fight Mode ve hız kuralları yakalar.

## #32 Veri merkezi ASN'si + Turnstile ön onayı ⏳

- ✅ Custom rule önerdiğiniz ifadeyle, Managed Challenge eylemiyle eklendi. Sırada blog
  yenileme istisnasının **altında**, yani ikinci kural. API sunucusu da Contabo'da
  (AS51167), `/api/revalidate` istisnası bu yüzden önde kalıyor.
- ✅ Turnstile widget'ında (`web-widget`) ön onay açıldı ("Skip future security rule
  challenges for verified visitors"), mod Managed. Sitedeki kutuyu çözen kullanıcı, Cloudflare'in
  challenge'ını Challenge Passage süresince (varsayılan 30 dk) yeniden görmez. Site key ve
  secret key değişmedi; web'de bir şey yapmanız gerekmiyor.
- **Test (2026-11-05):** aynı istemci (Chrome TLS parmak izi) kullanıldı.
  - Ev bağlantısından `/` → 200.
  - Contabo'daki sunucumuzdan `/` → 403 + `cf-mitigated: challenge`.
  - Aynı sunucudan `/api/revalidate` → challenge yok, sizin kendi yanıtınız (GET'e 405).
- Not: düz `curl` artık her yerden Super Bot Fight Mode'a takılıyor. Bu kuralları sınamak
  için tarayıcı TLS parmak izi gerekiyor.
