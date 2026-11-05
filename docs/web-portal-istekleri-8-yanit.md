# Web portalı 8. not (#27–#32) — API yanıtı

> **Okuyucu:** web portalı ekibi (`ihaletakip-web`) · **Hazırlayan:** `ihaletakip-api` ·
> **Tarih:** 2026-11-05 · **İlgili:** `portal-istekleri-8.md`, `docs/web-cloudflare-ayarlari.md`

| # | Konu | Durum | Web'den iş |
|---|---|---|---|
| #27 | Dolum kovasında İKN araması ayrı bütçe | ✅ yapıldı | 429 ele alımı (aşağıda) |
| #28 | Hesap başına farklı IP / UA | ✅ yapıldı | yok |
| #29 | Cloudflare dışından gelen trafik | ✅ yanıt + düzeltme | yok |
| #30 | Hız kuralına girişli liste yolları | ⏳ Cloudflare panelinde uygulanıyor | yok |
| #31 | AI Crawl Control: eğitim botları kapalı | ⏳ Cloudflare panelinde uygulanıyor | `robots.txt` çıktısını birlikte kontrol |
| #32 | Veri merkezi ASN'si + Turnstile ön onayı | ⏳ Cloudflare panelinde uygulanıyor | yok |

Panel adımları bitince #30–#32 için test sonuçlarıyla bu not güncellenecek.

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

**Kalan risk ve sonraki adım:** kilit "kaynak bir Cloudflare IP'si mi" diye bakıyor.
Başka bir Cloudflare hesabında sunucu IP'mize yönlendirilmiş bir alan adı bu kontrolü geçer
ve bizim kurallarımız ona uygulanmaz. Bunu kapatan şey zone'a özel sertifikayla
Authenticated Origin Pulls. Bu ayrı ve kontrollü bir adım olarak sonra yapılacak: nginx'te
istemci sertifikası doğrulaması yanlış kurulursa API ve web birlikte düşer.

## #30 Hız kuralına girişli liste yolları ⏳

"Sayfa tarama sınırı" kuralına `/yuklenici`, `/idare`, `/kurul-kararlari` ve `/bildirimler`
önekleri ekleniyor. Bitince kısa bir sınama yapılıp sonuç buraya yazılacak.

## #31 AI Crawl Control: eğitim botları kapalı ⏳

Kararınız doğru; bizim notumuzdaki tablo (`web-cloudflare-ayarlari.md` §5) yanlıştı ve
düzeltildi.

- Açık: Googlebot, Bingbot, Applebot, OAI-SearchBot, ChatGPT-User, Claude-SearchBot,
  Claude-User, PerplexityBot, Perplexity-User.
- Kapalı: GPTBot, ClaudeBot, anthropic-ai, CCBot, Google-Extended, Applebot-Extended,
  Bytespider, meta-externalagent, Amazonbot, Diffbot, omgili.

Cloudflare'de ayar bitince `robots.txt` çıktısını size göndereceğiz. Birlikte kontrol
ederiz.

## #32 Veri merkezi ASN'si + Turnstile ön onayı ⏳

- Custom rule önerdiğiniz ifadeyle, Managed Challenge eylemiyle ekleniyor. Sırada blog
  yenileme istisnasının **altında**, yani ikinci kural. API sunucusu da Contabo'da
  (AS51167), `/api/revalidate` istisnası bu yüzden önde kalıyor.
- Turnstile widget'ında Pre-clearance = Managed açılıyor.
- Sınama planı:
  - Contabo'daki sunucumuzdan `/` → challenge beklenir.
  - `/api/revalidate` → sizin kendi 401'iniz beklenir.
