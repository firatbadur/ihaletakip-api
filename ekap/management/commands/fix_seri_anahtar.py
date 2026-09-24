"""
`Tender.seri_anahtar` kolonunu yeni tanımıyla yeniden hesaplar (tek seferlik onarım).

## Neden

`series.series_key` 2026-09-24'te değişti: anahtar `(idare_id, okas_ana_kod, iskelet)`
üçlüsünden `(idare_id, iskelet)` çiftine indi. Gerekçe ve ölçümler `ekap/series.py`
modül başlığındadır — özetle `okas_ana_kod` kararsız olduğu için gerçek serilerin
**%36,9'u** ikiye/üçe bölünüyordu.

Anahtarın tanımı değiştiği için **arşivdeki her satırın değeri de değişir**; ingest
yalnızca yeni/yeniden senkronlanan kayıtları düzeltir. Bu komut geçmişi hizalar.

⚠️ Bu, CLAUDE.md'de iki kez belgelenen hata sınıfının aynısıdır: *"bir değer
ingest'te dolar" demek yetmez — tanım değişince geçmiş için ayrı bir doldurma adımı
gerekir"* (`Contract.sektor` %0,04, `okas_ana_kod` 0014).

## Ne yapar

`(pk, idare_id, ihale_adi)` okur, anahtarı yeniden hesaplar, **değişenleri** yazar.
**Saf DB işi** — `detail_raw` okunmaz (kolonlar satır-içi), TOAST'a dokunulmaz,
EKAP'a gidilmez.

⚠️ **PK imleçli ve parçalı**: tek dev `UPDATE` `settings.DB_STATEMENT_TIMEOUT_MS`
(240 sn) tarafından iptal edilir ve işin tamamı boşa gider (CLAUDE.md → "Yetim sorgu
sarmalı"). Kesilirse `--from-pk` ile devam edilir.

⚠️ `seri_anahtar` **indekslidir** → 1M satırlık güncelleme HOT update yapamaz ve
indeksi şişirir. Komut bitince **`VACUUM (ANALYZE) ekap_tender`** koşulmalıdır
(CLAUDE.md → "önce VACUUM (ANALYZE), sonra indeks eklemeyi düşünün").

⚠️ Bitince `detect_recurring_series` **yeniden koşturulmalıdır**; aksi hâlde seri
tablosu eski anahtarlarla kalır ve `/ekap/recurring/` hiçbir ihaleyle eşleşmez.

Kullanım:
    python manage.py fix_seri_anahtar --dry-run
    python manage.py fix_seri_anahtar
    python manage.py fix_seri_anahtar --from-pk 500000 --max-seconds 600
"""
import time

from django.core.management.base import BaseCommand

from ekap.models import Tender
from ekap.series import series_key

PARCA = 20_000


class Command(BaseCommand):
    help = "Tender.seri_anahtar'ı yeni tanımıyla yeniden hesaplar (tek seferlik onarım)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Yalnızca sayar, yazmaz")
        parser.add_argument("--parca", type=int, default=PARCA,
                            help=f"Tur başına PK aralığı (vars. {PARCA})")
        parser.add_argument("--from-pk", type=int, default=0,
                            help="Bu PK'dan devam et (kesilen işi sürdürmek için)")
        parser.add_argument("--max-seconds", type=int, default=0,
                            help="Süre bütçesi (0 = sınırsız)")

    def handle(self, *a, **o):
        basla = time.monotonic()
        bitis = Tender.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
        imlec = o["from_pk"]
        bakilan = degisen = bosalan = 0

        self.stdout.write(f"son pk={bitis:,} · imleç={imlec:,} · parça={o['parca']:,}")

        while imlec <= bitis:
            ust = imlec + o["parca"]
            yigin = []
            for pk, idare, adi, eski in (
                Tender.objects.filter(pk__gt=imlec, pk__lte=ust)
                .order_by()
                .values_list("pk", "idare_id", "ihale_adi", "seri_anahtar")
                .iterator(chunk_size=5000)
            ):
                bakilan += 1
                yeni = series_key(idare or "", adi or "")
                if yeni == (eski or ""):
                    continue
                degisen += 1
                bosalan += 1 if not yeni else 0
                yigin.append(Tender(pk=pk, seri_anahtar=yeni))

            if yigin and not o["dry_run"]:
                Tender.objects.bulk_update(yigin, ["seri_anahtar"], batch_size=2000)

            imlec = ust
            if bakilan and (bakilan % 200_000 < o["parca"]):
                self.stdout.write(
                    f"  pk≤{imlec:,} · bakılan {bakilan:,} · değişen {degisen:,} "
                    f"· {time.monotonic() - basla:.0f} sn"
                )
            if o["max_seconds"] and time.monotonic() - basla > o["max_seconds"]:
                self.stdout.write(self.style.WARNING(
                    f"süre doldu — devam için: --from-pk {imlec}"))
                break

        self.stdout.write(self.style.SUCCESS(
            f"bakılan {bakilan:,} · değişen {degisen:,} · boşalan {bosalan:,} "
            f"· {time.monotonic() - basla:.0f} sn"
            + (" (DRY-RUN, yazılmadı)" if o["dry_run"] else "")
        ))
        if not o["dry_run"] and degisen:
            self.stdout.write(self.style.WARNING(
                "⚠️ Sırada: VACUUM (ANALYZE) ekap_tender · "
                "ardından run_ingest/beat ile detect_recurring_series"))
