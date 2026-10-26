"""nginx yapılandırması: her vhost istemcinin `X-Real-IP`'sini EZMELİ (portal 2. not #9).

Web portalı son kullanıcı IP'sini yalnızca `X-Real-IP`'den okuyup hız sınırına
(`X-IT-Client-IP`) taşıyor; ezilmeyen başlık sahte IP ile sınırı atlatmak demek.
Yorum satırları (henüz kapalı bloklar) denetlenmez — açıldıkları anda denetlenir.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

CONF = Path(settings.BASE_DIR) / "docker" / "nginx" / "default.conf"
SATIR = re.compile(r"proxy_set_header\s+X-Real-IP\s+\$remote_addr\s*;")


def _bloklar(metin, anahtar):
    """`anahtar ... { ... }` bloklarının gövdeleri (iç içe süslü parantez dengeli)."""
    for m in re.finditer(rf"\b{anahtar}\b[^{{;]*{{", metin):
        derinlik, i = 1, m.end()
        while derinlik and i < len(metin):
            derinlik += {"{": 1, "}": -1}.get(metin[i], 0)
            i += 1
        yield metin[m.end():i - 1]


class NginxXRealIpTest(SimpleTestCase):
    def setUp(self):
        self.metin = "\n".join(
            s.split("#", 1)[0] for s in CONF.read_text(encoding="utf-8").splitlines())

    def test_her_server_blogu_x_real_ip_ezer(self):
        sunucular = list(_bloklar(self.metin, "server"))
        self.assertGreaterEqual(len(sunucular), 1)
        for govde in sunucular:
            if "proxy_pass" in govde:
                self.assertRegex(govde, SATIR, "server bloğunda X-Real-IP $remote_addr yok")

    def test_kendi_basligini_tanimlayan_location_x_real_ip_tasir(self):
        # nginx: location içinde tek proxy_set_header → üst seviye başlıklar miras alınmaz.
        for sunucu in _bloklar(self.metin, "server"):
            for loc in _bloklar(sunucu, "location"):
                if "proxy_set_header" in loc:
                    self.assertRegex(loc, SATIR, "location kendi başlığını tanımlıyor "
                                                 "ama X-Real-IP'yi ezmiyor")
