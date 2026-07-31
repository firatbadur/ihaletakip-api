"""
Sözleşme→yüklenici çözümlemesini elle çalıştırır (Celery gerekmez, EKAP'a gitmez).

`Tender.detail_raw` tam bir yerel arşiv olduğu için tüm iş offline yapılır: para
onarımı, EKAP anahtarlarının doldurulması, firma türetme ve Sonuç İlanı ayrıştırma
tek geçişte olur. Bu yüzden migration'larda `RunPython` YOKTUR — deploy'daki
healthcheck penceresi hiç riske girmez.

Kullanım:
    python manage.py rebuild_contractors --dry-run --limit 50   # yazmadan incele
    python manage.py rebuild_contractors --ikn 2024/1362677 --dry-run
    python manage.py rebuild_contractors --limit 5000           # partiler hâlinde
    python manage.py rebuild_contractors --aggregates-only
    python manage.py rebuild_contractors --reset                # imleci sıfırla
    python manage.py rebuild_contractors --purge                # firmaları da sil
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from ekap import contractors as contractors_mod
from ekap import sync as sync_mod
from ekap.models import (
    Contract,
    Contractor,
    ContractorAlias,
    ContractorMembership,
    SyncCheckpoint,
    Tender,
)


class Command(BaseCommand):
    help = "Sözleşmeleri yüklenici firmalara bağlar (detail_raw arşivinden, offline)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1000,
                            help="Bu çalışmada işlenecek azami ihale (vars. 1000).")
        parser.add_argument("--from-pk", type=int, default=None,
                            help="Bu Tender pk'sinden itibaren başla.")
        parser.add_argument("--ikn", type=str, default=None,
                            help="Tek ihaleyi işle (hata ayıklama).")
        parser.add_argument("--aggregates-only", action="store_true",
                            help="Yalnızca firma agregalarını yeniden hesapla.")
        parser.add_argument("--reset", action="store_true",
                            help="'contractors' checkpoint imlecini sıfırla.")
        parser.add_argument("--purge", action="store_true",
                            help="Firma/alias/üyelik tablolarını temizle (onay ister).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Hiçbir şey yazmadan ham ad → kanonik anahtar tablosu bas.")

    def handle(self, *args, **o):
        if o["purge"]:
            self._purge()
            return
        if o["reset"]:
            SyncCheckpoint.objects.update_or_create(
                name="contractors",
                defaults={"cursor_skip": 0, "done": False, "extra": {}},
            )
            self.stdout.write(self.style.SUCCESS("✅ contractors imleci sıfırlandı"))
            return
        if o["aggregates_only"]:
            n = contractors_mod.recompute_aggregates()
            self.stdout.write(self.style.SUCCESS(f"✅ {n} firmanın agregası güncellendi"))
            return

        qs = Tender.objects.filter(detail_raw__isnull=False)
        if o["ikn"]:
            qs = qs.filter(ikn=o["ikn"])
            if not qs.exists():
                raise CommandError(f"İhale bulunamadı ya da detayı yok: {o['ikn']}")
        elif o["from_pk"]:
            qs = qs.filter(pk__gte=o["from_pk"])
        qs = qs.order_by("pk")[: o["limit"]]

        if o["dry_run"]:
            self._dry_run(qs)
            return

        processed = errors = contracts = 0
        touched = set()
        for tender in qs.iterator(chunk_size=50):
            try:
                res = sync_mod.sync_contracts_from_raw(tender)
                contracts += res["contracts"]
                touched |= res["contractors"]
                processed += 1
            except Exception as e:
                errors += 1
                self.stderr.write(f"  ⚠ {tender.ikn}: {e}")

        if touched:
            contractors_mod.recompute_aggregates(touched)

        self.stdout.write(self.style.SUCCESS(
            f"✅ {processed} ihale · {contracts} sözleşme · {len(touched)} firma "
            f"· {errors} hata"
        ))
        self._report()

    # ── Yardımcılar ────────────────────────────────────
    def _dry_run(self, qs):
        """
        Kanonikleştiriciyi AYARLAMA döngüsü: hiçbir şey yazmadan hangi ham adın hangi
        anahtara ve mevcut/yeni hangi firmaya gideceğini gösterir.
        """
        rows, seen = [], {}
        for tender in qs.iterator(chunk_size=50):
            raw = tender.detail_raw or {}
            data = raw.get("item", raw) if isinstance(raw, dict) else {}
            for s in (data.get("sozlesmeBilgiList") or []):
                ham = (s.get("yukleniciAdi") or "").strip()
                if not ham:
                    continue
                key = contractors_mod.canonical_key(ham)
                jv = contractors_mod.split_joint_venture(ham)
                existing = Contractor.objects.filter(kanonik_anahtar=key).first()
                durum = f"mevcut #{existing.pk}" if existing else (
                    "YENİ (bu çalışmada tekrar)" if key in seen else "YENİ")
                seen[key] = ham
                rows.append((tender.ikn, ham, key, durum, jv))

        self.stdout.write(f"\n{len(rows)} sözleşme, {len(seen)} benzersiz kanonik anahtar\n")
        for ikn, ham, key, durum, jv in rows:
            self.stdout.write(f"  {ikn}")
            self.stdout.write(f"    ham    : {ham[:96]}")
            self.stdout.write(f"    anahtar: {key[:96]}")
            self.stdout.write(f"    durum  : {durum}")
            if jv.is_jv:
                self.stdout.write(self.style.WARNING(
                    f"    ORTAK GİRİŞİM: güven={jv.guven} üye={len(jv.members)}"
                ))
                for m in jv.members:
                    self.stdout.write(f"        └ {m[:88]}")
        self.stdout.write(self.style.WARNING("\n⚠ --dry-run: hiçbir şey yazılmadı"))

    def _report(self):
        toplam = Contractor.objects.count()
        bagsiz = Contract.objects.filter(yuklenici__isnull=True).count()
        anahtarsiz = Contract.objects.filter(ekap_sozlesme_id="").count()
        mukerrer = (Contract.objects.values("tender_id", "ekap_sozlesme_id")
                    .annotate(n=Count("id")).filter(n__gt=1).count())
        cozumsuz = Contractor.objects.filter(uyeleri_cozumlendi=False).count()
        self.stdout.write(
            f"   firma={toplam} alias={ContractorAlias.objects.count()} "
            f"üyelik={ContractorMembership.objects.count()}"
        )
        self.stdout.write(f"   yüklenicisi bağlanmamış sözleşme: {bagsiz}")
        if cozumsuz:
            self.stdout.write(self.style.WARNING(
                f"   ⚠ {cozumsuz} ortak girişimin üyeleri çözülemedi (elle inceleyin)"
            ))
        # 0007 unique-constraint migration'ı için geçit koşulları
        style = self.style.SUCCESS if (anahtarsiz == 0 and mukerrer == 0) else self.style.WARNING
        self.stdout.write(style(
            f"   unique-constraint geçidi: anahtarsız={anahtarsiz} mükerrer={mukerrer}"
        ))

    def _purge(self):
        onay = input("Tüm firma/alias/üyelik kayıtları silinecek. 'evet' yazın: ")
        if onay.strip().lower() != "evet":
            self.stdout.write("İptal edildi.")
            return
        ContractorMembership.objects.all().delete()
        ContractorAlias.objects.all().delete()
        Contract.objects.update(yuklenici=None)
        n, _ = Contractor.objects.all().delete()
        SyncCheckpoint.objects.filter(name="contractors").update(done=False, extra={})
        self.stdout.write(self.style.SUCCESS(f"✅ temizlendi ({n} kayıt), imleç sıfırlandı"))
