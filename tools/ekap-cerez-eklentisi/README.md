# EKAP Oturum Köprüsü (Chrome eklentisi)

EKAP 2026-09-08'de Cloudflare Turnstile insan doğrulaması koydu; doğrulama
**~8 dakika** yaşıyor ve sunucudaki otomasyon tarayıcısı **reddediliyor**.
Buna karşılık **gerçek bir insanın gerçek tarayıcısı** doğrulamayı geçiyor ve
EKAP arayüzü açık sekmede oturumu kendi kendine tazeliyor.

Bu eklenti yalnızca **tazelenen çerezi sunucuya taşır**. CAPTCHA çözmez,
tıklama taklit etmez, tarayıcı kimliğini gizlemez.

## Kurulum

1. Chrome → `chrome://extensions` → sağ üstte **Geliştirici modu** açık.
2. **Paketlenmemiş öğe yükle** → bu klasörü seç.
3. Eklenti simgesine tıkla:
   - **Sunucu adresi**: `https://ihale-takip.envisoft.com.tr`
   - **Paylaşılan sır**: sunucudaki `.env.prod` → `EKAP_COOKIE_PUSH_TOKEN`
   - **Kaydet**

## Kullanım

1. Bir sekmede `https://ekapv2.kik.gov.tr/ekap/search` aç, doğrulamayı geç.
2. **Sekmeyi açık bırak.** Arayüz oturumu kendi tazeler; eklenti 2 dakikada bir
   çerezi kontrol eder ve değiştiyse sunucuya gönderir.
3. Durumu eklenti penceresinden görebilirsin ("✅ gönderildi + saat").

⚠️ Bilgisayar açık ve sekme açık kaldığı sürece toplama çalışır. Sekme
kapanırsa doğrulama ~8 dakika içinde düşer ve admin panosundaki şerit kırmızıya
döner.

⚠️ Gönderim yalnızca **çerez değiştiğinde** yapılır; her turda göndermek sunucuya
ve loglara gereksiz yük bindirirdi.
