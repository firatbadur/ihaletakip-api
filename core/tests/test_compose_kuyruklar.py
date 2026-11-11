"""
`CELERY_TASK_ROUTES`'taki HER kuyruğu bir compose servisi dinlemeli.

Dinlenmeyen kuyruktaki görev hata vermez, sessizce sonsuza dek bekler — yeni bir kuyruk
eklenip compose servisi unutulursa (ör. `assistant_ek`) arıza ancak kullanıcı "dosyam
hiç hazırlanmıyor" deyince görülürdü.
"""
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase

COMPOSE = Path(settings.BASE_DIR) / "docker-compose.yml"


def dinlenen_kuyruklar() -> set:
    servisler = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    kuyruklar = set()
    for servis in servisler.values():
        komut = servis.get("command") or []
        if isinstance(komut, str):
            komut = komut.split()
        if "worker" not in komut:
            continue
        if "-Q" in komut:
            kuyruklar.update(komut[komut.index("-Q") + 1].split(","))
        else:
            kuyruklar.add("celery")  # -Q verilmeyen worker varsayılan kuyruğu dinler
    return kuyruklar


class ComposeKuyrukTest(SimpleTestCase):
    def test_her_rota_kuyrugu_dinleniyor(self):
        dinlenen = dinlenen_kuyruklar()
        rotalar = {r["queue"] for r in settings.CELERY_TASK_ROUTES.values() if "queue" in r}
        self.assertTrue(rotalar)
        self.assertEqual(rotalar - dinlenen, set(), "bu kuyrukları dinleyen compose servisi yok")

    def test_varsayilan_kuyruk_dinleniyor(self):
        # Rotası olmayan görevler `celery` kuyruğuna gider.
        self.assertIn("celery", dinlenen_kuyruklar())
