"""
EKAP insan doğrulaması oturumunu **sürekli ayakta tutan** servis.

⚠️ **Neden var**: EKAP 2026-09-08'de Cloudflare Turnstile insan doğrulaması
koydu ve doğrulamanın ömrü **~8 dakikadır** (ölçüldü 2026-09-09: doğrulama
09:39:48 → bitiş 09:47:48, yenileme 09:45:48). Elle çerez taşımak bu tempoda
imkânsız; doğrulama olmadan EKAP'a giden her istek `HTTP 406` alır ve
toplama + belge URL'leri + canlı proxy uçlarının tamamı durur.

**Çözüm = EKAP'ın kendi arayüzünü sunucuda ayakta tutmak.** Portal zaten bu 8
dakikayı kendi kendine tazeliyor (`scheduleRefresh` → bitişten 2 dk önce
`runVerification`). Burada yaptığımız da aynısı: gerçek bir Chromium'da arama
sayfası açık durur, süresi dolmadan **sayfa yeniden yüklenir** — bu, uygulamanın
normal açılış yoludur (`initialize()` → `ensureVerified()`) — ve tazelenen çerez
`AppSetting`'e yazılır.

⚠️ **CAPTCHA ÇÖZÜLMEZ.** Kutuyu geçip geçmeme kararını Cloudflare verir; burada
ne token üretilir ne de "insanım" tıklaması taklit edilir. Turnstile etkileşim
isterse daemon bunu **`insan_gerekli`** olarak işaretler, loglar ve bir kişinin
noVNC üzerinden tek tık atmasını bekler. Sahte fare/tık üretmek bilinçli olarak
YAPILMAZ — o, kontrolü yanıltmak olurdu.

⚠️ **Kalıcı profil şart** (`EKAP_BROWSER_PROFIL`): Turnstile yerleşik, geçmişi
olan tarayıcı oturumlarını sessiz geçirme eğilimindedir. Her turda temiz profil
açmak her turda etkileşimli kutu demek olurdu.

Çalıştırma: `python manage.py ekap_oturum_daemon` (docker: `ekap-browser` servisi).
Tek seferlik deneme için: `--tek-tur`.
"""
import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from ekap import session as ekap_session

logger = logging.getLogger("ihaletakip")

# Doğrulama ~8 dk yaşıyor; bitişe bu kadar kalınca tazeleriz. EKAP'ın kendi
# arayüzü 2 dk kala yeniliyor — biz biraz daha erken davranıp toplama görevlerinin
# ortasında pencerenin kapanmasını önlüyoruz.
TAZELEME_ESIGI_SN = 180


class Command(BaseCommand):
    help = "EKAP insan doğrulaması oturumunu tarayıcıda ayakta tutar."

    def add_arguments(self, parser):
        parser.add_argument("--tek-tur", action="store_true",
                            help="tek doğrulama yapıp çık (test)")
        parser.add_argument("--kontrol-sn", type=int, default=60,
                            help="durum kontrol aralığı (vars. 60 sn)")
        parser.add_argument("--headless", default=None,
                            help="true/false — vars. EKAP_BROWSER_HEADLESS")
        parser.add_argument("--tanila", action="store_true",
                            help="tek tur + ayrıntılı teşhis (iframe/başlık/ekran görüntüsü)")

    def handle(self, *args, **o):
        from playwright.sync_api import sync_playwright

        base = settings.EKAP_BASE_URL.rstrip("/")
        profil = getattr(settings, "EKAP_BROWSER_PROFIL", "/app/.browser")
        headless = (o["headless"].lower() == "true") if o["headless"] else \
            getattr(settings, "EKAP_BROWSER_HEADLESS", True)

        self.stdout.write(f"🌐 Chromium başlatılıyor (profil={profil}, headless={headless})")
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                profil,
                headless=headless,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                self._dongu(ctx, page, base, o)
            finally:
                ctx.close()

    # ── Döngü ────────────────────────────────────────────
    def _dongu(self, ctx, page, base, o):
        arama_url = f"{base}/ekap/search"
        ilk = True
        while True:
            try:
                d = self._durum(ctx, base)
                kalan = self._kalan_sn(d)
                if ilk or not d.get("verified") or kalan < TAZELEME_ESIGI_SN:
                    ilk = False
                    self.stdout.write(
                        f"🔄 Tazeleniyor (verified={d.get('verified')}, kalan={kalan}sn)")
                    self._tazele(page, arama_url)
                    d = self._durum(ctx, base)
                    kalan = self._kalan_sn(d)

                if d.get("verified"):
                    self._cerez_yaz(ctx, base)
                    self.stdout.write(self.style.SUCCESS(
                        f"✅ {timezone.localtime():%H:%M:%S} doğrulama geçerli, "
                        f"kalan {kalan} sn"))
                else:
                    # ⚠️ Sahte tık YOK: Turnstile etkileşim istiyorsa bir insan
                    # noVNC'den geçmeli. Durum kalıcı olarak işaretlenir.
                    ekap_session.dustu("tarayıcı oturumu doğrulanamadı — "
                                       "Turnstile etkileşim istiyor olabilir")
                    self.stderr.write(self.style.ERROR(
                        "❌ Doğrulanamadı. noVNC'den tarayıcıya bağlanıp "
                        "'robot değilim' kutusunu geçin."))

                if o.get("tanila"):
                    self._tanila(page, d)
                if o["tek_tur"] or o.get("tanila"):
                    return
            except Exception as e:
                logger.exception("ekap_oturum_daemon turu hatası: %s", e)
                self.stderr.write(self.style.ERROR(f"tur hatası: {str(e)[:200]}"))
            time.sleep(o["kontrol_sn"])

    # ── Parçalar ─────────────────────────────────────────
    def _durum(self, ctx, base):
        """Doğrulama durumu — tarayıcının kendi çerezleriyle.

        ⚠️ GET'e `Content-Type` EKLENMEZ (F5 ASM protokol ihlali sayıp 406 verir).
        """
        try:
            r = ctx.request.get(base + ekap_session.DOGRULAMA_YOLU,
                                headers={"Accept": "application/json",
                                         "api-version": "v1"})
            if r.status != 200:
                return {"verified": False, "http": r.status}
            return r.json()
        except Exception as e:
            logger.warning("durum sorgulanamadı: %s", e)
            return {"verified": False, "hata": str(e)[:120]}

    def _tazele(self, page, url):
        """Sayfayı yeniden yükler → uygulamanın kendi `ensureVerified()` yolu koşar.

        Bu, taklit değil uygulamanın normal açılışıdır; doğrulama kararını
        Turnstile/Cloudflare verir.
        """
        page.goto(url, wait_until="domcontentloaded",
                  timeout=settings.EKAP_TIMEOUT * 1000)
        # Turnstile + /verify turu için makul bekleme; networkidle bazı SPA'larda
        # hiç gelmediği için sabit pencere kullanıyoruz.
        page.wait_for_timeout(8000)

    def _kalan_sn(self, d):
        from datetime import datetime
        s = d.get("expiresAtUtc")
        if not d.get("verified") or not s:
            return 0
        try:
            bitis = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return 0
        return int((bitis - timezone.now()).total_seconds())

    def _cerez_yaz(self, ctx, base):
        """Tarayıcı çerezlerini toplayıcının kullanacağı biçimde kaydeder."""
        host = base.split("//", 1)[-1]
        cerezler = [c for c in ctx.cookies() if host.endswith(c["domain"].lstrip("."))]
        ham = "; ".join(f"{c['name']}={c['value']}" for c in cerezler)
        if not ham:
            return
        yeni = ekap_session.temizle(ham)
        if yeni and yeni != ekap_session.cerez():
            ekap_session.kaydet(yeni)

    # ── Teşhis ───────────────────────────────────────────
    def _tanila(self, page, d):
        """Doğrulama neden geçmiyor: kutu mu çıkıyor, sayfa mı yüklenmiyor?

        Turnstile widget'ı `challenges.cloudflare.com` kaynaklı bir iframe olarak
        gömülür. Iframe VARSA kutu gösteriliyor (etkileşim isteniyor olabilir);
        YOKSA sorun doğrulama değil sayfanın kendisidir (yüklenmedi/engellendi).
        """
        self.stdout.write("\n── TEŞHİS ──────────────────────────────")
        try:
            self.stdout.write(f"  url      : {page.url}")
            self.stdout.write(f"  başlık   : {page.title()}")
        except Exception as e:
            self.stdout.write(f"  sayfa okunamadı: {e}")
        try:
            cerceveler = [f.url for f in page.frames]
            self.stdout.write(f"  iframe   : {len(cerceveler)}")
            for u in cerceveler:
                isaret = " ← TURNSTILE" if "challenges.cloudflare.com" in u else ""
                self.stdout.write(f"    - {u[:110]}{isaret}")
        except Exception as e:
            self.stdout.write(f"  iframe okunamadı: {e}")
        self.stdout.write(f"  durum    : {d}")
        try:
            govde = page.evaluate("() => document.body.innerText.slice(0, 400)")
            self.stdout.write(f"  metin    : {' '.join(govde.split())[:300]}")
        except Exception:
            pass
        try:
            yol = "/app/.browser/tani.png"
            page.screenshot(path=yol, full_page=False)
            self.stdout.write(f"  ekran    : {yol}")
        except Exception as e:
            self.stdout.write(f"  ekran alınamadı: {e}")
