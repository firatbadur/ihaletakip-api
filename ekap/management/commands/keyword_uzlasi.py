"""
Ad uzlaşısıyla eksik keyword onarımı — **deterministik öneri + AI doğrulama**.

## Sorun

Model bazı ihalelere işin kendisini değil bir yan unsurunu etiketlemiş. Üretimde
ölçüldü (2026-09-23): "Sürekli Atıksu İzleme Sistemi (SAİS) Kabini Bakım Hizmeti"
ihalesinin keyword'leri `atiksu aritma` + `atiksu analizi` — yani fiyat analizi onu
**arıtma tesisi işletme** işleriyle karşılaştırıyordu. Çapa (`keywords.capa_kavramlari`)
bunu kurtaramaz: yanlış mahalleden başlıyor.

## Yöntem — AI'ya keyword ÜRETTİRMEK yerine, arşivin uzlaşısını sormak

Bir kalıbın **ad komşuları** (trigram, `ekap_tender_ihale_adi_norm_trgm` indeksi)
zaten doğru etiketlenmişse, onların uzlaştığı keyword eksik olanı söyler:

    "SAİS Kabini Bakım"  → 60 ad komşusunun 19'unda `atiksu izleme sistemi` (lift 11.899)

⚠️ **Kendi kalıbı komşuluktan DIŞLANIR** (`kalip_hash <> ...`): aynı kalıbı paylaşan
ihalelerin keyword'leri birebir aynıdır, dahil edilseydi kalıp kendi yanlışını
kendine onaylatırdı — sessiz ve tam ters yönde bir hata.

⚠️ **Uzlaşı TEK BAŞINA YETMEZ, semantik yargı gerekir.** 260 ihalelik pilotta ölçüldü:
`lift < 100` bandında öneriler yarı yarıya bozuktu — "1 KALEM MOTORİN ALIMI"na
`benzin`, "Taşımalı Ortaöğretim"e `personel tasima`, "Beton Agregası"na `hazir beton`.
Frekans motorinle benzini ayıramaz. Bu yüzden öneriler **AI'ya doğrulatılır**:
model keyword ÜRETMEZ, yalnızca "bu ihaleye bu kelime uyar mı" sorusuna evet/hayır
der — istek başına ~25 kelimelik bir yargı, 667k kalıbı baştan etiketlemek değil.

⚠️ **Hiçbir keyword SİLİNMEZ**, yalnızca eklenir. Silmek, doğru olduğunu
bilmediğimiz bir gerekçeyle veri atmak olurdu.

Kullanım:
    python manage.py keyword_uzlasi --pilot 120                # yalnızca öneri üret
    python manage.py keyword_uzlasi --pilot 120 --dogrula      # + AI doğrulaması
    python manage.py keyword_uzlasi --dogrula --uygula         # toplu iş (yazar)
"""
import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from ekap import keywords as kw
from ekap.models import Keyword, Tender, TenderKeyword, TenderNamePattern
from ekap.utils import normalize_tr

MIN_KOMSU = 20          # bu kadar ad komşusu yoksa uzlaşı istatistiği gürültüdür
MIN_PAY = 0.50          # uzlaşı: komşuların en az yarısında geçmeli
MIN_LIFT = 100.0        # pilotta ölçüldü: altında isabet ~%50, üstünde ~%90
TRIGRAM_ESIK = 0.45
DOGRULAMA_GRUP = 40     # istek başına öneri sayısı

_SISTEM = """Türk kamu ihaleleri (EKAP) üzerinde çalışan bir sınıflandırma denetçisisin.

Sana bir ihale adı, o ihaleye şu an atanmış anahtar kelimeler ve EKLENMESİ ÖNERİLEN
bir anahtar kelime verilecek. Önerilen kelime, benzer işleri bulup FİYAT
KARŞILAŞTIRMASI yapmak için kullanılacak.

Tek soruya cevap ver: bu kelime, ihalenin satın aldığı şeyi doğru tanımlıyor mu?

"hayir" de:
- Kelime farklı bir ürün/hizmet sınıfına götürüyorsa (motorin ihalesine "benzin",
  agrega ihalesine "hazir beton", öğrenci taşımaya "personel tasima").
- Kelime anlamsız/çöp ise ("binasi", "grubu" gibi tek başına bilgi taşımayan ekler).
- Kelime o kadar genel ki hiçbir şeyi ayırt etmiyorsa ("yol", "malzeme").

"evet" de:
- Kelime işin kendisini ya da doğrudan kapsadığı ürün/hizmeti adlandırıyorsa.
- Daha özel bir ifadeyse ve ihale adıyla uyumluysa.

Kararsızsan "hayir" de — yanlış bir eşleştirme, eksik bir eşleştirmeden zararlıdır.

Yalnızca şu JSON'u döndür: {"sonuclar":[{"id":<int>,"uygun":<bool>}]}"""


class Command(BaseCommand):
    help = "Ad uzlaşısıyla eksik keyword önerir; AI ile doğrular; isteğe bağlı uygular."

    def add_arguments(self, p):
        p.add_argument("--pilot", type=int, default=0,
                       help="Yalnızca N öneri üret ve dur (ölçüm için)")
        p.add_argument("--dogrula", action="store_true", help="Önerileri AI'ya doğrulat")
        p.add_argument("--uygula", action="store_true",
                       help="Doğrulananları YAZ (varsayılan: yalnızca göster)")
        p.add_argument("--min-lift", type=float, default=MIN_LIFT)
        p.add_argument("--max-seconds", type=int, default=0)
        p.add_argument("--from-pk", type=int, default=0)

    # ── öneri üretimi ────────────────────────────────────
    def _komsular(self, cur, ad_norm, kalip_hash):
        # ⚠️ `%` operatörü trigram GIN indeksini kullanır; `similarity() > x` KULLANMAYIN
        # (kolon üzerinde fonksiyon → indeks devre dışı, bkz. CLAUDE.md `icontains` notu).
        # ⚠️ psycopg'de `%%` literal yüzde işaretidir → SQL'e `%` (trigram operatörü)
        # olarak gider. `similarity(kolon, x) > 0.45` YAZILMAZ: kolon üzerindeki
        # fonksiyon GIN indeksini devre dışı bırakır (CLAUDE.md `icontains` tuzağı).
        cur.execute(
            "SELECT id FROM ekap_tender "
            "WHERE ihale_adi_norm %% %s AND kalip_hash <> %s LIMIT 80",
            [ad_norm, kalip_hash])
        return [r[0] for r in cur.fetchall()]

    def _oneri(self, cur, kalip, n_ihale):
        ad_norm = normalize_tr(kalip.ornek_ad or kalip.kalip_norm or "")
        if len(ad_norm) < 12:
            return None
        komsu = self._komsular(cur, ad_norm, kalip.kalip_hash)
        if len(komsu) < MIN_KOMSU:
            return None
        from collections import Counter
        sayac, dfler, adlar = Counter(), {}, {}
        for kid, metin, ham, df in (TenderKeyword.objects.filter(tender_id__in=komsu)
                                    .values_list("keyword_id", "keyword__metin",
                                                 "keyword__metin_ham",
                                                 "keyword__kullanim_sayisi")):
            sayac[kid] += 1
            dfler[kid] = df
            adlar[kid] = ham or metin
        aday = [(((f / len(komsu)) / (max(dfler[k], 1) / n_ihale)), f, k)
                for k, f in sayac.items() if f / len(komsu) >= MIN_PAY]
        if not aday:
            return None
        lift, f, kid = max(aday)
        mevcut = set(kalip.keyword_ids or [])
        # ⚠️ Kavram grubu kontrolü: kalıpta `atiksu izleme` varken `atiksu izleme
        # sistemi` önermek gereksizdir — çapa ikisini zaten aynı kümeye koyuyor.
        metin = Keyword.objects.filter(pk=kid).values_list("metin", flat=True).first() or ""
        if mevcut & kw.kavram_grubu(kid, metin):
            return None
        return {"kalip_pk": kalip.pk, "keyword_id": kid, "ad": kalip.ornek_ad or "",
                "mevcut": list(Keyword.objects.filter(pk__in=mevcut)
                               .values_list("metin_ham", flat=True)),
                "yeni": adlar[kid], "lift": round(lift), "pay": f, "komsu": len(komsu)}

    # ── AI doğrulama ─────────────────────────────────────
    def _dogrula(self, oneriler):
        from ai.services.claude import call_claude, get_api_key
        api_key = get_api_key()
        onay, in_tok, out_tok = {}, 0, 0
        for i in range(0, len(oneriler), DOGRULAMA_GRUP):
            grup = oneriler[i:i + DOGRULAMA_GRUP]
            satirlar = "\n".join(
                f'id={j} | ihale: {o["ad"][:110]} | mevcut: {", ".join(o["mevcut"])[:70]} '
                f'| onerilen: {o["yeni"]}'
                for j, o in enumerate(grup, start=i))
            sonuc = call_claude(api_key, [], f"{_SISTEM}\n\n{satirlar}",
                                max_tokens=2000, model=settings.CLAUDE_CHAT_MODEL)
            u = sonuc.get("usage") or {}
            in_tok += u.get("input_tokens", 0)
            out_tok += u.get("output_tokens", 0)
            metin = sonuc["analysis"]
            bas, son = metin.find("{"), metin.rfind("}")
            if bas < 0 or son < 0:
                self.stderr.write(f"  ⚠ AI yanıtı JSON değil: {metin[:80]}")
                continue
            for s in json.loads(metin[bas:son + 1]).get("sonuclar", []):
                if isinstance(s.get("id"), int):
                    onay[s["id"]] = bool(s.get("uygun"))
        return onay, in_tok, out_tok

    def handle(self, *a, **o):
        yaz = self.stdout.write
        n_ihale = Tender.objects.count() or 1
        hedef = (TenderNamePattern.objects.filter(durum="ok", pk__gt=o["from_pk"])
                 .exclude(ornek_ad="").order_by("-ihale_sayisi", "pk"))
        yaz(self.style.MIGRATE_HEADING("\n═══ AD UZLAŞISI — KEYWORD ONARIMI ═══"))
        oneriler, bakilan, basla = [], 0, time.monotonic()
        with connection.cursor() as cur:
            cur.execute("SET pg_trgm.similarity_threshold = %s", [TRIGRAM_ESIK])
            for kalip in hedef.iterator(chunk_size=200):
                bakilan += 1
                try:
                    one = self._oneri(cur, kalip, n_ihale)
                except Exception as exc:                       # noqa: BLE001
                    self.stderr.write(f"  kalıp {kalip.pk}: {exc}")
                    continue
                if one and one["lift"] >= o["min_lift"]:
                    oneriler.append(one)
                if o["pilot"] and len(oneriler) >= o["pilot"]:
                    break
                if o["max_seconds"] and time.monotonic() - basla >= o["max_seconds"]:
                    yaz(self.style.WARNING(f"  Süre doldu. --from-pk {kalip.pk}"))
                    break
        sure = time.monotonic() - basla
        yaz(f"  bakılan kalıp : {bakilan:,}  ({sure:.0f} sn, {1000*sure/max(bakilan,1):.0f} ms/kalıp)")
        yaz(f"  ÖNERİ         : {len(oneriler):,}  (%{100*len(oneriler)/max(bakilan,1):.1f})")
        if not oneriler:
            return

        onay = {}
        if o["dogrula"]:
            t0 = time.monotonic()
            onay, it, ot = self._dogrula(oneriler)
            usd = (it * 1.0 + ot * 5.0) / 1_000_000      # Haiku 4.5 normal fiyat
            yaz(f"  AI doğrulama  : {len(onay)}/{len(oneriler)} yanıt, "
                f"{time.monotonic()-t0:.0f} sn, {it:,} in + {ot:,} out tok ≈ ${usd:.3f}")
            kabul = sum(1 for v in onay.values() if v)
            yaz(f"  AI kabul      : {kabul}  ·  AI RED: {len(onay)-kabul}")

        yaz("\n" + "=" * 96)
        for i, one in enumerate(oneriler):
            damga = "" if not onay else ("  ✓AI" if onay.get(i) else "  ✗AI RED")
            yaz(f"lift{one['lift']:>7} | {one['ad'][:44]:44} | "
                f"{', '.join(one['mevcut'])[:30]:30} → {one['yeni']}{damga}")

        if o["uygula"] and onay:
            self._uygula([one for i, one in enumerate(oneriler) if onay.get(i)])

    def _uygula(self, kabuller):
        """Onaylanan keyword'ü kalıba ve onu paylaşan ihalelere yazar."""
        from ekap.tasks import _bekleyen_ihalelere_uygula
        yazilan = 0
        for one in kabuller:
            kalip = TenderNamePattern.objects.get(pk=one["kalip_pk"])
            idler = list(kalip.keyword_ids or [])
            if one["keyword_id"] in idler:
                continue
            idler.append(one["keyword_id"])
            TenderNamePattern.objects.filter(pk=kalip.pk).update(keyword_ids=idler)
            _bekleyen_ihalelere_uygula([(kalip.kalip_hash, idler, kalip.sektor)])
            yazilan += 1
        self.stdout.write(self.style.SUCCESS(f"\n  uygulanan kalıp: {yazilan:,}"))
