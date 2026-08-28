"""
Keyword pilotu — AI gerçekten işe yarar keyword üretiyor mu?

Toplu batch'e (ve paraya) girmeden ÖNCE çalıştırılır. Üç soruyu ayrı ayrı yanıtlar:

  A) BİÇİM     — model yasak kuralları (yıl, idare adı, jenerik ek) çiğniyor mu?
  B) PATLAMA   — üretilen tekil keyword sayısı makul mü? (varyant üretiyorsa
                 kanonikleştirici yetersiz demektir → önce o düzeltilir)
  C) AYIRT ETME— **asıl soru**: keyword'ler benzer işleri gerçekten yakınlaştırıyor mu?

⚠️ C bölümünün ground-truth'u BEDAVA: `Tender.seri_anahtar`. Aynı seriye düşen ihaleler
(aynı idarenin yıldan yıla tekrarladığı iş) *tanım gereği* benzerdir. Kardeş çiftlerin
keyword örtüşmesi (Jaccard) ile rastgele çiftlerinki karşılaştırılır. Aradaki fark
kapanıksa keyword katmanı benzerlik taşımıyor demektir — o hâlde toplu batch'e
GİRİLMEZ. Bu, "sonuçlar alakasız çıkıyor" şikâyetinin nicel karşılığıdır.

Kullanım (sunucuda):

    docker compose exec web python manage.py keyword_pilot
    docker compose exec web python manage.py keyword_pilot --n 300 --ciftler 60
    docker compose exec web python manage.py keyword_pilot --model claude-sonnet-5
    docker compose exec web python manage.py keyword_pilot --dry-run     # AI'ya gitmez

Maliyet: varsayılan ayarla (300 ad + 60 çift) birkaç sent. Hiçbir şey YAZMAZ — salt
okuma + ekran/CSV çıktısı.
"""
import csv
import json
import random
import time
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min

from ai.prompts import KEYWORD_BATCH_SYSTEM, keyword_schema
from ai.services.claude import get_api_key
from ekap import keywords as kw
from ekap.constants import SEKTOR_KODLARI, SEKTORLER
from ekap.models import Tender

# İstek başına kalıp. Toplu boru hattıyla aynı tutulmalı — pilot orada olmayan bir
# koşulu ölçerse sonucu taşımaz (25'te disiplinli olan model 50'de bozulabilir).
GRUP = 25
VARSAYILAN_MODEL = "claude-haiku-4-5"


class Command(BaseCommand):
    help = "AI keyword üretimini gerçek ihale adları üzerinde ölçer (hiçbir şey yazmaz)."

    def add_arguments(self, parser):
        parser.add_argument("--n", type=int, default=300,
                            help="Biçim/patlama ölçümü için rastgele ihale adı sayısı")
        parser.add_argument("--ciftler", type=int, default=60,
                            help="Ayırt etme ölçümü için seri kardeşi çift sayısı (0=atla)")
        parser.add_argument("--model", default=VARSAYILAN_MODEL)
        parser.add_argument("--csv", default="/tmp/keyword_pilot.csv",
                            help="Çıktının yazılacağı CSV (elle inceleme için)")
        parser.add_argument("--dry-run", action="store_true",
                            help="AI'ya gitme; yalnızca kalıp/dedup istatistiği bas")
        parser.add_argument("--seed", type=int, default=20260828,
                            help="Örneklem tekrar üretilebilir olsun diye")

    # ── örnekleme ───────────────────────────────────────
    def _rastgele_ihaleler(self, n, rastgele):
        """
        Rastgele n ihale — `order_by("?")` KULLANILMAZ.

        ⚠️ `ORDER BY random()` 1M satırda tablonun tamamını sıralar (dakikalar).
        Bunun yerine PK aralığından rastgele değerler üretilip `pk__in` ile çekilir:
        indeksli, sabit maliyetli. PK boşlukları için 4 kat fazla aday üretilir.
        """
        sinir = Tender.objects.aggregate(a=Min("pk"), b=Max("pk"))
        if not sinir["a"]:
            return []
        adaylar = [rastgele.randint(sinir["a"], sinir["b"]) for _ in range(n * 4)]
        return list(
            Tender.objects.filter(pk__in=adaylar)
            .exclude(ihale_adi="")
            .only("pk", "ihale_adi", "okas_ana_kod")[:n]
        )

    def _kardes_ciftleri(self, adet, rastgele):
        """
        Aynı `seri_anahtar`'a sahip ihale çiftleri — ground-truth "benzer iş" örnekleri.

        Önce hazır `RecurringTenderSeries` tablosu denenir (haftalık görev doldurur,
        bedava); boşsa `GROUP BY seri_anahtar`'a düşülür (indeksli ama daha pahalı).
        """
        anahtarlar = []
        try:
            from ekap.models import RecurringTenderSeries
            anahtarlar = list(
                RecurringTenderSeries.objects.exclude(seri_anahtar="")
                .order_by()
                .values_list("seri_anahtar", flat=True)[: adet * 3]
            )
        except Exception:                                     # model yok / tablo boş
            anahtarlar = []
        if not anahtarlar:
            self.stdout.write("  (RecurringTenderSeries boş — GROUP BY'a düşülüyor)")
            anahtarlar = list(
                Tender.objects.exclude(seri_anahtar="")
                .values("seri_anahtar")
                .annotate(n=Count("pk"))
                .filter(n__gte=2)
                .order_by()
                .values_list("seri_anahtar", flat=True)[: adet * 3]
            )
        rastgele.shuffle(anahtarlar)

        ciftler = []
        for anahtar in anahtarlar:
            uyeler = list(
                Tender.objects.filter(seri_anahtar=anahtar)
                .exclude(ihale_adi="")
                .only("pk", "ihale_adi", "okas_ana_kod")
                .order_by("pk")[:2]
            )
            if len(uyeler) == 2 and uyeler[0].ihale_adi != uyeler[1].ihale_adi:
                ciftler.append(tuple(uyeler))
            if len(ciftler) >= adet:
                break
        return ciftler

    # ── AI çağrısı ──────────────────────────────────────
    def _sor(self, client, model, kalip_listesi):
        """
        Bir grup kalıbı modele sorar → `{id: {"keywords": [...], "sektor": ..., ...}}`.

        `kalip_listesi`: [(id, kalip_norm), ...]
        """
        satirlar = "\n".join(f"id={i} | {k}" for i, k in kalip_listesi)
        istek = (
            "Aşağıdaki ihale adı kalıplarının her biri için keyword ve sektör üret.\n"
            'Her sonucun "id" alanı, verilen id ile birebir aynı olmalı.\n\n'
            f"{satirlar}"
        )
        yanit = client.messages.create(
            model=model,
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": KEYWORD_BATCH_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": istek}],
            output_config={
                "format": {"type": "json_schema", "schema": keyword_schema(SEKTOR_KODLARI)}
            },
        )
        ham = "".join(b.text for b in yanit.content if b.type == "text")
        try:
            veri = json.loads(ham)
        except json.JSONDecodeError:
            self.stderr.write(self.style.ERROR(f"JSON parse edilemedi:\n{ham[:600]}"))
            return {}, yanit.usage
        return {s["id"]: s for s in veri.get("sonuclar", [])}, yanit.usage

    # ── ana akış ────────────────────────────────────────
    def handle(self, *args, **o):
        rastgele = random.Random(o["seed"])
        model = o["model"]

        self.stdout.write(self.style.MIGRATE_HEADING("\n═══ KEYWORD PİLOTU ═══"))
        self.stdout.write(f"model={model}  n={o['n']}  ciftler={o['ciftler']}\n")

        # ---- örneklem + kalıp ----
        ihaleler = self._rastgele_ihaleler(o["n"], rastgele)
        if not ihaleler:
            self.stderr.write(self.style.ERROR("Hiç ihale bulunamadı."))
            return

        kaliplar = {}                       # kalip_norm → temsilci ihale
        kalipsiz = 0
        for t in ihaleler:
            h = kw.kalip_hash(t.ihale_adi)
            if not h:
                kalipsiz += 1
                continue
            kaliplar.setdefault(kw.kalip_norm(t.ihale_adi), t)

        self.stdout.write(self.style.MIGRATE_HEADING("── Kalıp / dedup ──"))
        self.stdout.write(f"  çekilen ihale        : {len(ihaleler)}")
        self.stdout.write(f"  ayırt edici olmayan  : {kalipsiz} "
                          f"(%{100 * kalipsiz / len(ihaleler):.1f}) — AI'ya gitmez")
        self.stdout.write(f"  tekil kalıp          : {len(kaliplar)}")
        if kaliplar:
            self.stdout.write(f"  dedup oranı          : "
                              f"{len(ihaleler) / len(kaliplar):.2f}× "
                              f"(bu örneklemde; gerçek oran tüm arşivde ölçülmeli)")

        if o["dry_run"]:
            self.stdout.write("\n--dry-run: AI'ya gidilmedi.\n")
            for i, k in enumerate(list(kaliplar)[:25], 1):
                self.stdout.write(f"  {i:>3}. {k}")
            return

        client = self._client()
        toplam_in = toplam_out = 0
        basla = time.monotonic()

        # ---- BÖLÜM A/B: biçim + patlama ----
        liste = [(i, k) for i, k in enumerate(kaliplar, 1)]
        sonuclar = {}
        for i in range(0, len(liste), GRUP):
            grup = liste[i:i + GRUP]
            veri, usage = self._sor(client, model, grup)
            sonuclar.update(veri)
            toplam_in += usage.input_tokens
            toplam_out += usage.output_tokens
            self.stdout.write(f"  …{len(sonuclar)}/{len(liste)} kalıp işlendi")

        eksik = [i for i, _ in liste if i not in sonuclar]
        satirlar, ham_ihlal, kanonik_ihlal = [], 0, 0
        ham_toplam = 0
        tekil_kanonik = set()
        sektor_dagilim = defaultdict(int)
        bos_sonuc = 0

        for i, kalip in liste:
            s = sonuclar.get(i)
            if not s:
                continue
            hams = s.get("keywords") or []
            if not hams:
                bos_sonuc += 1
            kanonikler = []
            for h in hams:
                ham_toplam += 1
                if kw.yasak_ihlali(h):
                    ham_ihlal += 1
                k = kw.kanonik_keyword(h)
                if k:
                    kanonikler.append(k)
                    tekil_kanonik.add(k)
                    if kw.yasak_ihlali(k):
                        kanonik_ihlal += 1
            sektor_dagilim[s.get("sektor", "?")] += 1
            satirlar.append({
                "kalip": kalip,
                "ornek_ad": kaliplar[kalip].ihale_adi,
                "ham_keywords": " | ".join(hams),
                "kanonik_keywords": " | ".join(kanonikler),
                "sektor": s.get("sektor", ""),
                "guven": s.get("guven", ""),
            })

        # ---- BÖLÜM C: ayırt etme ----
        ayirt = None
        if o["ciftler"] > 0:
            ayirt, ek_in, ek_out = self._ayirt_etme_olc(
                client, model, o["ciftler"], rastgele)
            toplam_in += ek_in
            toplam_out += ek_out

        gecen = time.monotonic() - basla

        # ---- rapor ----
        self._rapor(satirlar, liste, eksik, ham_toplam, ham_ihlal, kanonik_ihlal,
                    tekil_kanonik, sektor_dagilim, bos_sonuc, ayirt,
                    toplam_in, toplam_out, gecen, model, o["csv"])

    def _client(self):
        import anthropic                                    # lazy — proje kuralı
        return anthropic.Anthropic(api_key=get_api_key())

    # ── C bölümü: kardeş çiftler vs rastgele çiftler ────
    def _ayirt_etme_olc(self, client, model, adet, rastgele):
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n── Ayırt etme ölçümü (seri kardeşleri) ──"))
        ciftler = self._kardes_ciftleri(adet, rastgele)
        if len(ciftler) < 10:
            self.stdout.write(self.style.WARNING(
                f"  Yalnızca {len(ciftler)} kardeş çift bulundu — ölçüm atlanıyor. "
                "(`detect_recurring_series` hiç koşmamış olabilir.)"))
            return None, 0, 0

        # Her ihale için ayrı kalıp; hepsini tek listede sor.
        birimler, i = [], 1
        cift_idler = []
        for a, b in ciftler:
            ka, kb = kw.kalip_norm(a.ihale_adi), kw.kalip_norm(b.ihale_adi)
            if len(ka.split()) < 2 or len(kb.split()) < 2:
                continue
            birimler.append((i, ka))
            birimler.append((i + 1, kb))
            cift_idler.append((i, i + 1))
            i += 2

        sonuc, t_in, t_out = {}, 0, 0
        for j in range(0, len(birimler), GRUP):
            veri, usage = self._sor(client, model, birimler[j:j + GRUP])
            sonuc.update(veri)
            t_in += usage.input_tokens
            t_out += usage.output_tokens

        def kume(i):
            s = sonuc.get(i)
            if not s:
                return set()
            return {k for k in (kw.kanonik_keyword(x) for x in s.get("keywords") or []) if k}

        def jaccard(x, y):
            if not x or not y:
                return 0.0
            return len(x & y) / len(x | y)

        kumeler = {i: kume(i) for i, _ in birimler}
        kardes = [jaccard(kumeler[a], kumeler[b]) for a, b in cift_idler
                  if kumeler[a] and kumeler[b]]

        # Rastgele çiftler — aynı keyword havuzundan, kardeş OLMAYAN eşleşmeler.
        tum = [i for i, _ in birimler if kumeler[i]]
        kardes_set = {frozenset(c) for c in cift_idler}
        rast = []
        for _ in range(len(kardes) * 5):
            a, b = rastgele.sample(tum, 2) if len(tum) >= 2 else (None, None)
            if a is None or frozenset((a, b)) in kardes_set:
                continue
            rast.append(jaccard(kumeler[a], kumeler[b]))

        if not kardes:
            return None, t_in, t_out
        return {
            "cift_sayisi": len(kardes),
            "kardes_ort": sum(kardes) / len(kardes),
            "kardes_kesisim_var": sum(1 for x in kardes if x > 0) / len(kardes),
            "rastgele_ort": (sum(rast) / len(rast)) if rast else 0.0,
            "rastgele_kesisim_var": (sum(1 for x in rast if x > 0) / len(rast)) if rast else 0.0,
        }, t_in, t_out

    # ── rapor ───────────────────────────────────────────
    def _rapor(self, satirlar, liste, eksik, ham_toplam, ham_ihlal, kanonik_ihlal,
               tekil_kanonik, sektor_dagilim, bos_sonuc, ayirt,
               t_in, t_out, gecen, model, csv_yolu):
        yaz = self.stdout.write
        n_kalip = len(liste)

        yaz(self.style.MIGRATE_HEADING("\n── Örnek çıktı (ilk 20) ──"))
        for s in satirlar[:20]:
            yaz(f"\n  {s['ornek_ad'][:90]}")
            yaz(f"    → {s['kanonik_keywords']}")
            yaz(f"    → sektör: {SEKTORLER.get(s['sektor'], s['sektor'])} "
                f"(güven {s['guven']})")

        yaz(self.style.MIGRATE_HEADING("\n\n── Kalite kapıları ──"))

        if eksik:
            yaz(self.style.WARNING(
                f"  ⚠ {len(eksik)} kalıp için sonuç dönmedi (id eşleşmedi/atlandı)"))

        # A) biçim
        ham_oran = 100 * ham_ihlal / ham_toplam if ham_toplam else 0
        kan_oran = 100 * kanonik_ihlal / max(len(tekil_kanonik), 1)
        durum = self.style.SUCCESS("GEÇTİ") if kan_oran < 1 else self.style.ERROR("KALDI")
        yaz(f"  A) Yasak token ihlali")
        yaz(f"       ham çıktıda      : %{ham_oran:.1f}  ({ham_ihlal}/{ham_toplam}) "
            f"— prompt kalitesi")
        yaz(f"       kanonik sonrası  : %{kan_oran:.1f}   — kapı: <%1   [{durum}]")

        # B) patlama
        patlama = len(tekil_kanonik) / max(len(satirlar), 1)
        durum = self.style.SUCCESS("GEÇTİ") if patlama < 1.5 else self.style.ERROR("KALDI")
        yaz(f"  B) Keyword patlaması : {patlama:.2f} tekil/kalıp  "
            f"— kapı: <1.5   [{durum}]")
        yaz(f"       tekil kanonik keyword: {len(tekil_kanonik)}")
        yaz(f"       boş sonuç (dürüst red): {bos_sonuc}/{len(satirlar)}")

        # C) ayırt etme
        yaz(f"  C) Ayırt etme (seri kardeşleri vs rastgele)")
        if not ayirt:
            yaz(self.style.WARNING("       ölçülemedi (yeterli kardeş çift yok)"))
        else:
            k, r = ayirt["kardes_ort"], ayirt["rastgele_ort"]
            oran = (k / r) if r > 0.001 else float("inf")
            gecti = k >= 0.30 and (r < 0.001 or oran >= 3)
            durum = self.style.SUCCESS("GEÇTİ") if gecti else self.style.ERROR("KALDI")
            yaz(f"       kardeş çift ort. Jaccard   : {k:.3f} "
                f"(kesişim var: %{100 * ayirt['kardes_kesisim_var']:.0f})")
            yaz(f"       rastgele çift ort. Jaccard : {r:.3f} "
                f"(kesişim var: %{100 * ayirt['rastgele_kesisim_var']:.0f})")
            yaz(f"       ayrışma oranı              : {oran:.1f}×  "
                f"— kapı: kardeş≥0.30 ve ≥3×   [{durum}]")
            yaz(f"       ({ayirt['cift_sayisi']} çift üzerinden)")

        yaz(self.style.MIGRATE_HEADING("\n── Sektör dağılımı (ilk 12) ──"))
        for kod, adet in sorted(sektor_dagilim.items(), key=lambda x: -x[1])[:12]:
            yaz(f"  {adet:>4}  {SEKTORLER.get(kod, kod)}")

        # maliyet — Haiku 4.5 batch fiyatı üzerinden 1M ihale projeksiyonu
        yaz(self.style.MIGRATE_HEADING("\n── Maliyet ──"))
        yaz(f"  bu koşu: {t_in:,} girdi + {t_out:,} çıktı token, {gecen:.0f} sn")
        if n_kalip:
            in_kalip, out_kalip = t_in / n_kalip, t_out / n_kalip
            for hedef in (250_000, 300_000, 400_000):
                # batch %50 indirim + Haiku 4.5 ($1/$5 per 1M)
                usd = (in_kalip * hedef * 0.5 + out_kalip * hedef * 2.5) / 1_000_000
                yaz(f"  {hedef:>7,} kalıp → ~${usd:,.0f}  (batch %50 indirimli, {model})")
            yaz("  ⚠ Gerçek kalıp sayısı `keyword_pattern_stats` ile ölçülmeli.")

        if not satirlar:
            yaz(self.style.ERROR("\nHiç sonuç üretilemedi — CSV yazılmadı.\n"))
            return
        with open(csv_yolu, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
            w.writeheader()
            w.writerows(satirlar)
        yaz(self.style.SUCCESS(f"\nTam çıktı: {csv_yolu}  ({len(satirlar)} satır)\n"))
