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

# `ihale_ara`da KONUYU ifade eden alanlar. Bir daraltma turunda bunlardan en az biri
# hayatta kalmalı; hangisi olduğu önemli değil.
_KONU_ALANLARI = {"okas_kod", "okas_adi", "okas_ana_kod", "ihale_adi", "q"}

# (giriş, çıkış, önbellek okuma oranı) — $/milyon token; okuma oranı girişin katı.
# Önbellek yazma 5 dk TTL'de girişin 1,25 katı.
# ⚠️ Modele göre seçilir: tek bir fiyat listesine sabitlemek, kademe karşılaştırmasında
# ucuz modelin maliyetini pahalı modelin fiyatıyla hesaplayıp kararı bozar.
# ⚠️ SIRA ÖNEMLİ — alt dize eşleşmesi: "opus-5" "claude-opus-5-5"'i de yakalardı, bu
# yüzden daha özel adlar ÖNCE gelir. (Opus 5.5'te okuma $0,20 = girişin 0,05'i.)
_FIYAT = {
    "opus-5-5": (4.0, 20.0, 0.05),
    "opus-5": (5.0, 25.0, 0.1),
    "opus-4-8": (5.0, 25.0, 0.1),
    "opus-4-7": (5.0, 25.0, 0.1),
    "sonnet-5-5": (2.0, 10.0, 0.05),
    "sonnet-5": (2.0, 10.0, 0.1),
    "sonnet-4-6": (3.0, 15.0, 0.1),
    "haiku-4-5": (1.0, 5.0, 0.1),
    "fable-5-2": (8.0, 40.0, 0.025),
    "fable-5": (10.0, 50.0, 0.1),
}

# Ek senaryoları için örnek dosya içerikleri (repo'ya ikili dosya konmaz; çalışmada
# üretilir). `ekler: [{tur, ad, metin}]` — dokümanda metin .txt olur, fotoğrafta resme yazılır.


def _fiyat(model: str):
    for ad, ucret in _FIYAT.items():
        if ad in model:
            return ucret
    return None


def _ek_hazirla(user, spec, dizin):
    """Senaryodaki eki GERÇEK hazırlama yolundan geçirir (fotoğraf Files API'ye gider)."""
    import os
    from datetime import timedelta

    from django.utils import timezone

    from assistant.models import AssistantAttachment
    from assistant.services import attachments as ek_servis

    tur = spec.get("tur", "dokuman")
    yol = os.path.join(dizin, spec.get("ad", "ek.txt"))
    if tur == "fotograf":
        from PIL import Image, ImageDraw, ImageFont

        im = Image.new("RGB", (900, 300), "white")
        ImageDraw.Draw(im).multiline_text((30, 40), spec["metin"], fill="black",
                                          font=ImageFont.load_default(size=40))
        im.save(yol, "PNG")
    else:
        with open(yol, "w", encoding="utf-8") as f:
            f.write(spec["metin"])
    ek = AssistantAttachment(user=user, tur=tur, kaynak="cihaz", ad=spec.get("ad", "ek.txt"),
                             uzanti=os.path.splitext(yol)[1].lstrip("."), depo_yolu=yol,
                             sira=1, created_at=timezone.now())
    for alan, deger in ek_servis.hazirla(ek).items():
        setattr(ek, alan, deger)
    ek.durum = "hazir"
    ek.expires_at = ek.expires_at or timezone.now() + timedelta(days=1)
    return ek


def _senaryo_ihalesi(deger):
    """`ihale: <İKN>` ya da `ihale: otomatik` (teklifi açık, OKAS'lı ilk ihale)."""
    from django.utils import timezone

    from ekap.models import Tender

    if deger != "otomatik":
        return Tender.objects.filter(ikn=deger).first()
    return (Tender.objects.filter(ihale_tarihi__gte=timezone.now())
            .exclude(okas_ana_kod="").order_by("ihale_tarihi").first())


class Command(BaseCommand):
    help = "İhale Asistanı altın soru setini çalıştırır (gerçek LLM çağrısı yapar)."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="Profili kullanılacak kullanıcı e-postası")
        parser.add_argument("--sadece", type=int, help="Yalnızca N. soruyu çalıştır (1'den başlar)")

    def handle(self, *args, **opts):
        from django.contrib.auth import get_user_model

        import shutil
        import tempfile

        from django.utils import timezone

        from assistant.models import CompanyProfile
        from assistant.services import attachments as ek_servis
        from assistant.services.agent import sohbet_turu
        from assistant.tasks import _ekli_istek, _selected_tender_context
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
            ctx = ToolContext(user=user, premium=bool(getattr(user, "is_premium", False)),
                              ek_acik=bool(s.get("ekler") or s.get("ihale")))
            gecmis, son_arama, onceki_arama = [], None, None
            ekler, ek_dizini, ihale_baglami = [], None, ""
            t0 = time.monotonic()
            try:
                if s.get("ekler"):
                    # ⚠️ Gerçek hazırlama yolu: count_tokens + (fotoğrafta) Files API.
                    ek_dizini = tempfile.mkdtemp()
                    ekler = [_ek_hazirla(user, e, ek_dizini) for e in s["ekler"]]
                    for n, ek in enumerate(ekler, 1):
                        ek.sira = n
                    ctx.ekler = [{"sira": e.sira, "ad": e.ad, "tur": e.tur, "parca": e.parca}
                                 for e in ekler]
                if s.get("ihale"):
                    # Mobilin ihale sohbeti: seçili ihalenin bağlamı (maliyet analizi senaryosu).
                    t = _senaryo_ihalesi(s["ihale"])
                    if t is None:
                        raise CommandError("senaryo ihalesi bulunamadı")
                    ihale_baglami = _selected_tender_context(t, timezone.localdate(), ek_acik=True)
                    ctx.kart_ekle(t)
                for j, metin_turu in enumerate(turlar_metni):
                    gecmis.append({"role": "user", "content": metin_turu})
                    baglam = "Bugünün tarihi: bugün"
                    if ctx.ek_acik:
                        baglam += "\nEK GÖNDERME: AÇIK"
                    if ihale_baglami:
                        baglam += "\n\n" + ihale_baglami
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
                    # Ekli senaryoda üretimdeki bağlam yerleşimi (`assistant/tasks.py`).
                    baglam_metni, istek = _ekli_istek(gecmis, ekler, [baglam])
                    sonuc = sohbet_turu(ctx, profile.profile_map, baglam_metni, istek)
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
            finally:
                if ek_dizini:
                    shutil.rmtree(ek_dizini, ignore_errors=True)
                for ek in ekler:
                    if ek.anthropic_file_id:
                        try:
                            ek_servis.dosya_sil(ek_servis._istemci(), ek.anthropic_file_id)
                        except Exception:
                            pass
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
            biri = s.get("icermeli_biri") or []
            if biri and not any(k.lower() in metin for k in biri):
                sorunlar.append(f"hiçbiri yok: {biri!r}")
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

            # Konu filtresi korunmuş mu? ⚠️ Belirli bir ANAHTARI şart koşmak yanlış:
            # model konuyu meşru biçimde `okas_kod`dan `ihale_adi`na çevirebilir
            # (OKAS araması sonuç vermeyince ad aramasına düşmek doğru davranıştır).
            # Korunması gereken şey konunun KENDİSİ, hangi alanla ifade edildiği değil.
            if s.get("konu_korunmali"):
                onceki_konu = _KONU_ALANLARI & set(onceki_arama or {})
                if not onceki_konu:
                    sorunlar.append("önceki turda konu filtresi hiç yoktu")
                elif not son:
                    sorunlar.append("arama yapılmadı (konu korunamazdı)")
                elif not (_KONU_ALANLARI & set(son[-1].get("param") or {})):
                    sorunlar.append(
                        f"KONU FİLTRESİ DÜŞTÜ (önceki: {', '.join(sorted(onceki_konu))})")
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
                g, c, okuma = fiyat
                usd = (
                    u.get("input_tokens", 0) * g
                    + u.get("output_tokens", 0) * c
                    + u.get("cache_read_input_tokens", 0) * g * okuma
                    + u.get("cache_creation_input_tokens", 0) * g * 1.25
                ) / 1e6
                toplam_usd += usd

            if sorunlar:
                self.stdout.write(self.style.ERROR(
                    f"{i}. ✗ {s['soru'][:42]:<42} {sure:>5.0f}sn tur={sonuc['turlar']} "
                    f"| {'; '.join(sorunlar)}"))
                # Cevabın kendisi: ölçüt mü katı, model mi yanlış — ayırt etmenin tek yolu.
                self.stdout.write(f"   cevap: {(sonuc['metin'] or '')[:400]!r}")
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
