"""
EKAP Mobil API keşif/ölçüm aracı — **Faz 0**.

Hiçbir şey YAZMAZ (DB'ye dokunmaz); yalnızca ölçer ve rapor eder. Amaç, koda
başlamadan önce dört soruyu cevaplamak:

  1. `idare_id` mobil uçların herhangi birinden alınabiliyor mu?  → `--is idare`
  2. Güvenli tempo kaç saniye?                                    → `--is tempo`
  3. CAPTCHA OCR isabeti yüzde kaç?                               → `--is captcha`
  4. Mobil evren DB ile örtüşüyor mu?                             → `--is kapsam`

`--is uclar` tek bir ihale için bilinen TÜM uçları çağırıp ham yanıtları diske yazar.

⚠️ Bu komut gerçek istek atar ve hız sınırı IP tabanlıdır. Varsayılan aralık
`EKAP_MOBIL_MIN_INTERVAL_MS`'dir; `--aralik` ile geçici olarak değiştirilebilir.
⚠️ Eşiği zorlamak için tekrar tekrar engellenmeyin: aynı sunucu IP'si v2 için de
kullanılıyor ve tekrarlanan ihlal soğuma süresini uzatıyor.
"""
import json
import re
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ekap.mobil import captcha as captcha_mod
from ekap.mobil import constants as C
from ekap.mobil.client import EkapMobilClient, MobilError
from ekap.models import Tender
from ekap.utils import local_day_range


def _ikn_parcala(ikn: str):
    m = re.match(r"^\s*(\d{4})\s*/\s*(\d+)\s*$", ikn or "")
    if not m:
        raise CommandError(f"İKN biçimi 'YYYY/NNNNNNN' olmalı: {ikn!r}")
    return m.group(1), m.group(2)


class Command(BaseCommand):
    help = "EKAP Mobil API keşif/ölçüm aracı (yazma yapmaz)"

    def add_arguments(self, parser):
        parser.add_argument("--is", dest="is_", required=True,
                            choices=["uclar", "idare", "tempo", "captcha", "kapsam"])
        parser.add_argument("--ikn", help="Hedef İKN (uclar/idare için)")
        parser.add_argument("--adet", type=int, default=10,
                            help="Örneklem büyüklüğü (idare/captcha)")
        parser.add_argument("--gun", type=int, default=1, help="kapsam: kaç gün")
        parser.add_argument("--aralik", type=int,
                            help="Hız penceresi (sn) — bu çalışma için geçici")
        parser.add_argument("--cikti", default="", help="Ham yanıtların yazılacağı dizin")
        parser.add_argument("--adimlar", default="180,150,120,90",
                            help="tempo: denenecek aralıklar (sn, virgüllü)")
        parser.add_argument("--istek", type=int, default=10,
                            help="tempo: her adımda kaç istek")

    def handle(self, *args, **o):
        if o.get("aralik"):
            from django.conf import settings
            settings.EKAP_MOBIL_MIN_INTERVAL_MS = o["aralik"] * 1000
            self.stdout.write(f"Hız penceresi bu çalışma için {o['aralik']} sn.")
        getattr(self, f"_is_{o['is_']}")(o)

    # ── 1. Tüm uçlar, tek ihale ─────────────────────────
    def _is_uclar(self, o):
        ikn = o.get("ikn") or self._ornek_ikn()
        yil, sayi = _ikn_parcala(ikn)
        cli = EkapMobilClient()
        dizin = Path(o["cikti"] or f"/tmp/mobil_probe/{yil}_{sayi}")
        dizin.mkdir(parents=True, exist_ok=True)

        isler = [
            ("ihale", lambda: cli.ihale(yil, sayi)),
            ("sonuc_ilanlari", lambda: cli.sonuc_ilanlari(yil, sayi)),
            ("dokuman_liste", lambda: cli.dokuman_liste(yil, sayi)),
            ("teknik_sartname", lambda: cli.teknik_sartname(yil, sayi)),
            ("idari_sartname", lambda: cli.idari_sartname(yil, sayi)),
        ]
        sonuclar = {}
        for ad, cagir in isler:
            try:
                veri = cagir()
            except MobilError as e:
                self.stderr.write(self.style.WARNING(f"{ad}: {e}"))
                continue
            sonuclar[ad] = veri
            yol = dizin / f"{ad}.json"
            yol.write_text(json.dumps(veri, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            self.stdout.write(f"{ad:18} → {yol} ({len(json.dumps(veri))} bayt)")

        self._idare_avi(ikn, sonuclar)

    # ── 2. idare_id avı ─────────────────────────────────
    def _is_idare(self, o):
        """
        DB'de **sayısal `idare_id`si bilinen** ihaleleri seçer, mobil uçları çağırır
        ve ham yanıtın herhangi bir yerinde o değeri arar.

        Bu, alan adını bilmeye gerek bırakmaz: değer nerede geçiyorsa oradadır.
        """
        # ⚠️ `idare_id__regex` 1M satırlık tabloda tam tarama yapardı → son kayıtları
        # alıp sayısal olanları Python'da süzüyoruz (indeksli sıralama, küçük küme).
        aday = Tender.objects.exclude(idare_id="").order_by(
            "-ihale_tarihi"
        ).values_list("ikn", "idare_id")[:500]
        qs = [(i, d) for i, d in aday if d.isdigit()][:o["adet"]]
        if not qs:
            raise CommandError("DB'de sayısal idare_id taşıyan ihale bulunamadı.")

        cli = EkapMobilClient()
        bulundu = {}
        for ikn, idare_id in qs:
            yil, sayi = _ikn_parcala(ikn)
            paket = {}
            for ad, cagir in (
                ("ihale", lambda: cli.ihale(yil, sayi)),
                ("dokuman_liste", lambda: cli.dokuman_liste(yil, sayi)),
                ("idari_sartname", lambda: cli.idari_sartname(yil, sayi)),
            ):
                try:
                    paket[ad] = cagir()
                except MobilError as e:
                    self.stderr.write(self.style.WARNING(f"{ikn} {ad}: {e}"))
            yerler = self._ara(paket, idare_id)
            self.stdout.write(
                f"{ikn}  idare_id={idare_id}  → "
                + (", ".join(yerler) if yerler else self.style.WARNING("BULUNAMADI"))
            )
            for y in yerler:
                bulundu[y] = bulundu.get(y, 0) + 1

        self.stdout.write("")
        if bulundu:
            self.stdout.write(self.style.SUCCESS("idare_id geçen yerler (sıklık):"))
            for y, n in sorted(bulundu.items(), key=lambda x: -x[1]):
                self.stdout.write(f"  {n:3}× {y}")
        else:
            self.stdout.write(self.style.ERROR(
                "Hiçbir uçta idare_id bulunamadı. → Plan Faz 0.1'deki çözüm sırası: "
                "(1) üçlü eşleştirme ölçümü, (2) boş bırak + idare_kaynak kolonu, "
                "(3) v2 ile dar 'detay tamamlama'."
            ))

    def _ara(self, veri, hedef, yol="") -> list:
        """Ham yanıt ağacında `hedef` string'inin geçtiği yolları döndürür."""
        out = []
        if isinstance(veri, dict):
            for k, v in veri.items():
                out += self._ara(v, hedef, f"{yol}.{k}" if yol else k)
        elif isinstance(veri, list):
            for i, v in enumerate(veri[:50]):
                out += self._ara(v, hedef, f"{yol}[{i}]")
        elif veri is not None:
            s = str(veri)
            # ⚠️ Alt-string eşleşmesi yanıltır (tutar/tarih içinde geçebilir) →
            # sayı sınırıyla ara.
            if len(s) < 200_000 and re.search(rf"(?<!\d){re.escape(hedef)}(?!\d)", s):
                out.append(yol or "(kök)")
        return out

    def _idare_avi(self, ikn, sonuclar):
        t = Tender.objects.filter(ikn=ikn).only("idare_id").first()
        if not t or not t.idare_id:
            self.stdout.write("(DB'de bu İKN için idare_id yok — av atlandı)")
            return
        yerler = self._ara(sonuclar, t.idare_id)
        self.stdout.write("")
        self.stdout.write(
            f"idare_id={t.idare_id} → "
            + (", ".join(yerler) if yerler else self.style.WARNING("hiçbir uçta yok"))
        )

    # ── 3. Tempo rampası ────────────────────────────────
    def _is_tempo(self, o):
        """
        Yavaş rampa: aralığı adım adım daraltır, **ilk CAPTCHA'da durur** ve bir
        önceki adımı güvenli kabul eder.

        ⚠️ Bilerek muhafazakâr: eşiği tam bulmak için üst üste engellenmek, aynı IP'yi
        v2 için de riske atar ve soğuma süresini uzatır.
        """
        cli = EkapMobilClient()
        govde = cli.liste_govdesi(
            ihaleTarihiBaslangic=time.strftime("%Y-%m-%d 00:00:00"),
            ihaleTuru=1,
        )
        guvenli = None
        for adim in [int(x) for x in o["adimlar"].split(",") if x.strip()]:
            from django.conf import settings
            settings.EKAP_MOBIL_MIN_INTERVAL_MS = adim * 1000
            self.stdout.write(f"\n── {adim} sn aralık, {o['istek']} istek ──")
            for i in range(o["istek"]):
                try:
                    veri = cli.liste(govde)
                except MobilError as e:
                    self.stdout.write(self.style.ERROR(f"  {i+1}. istek: {e}"))
                    self.stdout.write(self.style.WARNING(
                        f"DURDURULDU. Güvenli kabul edilen aralık: "
                        f"{guvenli or 'ölçülemedi (ilk adımda engel)'} sn"
                    ))
                    return
                n = len(veri) if isinstance(veri, list) else "?"
                self.stdout.write(f"  {i+1}. istek ✓ ({n} kayıt)")
            guvenli = adim
        self.stdout.write(self.style.SUCCESS(
            f"\nTüm adımlar geçti. En dar güvenli aralık: {guvenli} sn "
            f"→ EKAP_MOBIL_MIN_INTERVAL_MS={guvenli * 1000}"
        ))

    # ── 4. CAPTCHA OCR isabeti ──────────────────────────
    def _is_captcha(self, o):
        """
        N captcha üretip OCR ile çözer ve `Sonuc` ile doğrular → **uçtan uca** isabet.

        ⚠️ İsabet düşükse otomatik çözüm `EKAP_MOBIL_CAPTCHA_OCR=False` ile kapatılır
        ve insan-döngü birincil olur: kötü bir OCR captcha'ları boşa tüketir.
        """
        cli = EkapMobilClient()
        dogru = bos = yanlis = 0
        for i in range(o["adet"]):
            try:
                veri = cli.captcha_getir()
            except MobilError as e:
                self.stderr.write(self.style.WARNING(f"{i+1}: {e}"))
                break
            cid = veri.get("captchaId") or ""
            resim = veri.get("captchaImage") or ""
            cevap = captcha_mod.ocr_coz(resim)
            if not cevap:
                bos += 1
                self.stdout.write(f"  {i+1:3}. OCR boş/uzunluk tutmadı")
            elif cli.captcha_sonuc(cid, cevap):
                dogru += 1
                self.stdout.write(self.style.SUCCESS(f"  {i+1:3}. {cevap} ✓"))
            else:
                yanlis += 1
                self.stdout.write(f"  {i+1:3}. {cevap} ✗")
            time.sleep(3)  # Getir'i de dövmeyelim
        toplam = dogru + bos + yanlis
        if toplam:
            self.stdout.write(
                f"\nİsabet: {dogru}/{toplam} (%{100*dogru/toplam:.0f}) · "
                f"boş {bos} · yanlış {yanlis}"
            )
            if dogru / toplam < 0.5:
                self.stdout.write(self.style.WARNING(
                    "⚠️ İsabet %50'nin altında → EKAP_MOBIL_CAPTCHA_OCR=False ile "
                    "başlatın, insan yedeği birincil olsun."
                ))

    # ── 5. Kapsam ───────────────────────────────────────
    def _is_kapsam(self, o):
        """Gün × tür dilimli tarama sonucunu DB ile karşılaştırır."""
        from datetime import timedelta

        from django.utils import timezone

        cli = EkapMobilClient()
        bugun = timezone.localdate()
        for gun_ofset in range(o["gun"]):
            gun = bugun + timedelta(days=gun_ofset)
            iknler, tavan_carpan = set(), []
            for tur in C.IHALE_TURU_DILIMLERI:
                govde = cli.liste_govdesi(
                    ihaleTarihiBaslangic=f"{gun:%Y-%m-%d} 00:00:00",
                    ihaleTarihiBitis=f"{gun:%Y-%m-%d} 23:59:59",
                    ihaleTuru=tur,
                )
                try:
                    veri = cli.liste(govde)
                except MobilError as e:
                    self.stderr.write(self.style.WARNING(f"{gun} tür={tur}: {e}"))
                    continue
                satirlar = veri if isinstance(veri, list) else []
                if len(satirlar) >= C.LISTE_TAVAN:
                    tavan_carpan.append(tur)
                iknler.update(str(s.get("ikn")) for s in satirlar if s.get("ikn"))

            # ⚠️ `ihale_tarihi__date=gun` KULLANILMAZ — kolonun üstündeki fonksiyon
            # indeksi öldürür (bkz. `ekap.utils.local_day_range`).
            bas, bit = local_day_range(gun)
            db = set(Tender.objects.filter(
                ihale_tarihi__gte=bas, ihale_tarihi__lt=bit
            ).values_list("ikn", flat=True))
            self.stdout.write(
                f"{gun}: mobil {len(iknler)} · DB {len(db)} · "
                f"mobilde olup DB'de olmayan {len(iknler - db)} · "
                f"DB'de olup mobilde olmayan {len(db - iknler)}"
            )
            if tavan_carpan:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠️ {tavan_carpan} türleri 250 tavanına takıldı → il kırılımı gerekli"
                ))

    def _ornek_ikn(self):
        t = Tender.objects.order_by("-ihale_tarihi").only("ikn").first()
        if not t:
            raise CommandError("--ikn verin (DB boş).")
        return t.ikn
