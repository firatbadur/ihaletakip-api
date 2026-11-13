"""
EKAP v2 insan doğrulamasını (Cloudflare Turnstile) **otomatik çözüp** çerezi taze tutan servis.

Çalıştırma: `python manage.py ekap_oturum_daemon` (docker: `ekap-browser` servisi, Xvfb altında).
Tek seferlik deneme: `--tek-tur` (karar beklemeden bir kez çözer, sonucu basar).

⚠️⚠️ 2026-11-13'e kadar bu servis CAPTCHA ÇÖZMÜYORDU (bir kişinin noVNC'den tıklaması
bekleniyordu) ve Cloudflare otomasyon tarayıcısını reddettiği için durdurulmuştu. Ürün sahibi
kararıyla artık patchright + Xvfb ile çözüyor (sunucu IP'sinden ölçüldü: 2/2, ~6 sn).
Mantık ve gerekçeler `ekap/turnstile.py`'de; bu dosya yalnızca döngüdür.

Döngü (her `--kontrol-sn`, panodan istek gelirse ≤5 sn):
  nabız → panodan istek var mı → EKAP doğrulama istiyor mu → `karar_ver` → çöz / sor / bekle.
Hatada üstel geri çekilme (1→15 dk), her olay panoya düşer (`/admin/ekap/dogrulama/`).
"""
import logging
import os
import time

# ⚠️ patchright'ın **sync** API'si bir event loop çalıştırır; Django ORM bunu "async
# bağlam" sayıp `SynchronousOnlyOperation` fırlatabilir. Süreç tek iş parçacıklı, ORM
# çağrıları kısa → belgelenmiş kaçış kapısı (eski daemon'da da vardı).
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

from django.core.cache import cache  # noqa: E402
from django.core.management.base import BaseCommand  # noqa: E402
from django.utils import timezone  # noqa: E402

from ekap import session as ekap_session  # noqa: E402
from ekap import turnstile  # noqa: E402

logger = logging.getLogger("ihaletakip")

UYKU_ADIM_SN = 5


class Command(BaseCommand):
    help = "EKAP Turnstile doğrulamasını otomatik çözer ve doğrulama çerezini taze tutar."

    def add_arguments(self, parser):
        parser.add_argument("--tek-tur", action="store_true",
                            help="karar beklemeden bir kez çöz ve çık (test)")
        parser.add_argument("--kontrol-sn", type=int, default=60,
                            help="karar turları arası azami süre (vars. 60 sn)")

    def handle(self, *args, **o):
        if o["tek_tur"]:
            sonuc = turnstile.coz("tek tur (komut satırı)")
            self.stdout.write(str(sonuc))
            return

        turnstile.olay_ekle("servis", "çözücü servisi başladı")
        self.stdout.write("🟢 EKAP Turnstile çözücü başladı")
        while True:
            try:
                bekle = self._tur(o["kontrol_sn"])
            except Exception as e:                   # noqa: BLE001
                # ⚠️ Döngü ASLA ölmez: konteyner yeniden başlarsa sebep kaybolur, panoda
                # yalnızca "nabız yok" görünür. Hata olay günlüğüne yazılıp devam edilir.
                logger.exception("Turnstile döngü hatası")
                turnstile.olay_ekle("hata", f"döngü hatası: {e}")
                bekle = 30
            self._uyu(bekle)

    def _tur(self, kontrol_sn):
        turnstile.kalp_at()
        istek = turnstile.istek_al()
        gerekli = ekap_session.dogrulama_gerekli(canli=True)
        k = turnstile.karar_ver(
            simdi=timezone.now(),
            durum=turnstile.durum_oku(),
            otomatik=turnstile.otomatik_acik(),
            istek=istek,
            gerekli=gerekli,
            dustu=bool(cache.get(ekap_session._DUSTU_CACHE)),
            cerez_var=bool(ekap_session.cerez()),
        )
        if k.eylem == "coz":
            turnstile.coz(k.sebep)
            return kontrol_sn
        if k.eylem == "sor":
            # Çerez köprü eklentisinden / elle gelmiş, bitişini bilmiyoruz → EKAP'a sor.
            d = ekap_session.durum(cerezli=True)
            if d.get("http") != 200:
                logger.warning("Turnstile: çerez durumu sorulamadı (HTTP %s)", d.get("http"))
                return kontrol_sn
            if d.get("verified") and d.get("expiresAtUtc"):
                turnstile.bitis_guncelle(d["expiresAtUtc"])
                turnstile.olay_ekle("bilgi", f"kayıtlı çerez geçerli, bitiş {d['expiresAtUtc'][:16]}Z")
                return UYKU_ADIM_SN
            turnstile.coz("kayıtlı çerez geçersiz")
            return kontrol_sn
        return turnstile.bekleme_suresi(k, kontrol_sn)

    def _uyu(self, sn):
        """Adım adım uyur; panodan "Şimdi çöz" gelirse hemen uyanır."""
        bitis = time.time() + max(1, sn)
        while time.time() < bitis:
            if cache.get(turnstile._ISTEK):
                return
            turnstile.kalp_at()
            time.sleep(min(UYKU_ADIM_SN, max(0.1, bitis - time.time())))
