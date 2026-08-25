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
            # `mesajlar` çok turlu senaryo: ölçütler SON tura uygulanır. Takip soruları
            # ("peki İstanbul'dakiler?") ancak böyle ölçülebilir — tek turlu bir sette
            # asistanın en sinsi hatası (önceki filtreyi düşürmek) hiç görünmez.
            turlar_metni = s.get("mesajlar") or [s["soru"]]
            s.setdefault("soru", turlar_metni[-1])
            ctx = ToolContext(user=user, premium=bool(getattr(user, "is_premium", False)))
            gecmis, son_arama, onceki_arama = [], None, None
            t0 = time.monotonic()
            try:
                for j, metin_turu in enumerate(turlar_metni):
                    gecmis.append({"role": "user", "content": metin_turu})
                    baglam = "Bugünün tarihi: bugün"
                    if son_arama:
                        # ⚠️ Üretimdeki davranışın AYNISI (assistant/tasks.py):
                        # sohbet geçmişi metin-only olduğu için önceki arama
                        # parametreleri bağlama ayrıca enjekte edilir.
                        import json as _json

                        baglam += ("\n\nSON ARAMANIN PARAMETRELERİ: "
                                   + _json.dumps(son_arama, ensure_ascii=False)
                                   + "\nKullanıcı bu aramayı daraltıyorsa parametrelerin "
                                     "TAMAMINI yeniden gönder.")
                    onceki_arama = son_arama          # bu tura GİREN bağlam
                    sonuc = sohbet_turu(ctx, profile.profile_map, baglam, gecmis)
                    gecmis.append({"role": "assistant", "content": sonuc["metin"]})
                    aramalar = [a for a in (sonuc.get("arac_izi") or [])
                                if a.get("arac") == "ihale_ara" and a.get("ok")]
                    if aramalar:
                        son_arama = aramalar[-1].get("param")
                    if len(turlar_metni) > 1:
                        # ⚠️ Her turun ÇAĞRILARINI parametreleriyle bas. Yalnızca son
                        # turu görmek, "filtre düştü" hatasının modelden mi yoksa zaten
                        # hiç kurulmamış bir filtreden mi geldiğini ayırt ettirmiyordu.
                        izler = "; ".join(
                            f"{a['arac']}({', '.join(sorted(a.get('param') or {}))})"
                            for a in (sonuc.get("arac_izi") or [])
                        ) or "araç yok"
                        self.stdout.write(f"   ↳ tur {j + 1}: {metin_turu[:44]:<44} {izler}")
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
            # Takip sorusu ölçütü: son aramada bu parametreler HÂLÂ duruyor olmalı.
            # Metne bakarak ölçmek yetmez — model doğru cümleyi kurup yanlış filtreyi
            # çalıştırabilir; hata tam olarak orada gizlenir.
            son = [a for a in (sonuc.get("arac_izi") or [])
                   if a.get("arac") == "ihale_ara" and a.get("ok")]
            for anahtar in s.get("param_korunmali") or []:
                if not son:
                    sorunlar.append(f"arama yapılmadı ({anahtar!r} korunamazdı)")
                elif anahtar in (son[-1].get("param") or {}):
                    continue
                elif anahtar in (onceki_arama or {}):
                    # Bağlamda VARDI ve son turda DÜŞTÜ → gerçek regresyon.
                    sorunlar.append(f"filtre düştü: {anahtar!r}")
                else:
                    # Bağlamda hiç YOKTU → önceki turun araç seçimi farklı olmuş.
                    # Bu bir bağlam taşıma hatası DEĞİL; ayrı raporlanır, yoksa
                    # ölçüt yanlış yeri suçlar ve gerçek hata gizlenir.
                    sorunlar.append(f"önceki turda hiç kurulmamış: {anahtar!r}")

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
