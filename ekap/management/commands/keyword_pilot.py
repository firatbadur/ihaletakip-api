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
VARSAYILAN_GRUP = 25
# Kalıp başına azami keyword. ⚠️ JSON şemasında `maxItems` ile zorlanamıyor (API 400
# veriyor), o yüzden prompt söyler + burada kırpılır.
AZAMI_KEYWORD = 8
VARSAYILAN_MODEL = "claude-haiku-4-5"


class Command(BaseCommand):
    help = "AI keyword üretimini gerçek ihale adları üzerinde ölçer (hiçbir şey yazmaz)."

    def add_arguments(self, parser):
        parser.add_argument("--n", type=int, default=300,
                            help="Biçim/patlama ölçümü için rastgele ihale adı sayısı")
        parser.add_argument("--ciftler", type=int, default=60,
                            help="Ayırt etme ölçümü için seri kardeşi çift sayısı (0=atla)")
        parser.add_argument("--model", default=VARSAYILAN_MODEL)
        parser.add_argument("--grup", type=int, default=VARSAYILAN_GRUP,
                            help="İstek başına kalıp. ⚠️ Girdi token'ının ~%75'i "
                                 "system prompt + şemanın her istekte tekrarı; grubu "
                                 "büyütmek girdiyi düşürür ama modelin geç kalıplarda "
                                 "kural disiplinini kaybetme riskini artırır — kapılara "
                                 "bakarak karar verin")
        parser.add_argument("--csv", default="/tmp/keyword_pilot.csv",
                            help="Çıktının yazılacağı CSV (elle inceleme için)")
        parser.add_argument("--dry-run", action="store_true",
                            help="AI'ya gitme; yalnızca kalıp/dedup istatistiği bas")
        parser.add_argument("--yontem", choices=["ai", "det", "ikisi"], default="ai",
                            help="ai = model · det = AI'siz taban çizgisi · "
                                 "ikisi = ikisini yan yana ölç (AI'nın parasını hak "
                                 "edip etmediğini gösteren karşılaştırma)")
        parser.add_argument("--df-tara", type=int, default=0,
                            help="Deterministik IDF/PMI için taranacak ihale sayısı "
                                 "(0 = tüm arşiv). ⚠️ Az taramak taban çizgisini haksız "
                                 "yere zayıflatır ve AI'yı olduğundan iyi gösterir")
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
        self.grup = max(1, o["grup"])

        self.stdout.write(self.style.MIGRATE_HEADING("\n═══ KEYWORD PİLOTU ═══"))
        self.stdout.write(f"model={model}  n={o['n']}  ciftler={o['ciftler']}  grup={self.grup}\n")

        if o["yontem"] == "ikisi":
            return self._kiyasla(o, rastgele, model)

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
        # ⚠️ Buradan dedup ORANI ÇIKARILAMAZ — doğum günü paradoksu. 1M kayıttan
        # 2000 örneklerken, popülasyonda 300k kalıp olsa bile aynı kalıptan iki
        # tane denk gelme beklentisi düşüktür; örneklem tekil sayısı daima ~1'e
        # yakın bir "oran" gösterir ve bu YANLIŞ okunur (bir kez okundu).
        # Gerçek dedup yalnızca tüm arşiv taranarak ölçülür:
        #     manage.py keyword_pattern_stats
        cakisma = len(ihaleler) - kalipsiz - len(kaliplar)
        self.stdout.write(f"  örneklem içi çakışma : {cakisma}")
        self.stdout.write(self.style.WARNING(
            "  ⚠ Bu örneklemden dedup ORANI çıkarılamaz (doğum günü paradoksu) — "
            "gerçek oran için: manage.py keyword_pattern_stats"))

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
        for i in range(0, len(liste), self.grup):
            grup = liste[i:i + self.grup]
            veri, usage = self._sor(client, model, grup)
            sonuclar.update(veri)
            toplam_in += usage.input_tokens
            toplam_out += usage.output_tokens
            self.stdout.write(f"  …{len(sonuclar)}/{len(liste)} kalıp işlendi")

        eksik = [i for i, _ in liste if i not in sonuclar]
        satirlar, ham_ihlal, kanonik_ihlal = [], 0, 0
        kume_map = {}                      # kalip → kanonik keyword kümesi
        ham_ornek, kanonik_ornek = [], set()
        ham_toplam = 0
        tekil_kanonik = set()
        sektor_dagilim = defaultdict(int)
        bos_sonuc = 0

        for i, kalip in liste:
            s = sonuclar.get(i)
            if not s:
                continue
            hams = (s.get("keywords") or [])[:AZAMI_KEYWORD]
            if not hams:
                bos_sonuc += 1
            for h in hams:
                ham_toplam += 1
                if kw.yasak_ihlali(h):
                    ham_ihlal += 1
                    ham_ornek.append(h)
            kanonikler = kw.kanonik_liste(hams, azami=AZAMI_KEYWORD)
            for k in kanonikler:
                tekil_kanonik.add(k)
                if kw.yasak_ihlali(k):
                    kanonik_ihlal += 1
                    kanonik_ornek.add(k)
            kume_map[kalip] = set(kanonikler)
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

        # ⚠️ En zorlu kontrol, insan gözü için: keyword örtüşmesi en yüksek çıkan
        # FARKLI kalıp çiftleri. Seri kardeşi testi (C) kolaydır — kardeşlerin
        # metinleri zaten neredeyse aynıdır. Asıl risk, birbirine benzemeyen işlerin
        # aynı keyword kümesine düşmesi (yanlış pozitif); benchmark'a o çiftler
        # sızarsa kullanıcı yine alakasız fiyat görür. Ek AI çağrısı gerektirmez.
        eslesmeler = self._yakin_ciftler(kume_map)

        # ---- rapor ----
        self._rapor(satirlar, liste, eksik, ham_toplam, ham_ihlal, kanonik_ihlal,
                    tekil_kanonik, sektor_dagilim, bos_sonuc, ayirt, eslesmeler,
                    ham_ornek, kanonik_ornek,
                    toplam_in, toplam_out, gecen, model, o["csv"])

    def _varyant_ciftleri(self, kanonikler):
        """
        Birbirinden yalnızca son ekle ayrılan kanonik keyword çiftleri.

        Kanonikleştiricinin KAÇIRDIKLARINI gösterir ("dogalgaz donusum" ~
        "dogalgaz donusumu" gibi). ⚠️ Alt küme ilişkisi ("elbise" ⊂ "kislik
        elbise") varyant DEĞİLDİR — 1-gram ile 2-gram bilinçli olarak birlikte
        üretiliyor; yalnızca aynı token sayısında ve son token'ı ek farkıyla
        ayrışan çiftler işaretlenir.
        """
        by_len = defaultdict(list)
        for k in kanonikler:
            by_len[len(k.split())].append(k)
        cift = []
        for _, grup in by_len.items():
            grup.sort()
            for i in range(len(grup)):
                for j in range(i + 1, len(grup)):
                    a, b = grup[i], grup[j]
                    if len(b) - len(a) > 3 or not b.startswith(a):
                        continue
                    if a.split()[:-1] == b.split()[:-1]:      # yalnızca son token farklı
                        cift.append((a, b))
        return cift

    def _yakin_ciftler(self, kume_map, kac=12):
        """Keyword örtüşmesi en yüksek kalıp çiftleri (yanlış pozitif denetimi)."""
        ogeler = [(k, v) for k, v in kume_map.items() if v]
        # Ters indeks: yalnızca ortak keyword'ü olan çiftler karşılaştırılır
        # (285 kalıpta kaba kuvvet de olurdu ama --n büyütülünce O(n²) patlardı).
        ters = defaultdict(list)
        for i, (_, kume) in enumerate(ogeler):
            for k in kume:
                ters[k].append(i)
        aday = set()
        for idler in ters.values():
            if len(idler) > 30:            # çok yaygın keyword — gürültü
                continue
            for a in range(len(idler)):
                for b in range(a + 1, len(idler)):
                    aday.add((idler[a], idler[b]))
        skorlu = []
        for a, b in aday:
            ka, kb = ogeler[a][1], ogeler[b][1]
            j = len(ka & kb) / len(ka | kb)
            skorlu.append((j, ogeler[a][0], ogeler[b][0], sorted(ka & kb)))
        skorlu.sort(reverse=True)
        return skorlu[:kac]

    # ── AI vs deterministik karşılaştırması ─────────────
    def _kume_metrikleri(self, kume_map):
        """Bir keyword kümesi haritasının kalite göstergeleri (yöntemden bağımsız)."""
        tekil = set()
        ihlal = 0
        for kume in kume_map.values():
            for k in kume:
                tekil.add(k)
                if kw.yasak_ihlali(k):
                    ihlal += 1
        dolu = [k for k, v in kume_map.items() if v]
        return {
            "tekil": len(tekil),
            "ihlal_oran": (100 * ihlal / max(len(tekil), 1)),
            "kapsam": len(dolu),
            "ort_keyword": sum(len(v) for v in kume_map.values()) / max(len(kume_map), 1),
            "varyant": len(self._varyant_ciftleri(tekil)),
            "yakin": self._yakin_ciftler(kume_map),
        }

    def _jaccard(self, a, b):
        return len(a & b) / len(a | b) if (a and b) else 0.0

    def _ayirt_skoru(self, kume_a, kume_b, rastgele, tur=400):
        """(kardeş ort. Jaccard, rastgele ort. Jaccard) — çift listesi dışarıdan gelir."""
        kardes = [self._jaccard(a, b) for a, b in zip(kume_a, kume_b) if a and b]
        havuz = [k for k in list(kume_a) + list(kume_b) if k]
        rast = []
        for _ in range(tur):
            if len(havuz) < 2:
                break
            x, y = rastgele.sample(havuz, 2)
            rast.append(self._jaccard(x, y))
        return (sum(kardes) / len(kardes) if kardes else 0.0,
                sum(rast) / len(rast) if rast else 0.0,
                len(kardes))

    def _kiyasla(self, o, rastgele, model):
        """
        Aynı kalıplar için AI ve deterministik keyword'leri yan yana ölçer.

        ⚠️ Asıl soru "hangisi daha çok keyword üretiyor" DEĞİL. Pilotta gözlendi ki
        eşleşmelerin çoğunu ihale adında zaten geçen kelimeler kuruyor — onlar için
        modele gerek yok. AI'nın ölçülmesi gereken katkısı, adda GEÇMEYEN üst kavramı
        ekleyip farklı kelimelerle yazılmış aynı işleri köprülemesi. Bu yüzden rapor
        "AI'nın kurduğu ama deterministiğin kuramadığı eşleşmeler" ile bitiyor —
        parayı hak eden şey varsa orada görünür.
        """
        yaz = self.stdout.write
        yaz(self.style.MIGRATE_HEADING("\n═══ KARŞILAŞTIRMA: AI vs DETERMİNİSTİK ═══\n"))

        token_df, bigram_df, n_kalip = self._istatistik_topla(o["df_tara"])

        ihaleler = self._rastgele_ihaleler(o["n"], rastgele)
        kaliplar = {}
        for t in ihaleler:
            if kw.kalip_hash(t.ihale_adi):
                kaliplar.setdefault(kw.kalip_norm(t.ihale_adi), t)
        liste = [(i, k) for i, k in enumerate(kaliplar, 1)]
        yaz(f"\n  {len(liste)} tekil kalıp ölçülüyor…\n")

        # --- deterministik (bedava, anında) ---
        det_map = {
            k: set(kw.deterministik_keywords(k, token_df, bigram_df, n_kalip,
                                             azami=AZAMI_KEYWORD))
            for _, k in liste
        }
        # --- AI ---
        client = self._client()
        sonuclar, t_in, t_out = {}, 0, 0
        for i in range(0, len(liste), self.grup):
            veri, usage = self._sor(client, model, liste[i:i + self.grup])
            sonuclar.update(veri)
            t_in += usage.input_tokens
            t_out += usage.output_tokens
            yaz(f"  …AI {len(sonuclar)}/{len(liste)}")
        ai_map, ai_sektor = {}, {}
        for i, kalip in liste:
            sn = sonuclar.get(i) or {}
            ai_map[kalip] = set(kw.kanonik_liste(sn.get("keywords"), azami=AZAMI_KEYWORD))
            ai_sektor[kalip] = sn.get("sektor", "")

        m_ai, m_det = self._kume_metrikleri(ai_map), self._kume_metrikleri(det_map)

        # --- yan yana tablo ---
        yaz(self.style.MIGRATE_HEADING("\n── Yan yana ──"))
        yaz(f"  {'ölçüt':<34}{'AI':>12}{'deterministik':>16}")
        satir = lambda ad, a, d: yaz(f"  {ad:<34}{a:>12}{d:>16}")
        satir("kapsam (keyword alan kalıp)", f"{m_ai['kapsam']}/{len(liste)}",
              f"{m_det['kapsam']}/{len(liste)}")
        satir("kalıp başına keyword", f"{m_ai['ort_keyword']:.2f}", f"{m_det['ort_keyword']:.2f}")
        satir("tekil keyword", m_ai["tekil"], m_det["tekil"])
        satir("yasak token ihlali", f"%{m_ai['ihlal_oran']:.1f}", f"%{m_det['ihlal_oran']:.1f}")
        satir("şüpheli varyant çifti", m_ai["varyant"], m_det["varyant"])

        # --- ayırt etme, iki yöntem için ---
        ciftler = self._kardes_ciftleri(o["ciftler"], rastgele)
        if len(ciftler) >= 10:
            ck = [(kw.kalip_norm(a.ihale_adi), kw.kalip_norm(b.ihale_adi))
                  for a, b in ciftler]
            det_a = [set(kw.deterministik_keywords(x, token_df, bigram_df, n_kalip)) for x, _ in ck]
            det_b = [set(kw.deterministik_keywords(y, token_df, bigram_df, n_kalip)) for _, y in ck]
            dk, dr, dn = self._ayirt_skoru(det_a, det_b, rastgele)
            # AI tarafı için kardeş kalıpları da modele sorulur
            birimler, idx = [], 1
            for x, y in ck:
                birimler += [(idx, x), (idx + 1, y)]
                idx += 2
            ai_kardes = {}
            for j in range(0, len(birimler), self.grup):
                veri, usage = self._sor(client, model, birimler[j:j + self.grup])
                ai_kardes.update(veri)
                t_in += usage.input_tokens
                t_out += usage.output_tokens
            ku = lambda i: set(kw.kanonik_liste((ai_kardes.get(i) or {}).get("keywords")))
            ai_a = [ku(i) for i, _ in birimler[::2]]
            ai_b = [ku(i) for i, _ in birimler[1::2]]
            ak, ar, an = self._ayirt_skoru(ai_a, ai_b, rastgele)
            yaz("")
            satir("kardeş çift Jaccard", f"{ak:.3f}", f"{dk:.3f}")
            satir("rastgele çift Jaccard", f"{ar:.3f}", f"{dr:.3f}")
            satir("ayrışma oranı",
                  f"{(ak / ar if ar > 0.001 else 999):.0f}×",
                  f"{(dk / dr if dr > 0.001 else 999):.0f}×")

        # --- ASIL SORU: AI'nın kazandırdığı köprüler ---
        yaz(self.style.MIGRATE_HEADING(
            "\n── AI'nın KURDUĞU, deterministiğin kuramadığı eşleşmeler ──"))
        yaz("  AI'nın parasını hak ettiği yer burasıdır: adda geçmeyen üst kavram\n"
            "  sayesinde birbirini bulan işler. Liste boşsa AI'ya gerek yok.\n")
        ai_yakin = {(a, b): j for j, a, b, _ in m_ai["yakin"]}
        det_j = {}
        for j, a, b, _ in m_det["yakin"]:
            det_j[(a, b)] = j
        kazanc = []
        for (a, b), j in ai_yakin.items():
            dj = self._jaccard(det_map.get(a, set()), det_map.get(b, set()))
            if j >= 0.5 and dj < 0.2:
                kazanc.append((j, dj, a, b, sorted(ai_map[a] & ai_map[b])))
        if not kazanc:
            yaz(self.style.WARNING(
                "  (yok) — AI'nın kurduğu yakın eşleşmelerin hepsini deterministik de "
                "kuruyor.\n  Bu durumda keyword üretimi için AI'ya ödeme yapmanın "
                "gerekçesi kalmaz;\n  geriye yalnızca sektör etiketi kalır."))
        for j, dj, a, b, ortak in sorted(kazanc, reverse=True)[:12]:
            yaz(f"  AI {j:.2f} / det {dj:.2f}  {a[:60]}")
            yaz(f"                     {b[:60]}")
            yaz(f"        AI'nın ortak keyword'leri: {', '.join(ortak)}")

        # --- tersi: deterministiğin kurup AI'nın kuramadıkları ---
        kayip = []
        for (a, b), dj in det_j.items():
            aj = self._jaccard(ai_map.get(a, set()), ai_map.get(b, set()))
            if dj >= 0.5 and aj < 0.2:
                kayip.append((dj, aj, a, b))
        if kayip:
            yaz(self.style.MIGRATE_HEADING(
                "\n── Deterministiğin kurduğu, AI'nın kuramadığı eşleşmeler ──"))
            for dj, aj, a, b in sorted(kayip, reverse=True)[:8]:
                yaz(f"  det {dj:.2f} / AI {aj:.2f}  {a[:58]}")
                yaz(f"                      {b[:58]}")

        # --- sektör: deterministik tahmin AI ile ne kadar uyuşuyor ---
        yaz(self.style.MIGRATE_HEADING("\n── Sektör: deterministik tahmin vs AI ──"))
        uyum = kanit = 0
        sapma = []
        for _, kalip in liste:
            det_s, puan = kw.sektor_tahmin(kalip, det_map.get(kalip))
            ai_s = ai_sektor.get(kalip, "")
            if puan:
                kanit += 1
            if ai_s and det_s == ai_s:
                uyum += 1
            elif ai_s and len(sapma) < 12:
                sapma.append((kalip, ai_s, det_s))
        yaz(f"  AI ile aynı etiket : {uyum}/{len(liste)} (%{100 * uyum / max(len(liste), 1):.0f})")
        yaz(f"  kanıt bulunan      : {kanit}/{len(liste)} "
            f"(kalanı 'diger' — kural sözlüğü eksik)")
        yaz("  örnek ayrışmalar (AI → deterministik):")
        for kalip, a, d in sapma:
            yaz(f"    {kalip[:52]:<52} {SEKTORLER.get(a, a)[:22]:<22} → "
                f"{SEKTORLER.get(d, d)}")

        yaz(self.style.MIGRATE_HEADING("\n── Maliyet ──"))
        yaz(f"  AI            : {t_in:,} girdi + {t_out:,} çıktı token")
        if liste:
            usd = ((t_in / len(liste)) * 669_463 * 0.5
                   + (t_out / len(liste)) * 669_463 * 2.5) / 1_000_000
            yaz(f"                  669.463 kalıp → ~${usd:,.0f}")
        yaz("  deterministik : $0 · tüm arşiv için ~2 dk CPU\n")

    def _istatistik_topla(self, limit):
        """
        Tüm arşivden `(token_df, bigram_df, kalip_sayisi)` — deterministik yöntemin
        IDF ve PMI kaynağı.

        ⚠️ Bigram sözlüğü 1M kalıpta milyonlarca girdiye çıkabilir; bellek sabit
        kalsın diye periyodik olarak tek gözlemli çiftler budanır. Bilgi kaybı yok:
        PMI zaten `PMI_MIN_GOZLEM` altını kullanmıyor.
        """
        from collections import Counter

        qs = Tender.objects.exclude(ihale_adi="").only("pk", "ihale_adi").order_by("pk")
        if limit:
            qs = qs[:limit]
        token_df, bigram_df, toplam = Counter(), Counter(), 0
        self.stdout.write("  IDF/PMI istatistiği toplanıyor (deterministik yöntem için)…")
        for t in qs.iterator(chunk_size=5000):
            norm = kw.kalip_norm(t.ihale_adi)
            tokenlar = norm.split()
            if len(tokenlar) < 2:
                continue
            toplam += 1
            token_df.update(set(tokenlar))
            bigram_df.update({f"{a} {b}" for a, b in zip(tokenlar, tokenlar[1:])})
            if toplam % 250_000 == 0:
                onceki = len(bigram_df)
                for anahtar, adet in list(bigram_df.items()):
                    if adet < 2:
                        del bigram_df[anahtar]
                self.stdout.write(f"    …{toplam:,} kalıp · {len(token_df):,} token · "
                                  f"{len(bigram_df):,} bigram (budandı: {onceki:,})")
        self.stdout.write(f"  → {toplam:,} kalıp, {len(token_df):,} token, "
                          f"{len(bigram_df):,} bigram")
        return token_df, bigram_df, toplam

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
        for j in range(0, len(birimler), self.grup):
            veri, usage = self._sor(client, model, birimler[j:j + self.grup])
            sonuc.update(veri)
            t_in += usage.input_tokens
            t_out += usage.output_tokens

        def kume(i):
            s = sonuc.get(i)
            if not s:
                return set()
            return set(kw.kanonik_liste(s.get("keywords"), azami=AZAMI_KEYWORD))

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
               tekil_kanonik, sektor_dagilim, bos_sonuc, ayirt, eslesmeler,
               ham_ornek, kanonik_ornek,
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
        if kanonik_ornek:
            yaz(f"       kanonik ihlaller : {', '.join(sorted(kanonik_ornek)[:20])}")
        if ham_ornek:
            yaz(f"       ham ihlaller     : {', '.join(ham_ornek[:15])}")

        # B) hacim — ⚠️ KAPI DEĞİL, bkz. aşağıdaki uyarı
        patlama = len(tekil_kanonik) / max(len(satirlar), 1)
        yaz(f"  B) Keyword hacmi     : {patlama:.2f} tekil/kalıp "
            f"({len(tekil_kanonik)} tekil, {len(satirlar)} kalıp)")
        yaz(f"       boş sonuç (dürüst red): {bos_sonuc}/{len(satirlar)}")
        yaz(self.style.WARNING(
            "       ⚠ Bu oran KAPI DEĞİLDİR ve popülasyona ekstrapole EDİLEMEZ."))
        yaz("         Küçük örneklemde her yeni kalıp neredeyse hep yeni keyword "
            "getirir;\n         669k kalıpta oran doygunluğa girer (Heaps yasası) — "
            "dedup'taki doğum\n         günü paradoksuyla aynı hata sınıfı. Gerçek "
            "tekil keyword sayısı ancak\n         toplu işlemede bilinir; koruma "
            "orada: KEYWORD_MAX_UNIQUE + refresh_keyword_df.")
        # Varyant denetimi — patlamanın ölçülebilir kısmı budur.
        varyantlar = self._varyant_ciftleri(tekil_kanonik)
        if varyantlar:
            yaz(f"       şüpheli varyant çifti ({len(varyantlar)}): kanonikleştirici "
                "bunları\n         birleştirmeliydi — çoksa kural eksik demektir")
            for a, b in varyantlar[:12]:
                yaz(f"         {a}  ~  {b}")
        else:
            yaz("       şüpheli varyant çifti: yok")

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

        yaz(self.style.MIGRATE_HEADING(
            "\n── En yakın bulunan çiftler (YANLIŞ POZİTİF denetimi — göz gerekir) ──"))
        yaz("  Bunlar benchmark'ta birbirinin 'benzer işi' sayılacak. Alakasız bir çift"
            "\n  görüyorsan keyword katmanı o iş için gürültü üretiyor demektir.\n")
        for j, a, b, ortak in eslesmeler:
            yaz(f"  {j:.2f}  {a[:66]}")
            yaz(f"        {b[:66]}")
            yaz(f"        ortak: {', '.join(ortak)}")

        yaz(self.style.MIGRATE_HEADING("\n── Sektör dağılımı (ilk 12) ──"))
        for kod, adet in sorted(sektor_dagilim.items(), key=lambda x: -x[1])[:12]:
            yaz(f"  {adet:>4}  {SEKTORLER.get(kod, kod)}")

        # maliyet — Haiku 4.5 batch fiyatı üzerinden 1M ihale projeksiyonu
        yaz(self.style.MIGRATE_HEADING("\n── Maliyet ──"))
        yaz(f"  bu koşu: {t_in:,} girdi + {t_out:,} çıktı token, {gecen:.0f} sn")
        if n_kalip:
            in_kalip, out_kalip = t_in / n_kalip, t_out / n_kalip
            for hedef in (669_463,):     # üretimde ölçülen gerçek tekil kalıp sayısı
                # batch %50 indirim + Haiku 4.5 ($1/$5 per 1M)
                usd = (in_kalip * hedef * 0.5 + out_kalip * hedef * 2.5) / 1_000_000
                yaz(f"  {hedef:>7,} kalıp → ~${usd:,.0f}  (batch %50 indirimli, {model})")
            yaz(f"  girdi {in_kalip:.0f} tok/kalıp · çıktı {out_kalip:.0f} tok/kalıp")
            yaz("  ⚠ Girdinin çoğu system prompt'un her istekte tekrarı → toplu boru "
                "hattında\n    prompt cache devreye girince bu rakam DÜŞER (üst sınır).")

        if not satirlar:
            yaz(self.style.ERROR("\nHiç sonuç üretilemedi — CSV yazılmadı.\n"))
            return
        with open(csv_yolu, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
            w.writeheader()
            w.writerows(satirlar)
        yaz(self.style.SUCCESS(f"\nTam çıktı: {csv_yolu}  ({len(satirlar)} satır)\n"))
