"""
EKAP insan doğrulaması (Turnstile) çerezini yönetir.

Kullanım:
    # durumu göster (EKAP'a canlı sorar)
    python manage.py ekap_dogrula

    # yeni çerezi kaydet — tarayıcıdan kopyalanan `Cookie:` başlığı
    python manage.py ekap_dogrula --cookie "ekap.human-verification=...; TS0...=..."

    # uzun çerezi kabuk tırnaklarıyla boğuşmadan vermek için stdin
    pbpaste | python manage.py ekap_dogrula --stdin

Çerezin nasıl alınacağı: tarayıcıda ekapv2.kik.gov.tr/ekap/search açılır,
"robot değilim" doğrulaması geçilir, F12 → Network → herhangi bir
`GetListByParameters` isteği → Request Headers → `Cookie:` satırı kopyalanır.
⚠️ Application → Cookies YETMEZ: doğrulamayı taşıyan `ekap.human-verification`
HttpOnly'dir ve orada görünmez.
"""
from django.core.management.base import BaseCommand

from ekap import session as ekap_session


class Command(BaseCommand):
    help = "EKAP insan doğrulaması çerezini gösterir / kaydeder."

    def add_arguments(self, parser):
        parser.add_argument("--cookie", help="tarayıcıdan kopyalanan Cookie başlığı")
        parser.add_argument("--stdin", action="store_true",
                            help="çerezi standart girdiden oku")
        parser.add_argument("--sil", action="store_true", help="kayıtlı çerezi siler")

    def handle(self, *args, **o):
        if o["sil"]:
            from core.models import AppSetting
            AppSetting.objects.filter(key=ekap_session.ANAHTAR_CEREZ).delete()
            from django.core.cache import cache
            cache.delete("ekap:dogrulama:cerez")
            self.stdout.write(self.style.WARNING("Çerez silindi."))
            return

        ham = o.get("cookie")
        if o["stdin"]:
            import sys
            ham = sys.stdin.read()
        if ham:
            temiz = ekap_session.kaydet(ham)
            self.stdout.write(self.style.SUCCESS(
                f"✅ Çerez kaydedildi — {ekap_session.maskele(temiz)}"))
            if "ekap.human-verification" not in temiz:
                self.stdout.write(self.style.ERROR(
                    "⚠️ `ekap.human-verification` çerezi YOK. Doğrulamayı taşıyan "
                    "çerez budur ve HttpOnly olduğu için yalnızca Network "
                    "sekmesindeki `Cookie:` başlığında görünür."))

        # Mevcut durum
        self.stdout.write(f"\n📦 Kayıtlı çerez: {ekap_session.maskele(ekap_session.cerez())}")
        self.stdout.write("📡 EKAP'a soruluyor...")
        try:
            d = ekap_session.durum()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"❌ Durum sorgulanamadı: {e}"))
            return

        if d.get("http") != 200:
            self.stderr.write(self.style.ERROR(
                f"❌ HTTP {d.get('http')} — {d.get('hata', '')}"))
            return
        if d.get("verified"):
            self.stdout.write(self.style.SUCCESS(
                f"✅ Doğrulama GEÇERLİ — bitiş: {d.get('expiresAtUtc')} "
                f"(yenileme: {d.get('refreshAtUtc')})"))
        else:
            self.stdout.write(self.style.ERROR(
                f"❌ Doğrulama YOK (enabled={d.get('enabled')}). Toplama duracak; "
                f"tarayıcıda doğrulayıp çerezi --cookie ile kaydedin."))
