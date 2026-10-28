"""
v2 tazelemesinin KONTROLLÜ denemesi — açmadan önce (ürün kararı 2026-10-27).

Seçilen ihaleler gerçekten v2'den tazelenir (yazar!) ve her birinin önceki/sonraki hâli
alan alan karşılaştırılır:

  • beklenen — v2'nin yazdığı kritik alanlar: durum, v2 kimliği, sonuç (sözleşme/bedel)
  • şüpheli  — BAŞKA herhangi bir alanın değişmesi (v2 yalnızca kritik alanları yazar),
               dolu alanın boşalması, çocuk satır sayısının düşmesi

Mobil hattın captcha sayacı önce/sonra raporlanır (aynı IP). Rapor JSON olarak yazılır.

    python manage.py v2_tazeleme_deneme [--adet 100] [--sonuc-orani 0.2]
"""
import json
import time
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ekap import tasks, v2_tazeleme as v2
from ekap.models import Tender

ALANLAR = (
    "ikn", "ekap_id", "ihale_adi", "idare_adi", "idare_id", "idare_kaynak",
    "en_ust_idare_kod", "en_ust_idare_adi", "il_id", "ihale_tarihi", "ilan_tarihi",
    "ihale_durum", "ihale_usul", "ihale_tip", "yasa_kapsami", "okas_ana_kod", "sektor",
    "kalip_hash", "seri_anahtar", "fiyat_disi_unsur_var", "e_eksiltme_yapilacak",
    "istekli_sayisi", "ilansiz_mi", "dokuman_sayisi", "yaklasik_maliyet_num",
    "sozlesme_sayisi", "toplam_sozlesme_bedeli", "detay_kaynak", "v2_id",
    "ihale_durum_aciklama", "iptal_nedeni", "ihale_il_adi", "ilce_adi", "ust_idare",
    "detail_synced_at",
)
COCUKLAR = (("ilan", "ilanlar"), ("sozlesme", "sozlesmeler"), ("okas", "okas_kalemleri"),
            ("tarih", "tarihler"))
# v2'nin yazmaya yetkili olduğu alanlar; gerisinin değişmesi şüphelidir.
BEKLENEN = {"ihale_durum", "ihale_durum_aciklama", "iptal_nedeni", "v2_id",
            "yaklasik_maliyet_num", "sozlesme_sayisi", "toplam_sozlesme_bedeli",
            "#sozlesme", "#ilan"}


def _bos(v):
    return v is None or v == ""


def _jsonla(v):
    if isinstance(v, Decimal):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _anlik(pk):
    t = Tender.objects.get(pk=pk)
    veri = {a: _jsonla(getattr(t, a, None)) for a in ALANLAR}
    for ad, iliski in COCUKLAR:
        veri[f"#{ad}"] = getattr(t, iliski).count()
    return veri


def _karsilastir(once, sonra):
    beklenen, supheli = {}, {}
    for alan in once:
        a, b = once[alan], sonra[alan]
        if a == b:
            continue
        dusus = (b < a) if alan.startswith("#") else (not _bos(a) and _bos(b))
        (beklenen if alan in BEKLENEN and not dusus else supheli)[alan] = [a, b]
    return beklenen, supheli

class Command(BaseCommand):
    help = "v2 tazelemesini sınırlı sayıda ihalede dener ve alan alan karşılaştırır (YAZAR)."

    def add_arguments(self, parser):
        parser.add_argument("--adet", type=int, default=100)
        parser.add_argument("--sonuc-orani", type=float, default=0.2, dest="sonuc_orani")
        parser.add_argument("--rapor", default="")

    def handle(self, *args, **o):
        if not tasks._dogrulama_kapisi("v2_tazeleme_deneme"):
            raise CommandError("v2 doğrulama kapısı kapalı.")
        from ekap.mobil.tasks import sayaclar as mobil_sayaclar

        simdi = timezone.now()
        n_sonuc = int(o["adet"] * o["sonuc_orani"])
        secim = [(t.pk, False) for t in v2.tazeleme_adaylari(simdi).only("pk")[: o["adet"] - n_sonuc]]
        secim += [(t.pk, True) for t in v2.sonuc_adaylari(simdi).only("pk")[:n_sonuc]]
        self.stdout.write(f"seçilen: {len(secim)} ihale ({n_sonuc} sonuç toplama)")

        captcha_once = mobil_sayaclar().get("captcha", 0)
        tempo = v2._Tempo(getattr(settings, "EKAP_V2_TAZELEME_ARALIK_SN", 3))
        cli = v2.EkapV2Client()
        satirlar, ozet, durum_gecis = [], {}, {}
        bas = time.monotonic()
        for i, (pk, sonuc_mu) in enumerate(secim, 1):
            once = _anlik(pk)
            t0 = time.monotonic()
            try:
                ne = v2.isle(Tender.objects.get(pk=pk), sonuc_mu, cli=cli, tempo=tempo)
                hata = ""
            except v2.EkapDogrulamaError as e:
                self.stdout.write(self.style.ERROR(f"doğrulama düştü, durduruldu: {e}"))
                break
            except Exception as e:                       # noqa: BLE001
                ne, hata = "hata", str(e)[:300]
            sonra = _anlik(pk)
            beklenen, supheli = _karsilastir(once, sonra)
            ozet[ne] = ozet.get(ne, 0) + 1
            gecis = f"{once['ihale_durum']}→{sonra['ihale_durum']}"
            durum_gecis[gecis] = durum_gecis.get(gecis, 0) + 1
            satirlar.append({"ikn": once["ikn"], "sonuc_mu": sonuc_mu, "ne": ne, "hata": hata,
                             "sure_sn": round(time.monotonic() - t0, 1), "beklenen": beklenen,
                             "supheli": supheli})
            if supheli:
                self.stdout.write(self.style.WARNING(f"  ŞÜPHELİ {once['ikn']}: {supheli}"))
            if i % 10 == 0:
                self.stdout.write(f"  {i}/{len(secim)} · {ozet}")

        supheli_alan = {}
        for s in satirlar:
            for alan in s["supheli"]:
                supheli_alan[alan] = supheli_alan.get(alan, 0) + 1
        rapor = {
            "zaman": simdi.isoformat(), "islenen": len(satirlar), "sonuclar": ozet,
            "sure_sn": round(time.monotonic() - bas), "durum_gecisleri": durum_gecis,
            "supheli_alanlari": supheli_alan,
            "mobil_captcha": [captcha_once, mobil_sayaclar().get("captcha", 0)],
            "v2_sayaclari": v2.sayaclar(), "satirlar": satirlar,
        }
        yol = o["rapor"] or f"/tmp/v2_deneme_{simdi:%Y%m%d_%H%M}.json"
        with open(yol, "w") as f:
            json.dump(rapor, f, ensure_ascii=False, indent=1)
        self.stdout.write(json.dumps({k: v for k, v in rapor.items() if k != "satirlar"},
                                     ensure_ascii=False, indent=1))
        self.stdout.write(self.style.SUCCESS(f"rapor: {yol}"))
