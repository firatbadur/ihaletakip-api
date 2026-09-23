"""
Kalıbı çözülmüş ama keyword'ü yazılmamış ihaleleri onarır (tek seferlik).

## Sorun

Yeni bir ihale geldiğinde kalıbı henüz sözlükte olmaz → `keywords.uygula` kalıbı
`pending` açıp **False** döner (keyword yazılmaz). Kalıp saatler sonra AI ile `ok`
olur, ama onu **bekleyen ihaleye geri dönen kimse yoktu**:
`propagate_tender_keywords` arşivi bitirip `done=True` olmuş ve
`keywords.uygula` ancak detay YENİDEN senkronlanırsa tekrar çalışıyor.

Üretimde ölçüldü (2026-09-23, tinyfect):

    son 7 günde DB'ye giren ihale : 1.174
      keyword'ü olan              :   394  (%33,6)
    incelenen 294 bağsız ihalenin 294'ünde kalıp, detay senkronundan SONRA çözülmüş

⚠️ **Belirti sessizdir**: hata yok, `SyncRun` temiz, kalıp sözlüğü `ok` dolu. Yalnızca
fiyat analizi o ihalelerde `anahtar` kademesini kullanamaz ve daha genel/alakasız bir
kademeye düşer — yani özelliğin var oluş sebebi olan şikâyet geri gelir.

Kalıcı düzeltme `tasks._bekleyen_ihalelere_uygula` (kalıp `ok` olur olmaz uygulanır);
bu komut yalnızca **geçmiş birikimi** kapatır.

## Ne yapar

`durum="ok"` kalıpları gezer, her birini `kalip_hash` üzerinden ihalelere uygular.
Saf DB işi — `detail_raw` okumaz, EKAP'a ve AI'ya gitmez, para harcamaz.
`ignore_conflicts` sayesinde tekrar çalıştırmak zararsızdır.

⚠️ Varsayılan pencere `tender_keywords` checkpoint'inin son güncellemesidir: yayma
görevi o ana kadarki kalıpları zaten uygulamıştı, ondan öncesini yeniden taramak
milyonlarca satırı boşuna gezmek olurdu.

Kullanım:
    python manage.py fix_bekleyen_keyword --dry-run
    python manage.py fix_bekleyen_keyword
    python manage.py fix_bekleyen_keyword --since 2026-09-01 --max-seconds 600
"""
import time

from django.core.management.base import BaseCommand
from django.utils import timezone as tz
from django.utils.dateparse import parse_datetime

from ekap.models import SyncCheckpoint, TenderNamePattern
from ekap.tasks import _bekleyen_ihalelere_uygula

PARCA = 200


class Command(BaseCommand):
    help = "Çözülmüş kalıpları, keyword'ü yazılmamış ihalelere uygular."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="YYYY-MM-DD — bu tarihten sonra çözülen "
                                            "kalıplar (vars. yayma checkpoint'i)")
        parser.add_argument("--tumu", action="store_true",
                            help="Pencereyi yok say, TÜM 'ok' kalıpları gez")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--max-seconds", type=int, default=0)

    def handle(self, *args, **o):
        yaz = self.stdout.write
        qs = TenderNamePattern.objects.filter(durum="ok").exclude(kalip_hash="")

        if o["tumu"]:
            pencere = None
        elif o["since"]:
            pencere = parse_datetime(f"{o['since']}T00:00:00+03:00")
            if pencere is None:
                yaz(self.style.ERROR("  --since biçimi: YYYY-MM-DD"))
                return
        else:
            cp = SyncCheckpoint.objects.filter(name="tender_keywords").first()
            pencere = cp.updated_at if cp else None

        if pencere is not None:
            qs = qs.filter(islendi_at__gte=pencere)

        toplam = qs.count()
        yaz(self.style.MIGRATE_HEADING("\n═══ BEKLEYEN KEYWORD ONARIMI ═══"))
        yaz(f"  pencere       : {pencere:%Y-%m-%d %H:%M}" if pencere else
            "  pencere       : (tümü)")
        yaz(f"  'ok' kalıp    : {toplam:,}")
        if not toplam:
            yaz(self.style.SUCCESS("  Uygulanacak kalıp yok."))
            return
        if o["dry_run"]:
            yaz(self.style.WARNING("  --dry-run: hiçbir şey yazılmadı."))
            return

        basla = time.monotonic()
        butce = o["max_seconds"]
        bakilan = uygulanan = 0
        # ⚠️ PK imleçli: `OFFSET` büyük tabloda her turda baştan sayar.
        imlec = 0
        while True:
            parca = list(qs.filter(pk__gt=imlec).order_by("pk")
                         .values_list("pk", "kalip_hash", "keyword_ids", "sektor")[:PARCA])
            if not parca:
                break
            imlec = parca[-1][0]
            uygulanan += _bekleyen_ihalelere_uygula([(h, k, s) for _, h, k, s in parca])
            bakilan += len(parca)
            gecen = time.monotonic() - basla
            if (bakilan // PARCA) % 10 == 0:
                yaz(f"  …kalıp {bakilan:,}/{toplam:,} · dokunulan ihale "
                    f"{uygulanan:,} ({gecen:.0f} sn)")
            if butce and gecen >= butce:
                yaz(self.style.WARNING(
                    f"  Süre bütçesi doldu. Kaldığı yer: --since aynı, pk>{imlec}"))
                break

        yaz(self.style.SUCCESS(
            f"\n  gezilen kalıp   : {bakilan:,}"))
        yaz(f"  dokunulan ihale : {uygulanan:,}  ({time.monotonic() - basla:.0f} sn)")
        yaz("")
