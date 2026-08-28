"""
Ad kalıbı istatistiği — AI maliyetinin TEK dayanağı.

Keyword boru hattı 1M ihaleyi tek tek AI'ya sormaz: ihale adları yıl/miktar arındırılmış
"kalıba" indirgenir, AI'ya yalnızca **benzersiz kalıplar** gider, sonuç kalıbı paylaşan
tüm ihalelere yayılır. Dolayısıyla maliyeti belirleyen sayı ihale sayısı değil, **tekil
kalıp sayısıdır** — ve o sayı ancak tüm arşiv taranarak bilinir.

⚠️ **Örneklemden dedup oranı ÇIKARILAMAZ** (bir kez yanlış okundu). 1M kayıttan 2000
örneklerken, popülasyonda 300k kalıp olsa bile aynı kalıptan iki tane denk gelme
beklentisi ~%0,3'tür; örneklem her hâlükârda "1,0×" gibi görünür. Doğum günü paradoksu.
Bu yüzden burada **tam tarama** yapılır.

Maliyet: yok (salt okuma). Süre: ~1M satırda 3-8 dk.
⚠️ `.only("pk", "ihale_adi")` → `detail_raw`/`list_raw` TOAST'ına **hiç dokunmaz**, yani
`sync_contractors` süpürmesiyle buffer cache yarışına girmez; gündüz de çalıştırılabilir.

Kullanım:

    docker compose exec web python manage.py keyword_pattern_stats
    docker compose exec web python manage.py keyword_pattern_stats --limit 200000
    docker compose exec web python manage.py keyword_pattern_stats --ornek 40
"""
import time
from collections import Counter

from django.core.management.base import BaseCommand

from ekap import keywords as kw
from ekap.models import Tender

# Örnek metin saklanacak azami farklı kalıp. Tüm kalıp metinlerini tutmak 1M satırda
# ~250 MB'a çıkardı; sık kalıplar taramanın başında zaten görüldüğü için bu tavan
# raporun içeriğini pratikte etkilemez, belleği ise sabitler.
ORNEK_TAVANI = 200_000
ORNEK_KIRPMA = 90

# Haiku 4.5 **batch** (%50 indirimli) birim fiyatları — $/1M token.
FIYAT = {
    "claude-haiku-4-5": (0.50, 2.50),
    "claude-sonnet-5": (1.00, 5.00),
    "claude-opus-5": (2.50, 12.50),
}
# Kalıp başına token — **üretim pilotunda ölçüldü** (2026-08-28, 285 kalıp,
# 25'erli gruplar, Haiku 4.5): 44.615 girdi / 17.857 çıktı token.
# ⚠️ Girdinin büyük kısmı system prompt'un her istekte tekrarı; toplu boru hattında
# prompt cache devreye girince bu rakam düşer, yani tahmin ÜST sınırdır.
VARSAYILAN_TOKEN = (156.5, 62.7)


class Command(BaseCommand):
    help = "İhale adı kalıplarını sayar — AI maliyet tahmininin dayanağı (salt okuma)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                            help="Yalnızca ilk N ihale (0 = tümü)")
        parser.add_argument("--ornek", type=int, default=25,
                            help="Raporda gösterilecek en sık kalıp sayısı")
        parser.add_argument("--in-tok", type=float, default=VARSAYILAN_TOKEN[0],
                            help="Kalıp başına girdi token (pilot çıktısından)")
        parser.add_argument("--out-tok", type=float, default=VARSAYILAN_TOKEN[1],
                            help="Kalıp başına çıktı token (pilot çıktısından)")

    def handle(self, *args, **o):
        yaz = self.stdout.write
        yaz(self.style.MIGRATE_HEADING("\n═══ AD KALIBI İSTATİSTİĞİ ═══"))

        qs = Tender.objects.exclude(ihale_adi="").only("pk", "ihale_adi").order_by("pk")
        if o["limit"]:
            qs = qs[: o["limit"]]

        import hashlib                       # yalnızca burada gerekli

        sayac = Counter()
        ornekler = {}
        toplam = kalipsiz = 0
        basla = time.monotonic()

        for t in qs.iterator(chunk_size=5000):
            toplam += 1
            norm = kw.kalip_norm(t.ihale_adi)
            if len(norm.split()) < 2:
                kalipsiz += 1
                continue
            # sha1'in ilk 16 hex hanesi → 64-bit int. 1M anahtarlık bir Counter'da
            # string yerine int tutmak belleği ~3 kat düşürür; çakışma olasılığı
            # 1M anahtarda ihmal edilebilir (~3e-8).
            anahtar = int(hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16], 16)
            sayac[anahtar] += 1
            if anahtar not in ornekler and len(ornekler) < ORNEK_TAVANI:
                ornekler[anahtar] = norm[:ORNEK_KIRPMA]
            if toplam % 100_000 == 0:
                yaz(f"  …{toplam:,} ihale tarandı, {len(sayac):,} tekil kalıp "
                    f"({time.monotonic() - basla:.0f} sn)")

        gecen = time.monotonic() - basla
        kalipli = toplam - kalipsiz
        tekil = len(sayac)
        if not tekil:
            self.stderr.write(self.style.ERROR("Hiç kalıp üretilemedi."))
            return

        yaz(self.style.MIGRATE_HEADING("\n── Kapsam ──"))
        yaz(f"  taranan ihale        : {toplam:,}  ({gecen:.0f} sn)")
        yaz(f"  ayırt edici olmayan  : {kalipsiz:,} (%{100 * kalipsiz / toplam:.1f}) "
            f"— AI'ya gitmez, keyword de almaz")
        yaz(f"  kalıbı olan ihale    : {kalipli:,}")
        yaz(self.style.SUCCESS(f"  TEKİL KALIP          : {tekil:,}"))
        yaz(self.style.SUCCESS(f"  DEDUP ORANI          : {kalipli / tekil:.2f}×  "
                               f"← AI maliyetini bu böler"))

        # ── tekrar histogramı ──
        yaz(self.style.MIGRATE_HEADING("\n── Kalıp başına ihale sayısı ──"))
        kovalar = [(1, 1), (2, 2), (3, 5), (6, 10), (11, 50), (51, 10**9)]
        etiket = {(1, 1): "1 (benzersiz)", (2, 2): "2", (3, 5): "3-5",
                  (6, 10): "6-10", (11, 50): "11-50", (51, 10**9): "51+"}
        dagilim = Counter()
        for adet in sayac.values():
            for alt, ust in kovalar:
                if alt <= adet <= ust:
                    dagilim[(alt, ust)] += 1
                    break
        for kova in kovalar:
            n = dagilim[kova]
            yaz(f"  {etiket[kova]:>14} : {n:>9,} kalıp  (%{100 * n / tekil:.1f})")

        # ── kümülatif kapsam: AI bütçesinin ilk %X'i değerin %kaçını getirir ──
        yaz(self.style.MIGRATE_HEADING("\n── Kümülatif kapsam (en sık kalıplar önce) ──"))
        sirali = sorted(sayac.values(), reverse=True)
        birikim, esikler, i = 0, [0.01, 0.05, 0.10, 0.20, 0.50], 0
        hedefler = [(int(tekil * e), e) for e in esikler]
        for idx, adet in enumerate(sirali, 1):
            birikim += adet
            while i < len(hedefler) and idx >= hedefler[i][0]:
                n_kalip, oran = hedefler[i]
                yaz(f"  en sık %{oran * 100:>4.0f} kalıp ({n_kalip:>8,}) → "
                    f"ihalelerin %{100 * birikim / kalipli:.1f}'i")
                i += 1
        yaz("  ⚠️ `dispatch` kalıpları `-ihale_sayisi` sırasıyla gönderir → bütçe "
            "yarıda kesilse bile kapsamın büyük kısmı alınmış olur.")

        # ── en sık kalıplar ──
        yaz(self.style.MIGRATE_HEADING(f"\n── En sık {o['ornek']} kalıp ──"))
        for anahtar, adet in sayac.most_common(o["ornek"]):
            yaz(f"  {adet:>6,}×  {ornekler.get(anahtar, '(örnek saklanmadı)')}")

        # ── maliyet ──
        yaz(self.style.MIGRATE_HEADING("\n── AI maliyeti (Batches API, %50 indirimli) ──"))
        yaz(f"  varsayım: kalıp başına {o['in_tok']:.0f} girdi + {o['out_tok']:.0f} "
            f"çıktı token  (pilot çıktısındaki gerçek sayılarla --in-tok/--out-tok)")
        if o["limit"]:
            oran = toplam / max(Tender.objects.count(), 1)
            yaz(self.style.WARNING(
                f"  ⚠ --limit ile taradın (arşivin ~%{100 * oran:.0f}'i); tekil kalıp "
                "sayısı tam taramada ARTAR, aşağıdaki rakam alt sınırdır."))
        for model, (f_in, f_out) in FIYAT.items():
            usd = (o["in_tok"] * tekil * f_in + o["out_tok"] * tekil * f_out) / 1_000_000
            isaret = "  ←" if model == "claude-haiku-4-5" else ""
            yaz(f"  {model:<20} ~${usd:>9,.2f}{isaret}")
        yaz("")
