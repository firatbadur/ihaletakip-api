"""
Altın soru setini gerçek agent döngüsünden geçirir ve sonucu tablo olarak basar.

⚠️ Ücretli ve yavaştır (soru başına bir ya da birkaç LLM çağrısı). Birim test değildir;
`manage.py test` bunu ÇALIŞTIRMAZ. Amacı: `ASSISTANT_AGENT_MODEL` ya da persona
değiştirildiğinde "daha ucuz/farklı ama hâlâ doğru mu?" sorusunu tek komutla yanıtlamak.
Bu karşılaştırma daha önce elle yapılıyordu ve her seferinde farklı sorularla yapıldığı
için sonuçlar kıyaslanabilir değildi.
"""
import time
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

SET_YOLU = Path(__file__).resolve().parent.parent.parent / "tests" / "golden.yaml"

# (giriş, çıkış) $/milyon token. Cache okuma girişin ~%10'u, cache yazma ~%125'i.
# ⚠️ Modele göre seçilir: tek bir fiyat listesine sabitlemek, kademe karşılaştırmasında
# ucuz modelin maliyetini pahalı modelin fiyatıyla hesaplayıp kararı bozar.
_FIYAT = {
    "opus-5": (5.0, 25.0),
    "opus-4-8": (5.0, 25.0),
    "opus-4-7": (5.0, 25.0),
    "sonnet-5": (2.0, 10.0),
    "sonnet-4-6": (3.0, 15.0),
    "haiku-4-5": (1.0, 5.0),
    "fable-5": (10.0, 50.0),
}


def _fiyat(model: str):
    for ad, ucret in _FIYAT.items():
        if ad in model:
            return ucret
    return None


class Command(BaseCommand):
    help = "İhale Asistanı altın soru setini çalıştırır (gerçek LLM çağrısı yapar)."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="Profili kullanılacak kullanıcı e-postası")
        parser.add_argument("--sadece", type=int, help="Yalnızca N. soruyu çalıştır (1'den başlar)")

    def handle(self, *args, **opts):
        from django.contrib.auth import get_user_model

        from assistant.models import CompanyProfile
        from assistant.services.agent import sohbet_turu
        from assistant.tools.context import ToolContext

        user = get_user_model().objects.filter(email=opts["user"]).first()
        if not user:
            raise CommandError(f"Kullanıcı bulunamadı: {opts['user']}")
        profile = CompanyProfile.objects.filter(user=user).select_related("contractor").first()
        if not profile:
            raise CommandError("Bu kullanıcının firma profili yok.")

        sorular = yaml.safe_load(SET_YOLU.read_text(encoding="utf-8"))
        if opts.get("sadece"):
            sorular = sorular[opts["sadece"] - 1 : opts["sadece"]]

        model = settings.ASSISTANT_AGENT_MODEL
        fiyat = _fiyat(model)
        if not fiyat:
            self.stdout.write(self.style.WARNING(
                f"{model} için fiyat tanımlı değil; maliyet hesaplanmayacak "
                f"(_FIYAT tablosuna ekleyin)."))
        self.stdout.write(f"Model: {model} | {len(sorular)} soru | kullanıcı: {user.email}\n")

        gecen, toplam_usd, toplam_sn = 0, 0.0, 0.0
        for i, s in enumerate(sorular, 1):
            ctx = ToolContext(user=user, premium=bool(getattr(user, "is_premium", False)))
            t0 = time.monotonic()
            try:
                sonuc = sohbet_turu(
                    ctx, profile.profile_map, "Bugünün tarihi: bugün",
                    [{"role": "user", "content": s["soru"]}],
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"{i}. {s['soru'][:45]} → ÇÖKTÜ: {e}"))
                continue
            sure = time.monotonic() - t0
            toplam_sn += sure

            metin = (sonuc["metin"] or "").lower()
            cagrilan = {a["arac"] for a in sonuc["arac_izi"]}
            kart = len([k for k in ctx.card_pool if k in (ctx.son_grup or [])])

            sorunlar = []
            beklenen = set(s.get("arac_bekle") or [])
            if beklenen and not (beklenen & cagrilan):
                sorunlar.append(f"araç yok ({', '.join(sorted(beklenen))})")
            for k in s.get("icermeli") or []:
                if k.lower() not in metin:
                    sorunlar.append(f"eksik: {k!r}")
            for k in s.get("icermemeli") or []:
                if k.lower() in metin:
                    sorunlar.append(f"YASAK: {k!r}")
            if kart < (s.get("kart_bekle") or 0):
                sorunlar.append(f"kart {kart}<{s['kart_bekle']}")

            u = sonuc.get("usage") or {}
            if fiyat:
                g, c = fiyat
                usd = (
                    u.get("input_tokens", 0) * g
                    + u.get("output_tokens", 0) * c
                    + u.get("cache_read_input_tokens", 0) * g * 0.1
                    + u.get("cache_creation_input_tokens", 0) * g * 1.25
                ) / 1e6
                toplam_usd += usd

            if sorunlar:
                self.stdout.write(self.style.ERROR(
                    f"{i}. ✗ {s['soru'][:42]:<42} {sure:>5.0f}sn tur={sonuc['turlar']} "
                    f"| {'; '.join(sorunlar)}"))
            else:
                gecen += 1
                self.stdout.write(self.style.SUCCESS(
                    f"{i}. ✓ {s['soru'][:42]:<42} {sure:>5.0f}sn tur={sonuc['turlar']} "
                    f"araç={','.join(sorted(cagrilan)) or '-'}"))

        n = max(len(sorular), 1)
        satir = f"\n{gecen}/{len(sorular)} geçti | ortalama {toplam_sn/n:.0f} sn"
        if fiyat:
            satir += (f" | ~${toplam_usd:.3f} toplam, ${toplam_usd/n:.4f}/soru "
                      f"(~{toplam_usd/n*42:.2f} TL)")
        self.stdout.write(satir)
