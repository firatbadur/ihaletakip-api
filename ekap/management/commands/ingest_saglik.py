"""
Toplama hattının tek ekranda sağlık raporu.

⚠️ **Neden var**: "ihale gelmiyor / bildirim gelmiyor" şikâyetinin sebebi zincirin
farklı halkalarında olabiliyor ve halkalar birbirini maskeliyor:

    EKAP → sync_recent (liste) → ekap_oncelik kuyruğu → sync_detail → ilan_tarihi
                                                                          ↓
                                                            bildirim görevleri

`SyncRun` **`ok`** görünürken bile kullanıcı yeni ihale göremeyebilir: liste
senkronu ihaleyi DB'ye yazar ama **`ilan_tarihi` yalnızca DETAY senkronunda dolar**
(EKAP liste yanıtında bu alan %100 boştur). Bildirim görevlerinin tamamı
"`ilan_tarihi` BUGÜN" üzerinden çalıştığı için, detay kuyruğu erimiyorsa
bildirimler **boş küme** üzerinde koşar ve hiçbir hata üretmez.

Bu komut halkaları ayrı ayrı gösterir, böylece "hangi halka koptu" tahminle değil
ölçümle bulunur.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Max
from django.utils import timezone


def _yas(now, an):
    """İnsan okunur yaş. Negatif fark (gelecekteki zaman damgası) "az önce" olur."""
    if not an:
        return "HİÇ"
    sn = (now - an).total_seconds()
    if sn < 60:
        return "az önce"
    if sn < 3600:
        return f"{int(sn // 60)} dk önce"
    return f"{int(sn // 3600)} saat önce"


class Command(BaseCommand):
    help = "EKAP toplama + bildirim zincirinin sağlık raporu."

    def add_arguments(self, parser):
        parser.add_argument("--gun", type=int, default=3, help="geriye kaç gün özetlensin")

    def _baslik(self, s):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n── {s} " + "─" * (58 - len(s))))

    def _mobil_bolumu(self, now):
        """
        ⚠️ Mobil hattın teşhisi `SyncRun`'a bakarak YAPILMAZ: çekmeli tik görevi tur
        başına satır yazmıyor (günde ~720 boş kayıt olurdu). Gerçek kanıt, bugün
        yapılan iş sayaçları ve `Tender.detay_kaynak='mobil'` satırlarıdır.
        """
        from django.conf import settings

        from ekap.mobil import captcha as mobil_captcha
        from ekap.mobil import tasks as mobil_tasks
        from ekap.mobil import throttle as mobil_throttle
        from ekap.models import SyncCheckpoint, Tender
        from ekap.utils import local_day_range

        self._baslik("EKAP MOBİL (birincil kaynak)")
        acik = getattr(settings, "EKAP_MOBIL_ENABLED", False)
        self.stdout.write(
            ("  ✅ AÇIK" if acik else "  ⚠️ KAPALI (EKAP_MOBIL_ENABLED=False)")
            + f" · pencere {settings.EKAP_MOBIL_MIN_INTERVAL_MS / 1000:.0f} sn"
        )

        sayac = mobil_tasks.sayaclar()
        self.stdout.write(
            "  bugün: keşif={kesif} detay={detay} sonuç={sonuc} "
            "tazeleme={tazeleme} hata={hata}".format(**sayac)
        )
        for ad, v in mobil_throttle.butce_ozet().items():
            self.stdout.write(f"  bütçe {ad:10s} {v['kullanilan']}/{v['tavan']}")

        cp = SyncCheckpoint.objects.filter(name=mobil_tasks.CHECKPOINT).first()
        extra = (cp.extra if cp and isinstance(cp.extra, dict) else {}) or {}
        self.stdout.write(
            f"  keşif: bekleyen dilim={len(extra.get('yigin') or [])} "
            f"son tur={extra.get('son_tur') or '—'}"
        )

        bas, bit = local_day_range(timezone.localdate())
        self.stdout.write(
            "  bugün mobil kaynaklı detay: "
            f"{Tender.objects.filter(detay_kaynak='mobil', detail_synced_at__gte=bas, detail_synced_at__lt=bit).count()}"
        )

        # ⚠️ CAPTCHA en sık sessiz durma sebebi: geri çekilme varken hiçbir istek
        # atılmaz ve `SyncRun`'da iz kalmaz.
        if mobil_captcha.bekliyor_mu():
            self.stdout.write(self.style.ERROR(
                "  ❌ CAPTCHA GERİ ÇEKİLMESİ AKTİF — toplama duraklattı. "
                "Admin → EKAP Captcha ya da `manage.py mobil_captcha --cevap XXXXXX`"
            ))
        elif mobil_captcha.bekleyen_oku():
            self.stdout.write(self.style.WARNING(
                "  ⚠️ Operatör cevabı bekleyen captcha var."
            ))
        else:
            self.stdout.write("  captcha: bekleyen yok")
        if mobil_captcha.bekleyen_durum():
            self.stdout.write(f"  captcha durumu: {mobil_captcha.bekleyen_durum()[:120]}")

    def handle(self, *args, **o):
        from ekap.models import SyncRun, Tender
        from ekap.utils import local_day_range

        now = timezone.now()
        bugun_bas, bugun_bit = local_day_range(timezone.localdate())

        # 0) EKAP MOBİL — birincil kaynak. Bu durursa yeni ihale gelmez.
        self._mobil_bolumu(now)

        # 0b) v2 insan doğrulaması — artık YEDEK yolun ön koşulu (birincil mobildir).
        self._baslik("EKAP v2 insan doğrulaması (yedek yol)")
        from ekap import session as ekap_session
        self.stdout.write(f"  çerez: {ekap_session.maskele(ekap_session.cerez())}")
        try:
            d = ekap_session.durum()
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  durum sorgulanamadı: {e}"))
        else:
            if d.get("http") != 200:
                self.stdout.write(self.style.ERROR(
                    f"  HTTP {d.get('http')} — {d.get('hata', '')}"))
            elif d.get("verified"):
                self.stdout.write(self.style.SUCCESS(
                    f"  ✅ GEÇERLİ — bitiş {d.get('expiresAtUtc')} "
                    f"(yenileme {d.get('refreshAtUtc')})"))
            else:
                # ⚠️ Bu artık toplamayı DURDURMAZ (birincil kaynak mobil). Yalnızca
                # v2'nin verebildiği alanlar (idare_id, DETSIS ağacı, OKAS kataloğu,
                # düzeltme/iptal ilanları) tazelenemez.
                self.stdout.write(self.style.WARNING(
                    "  ⚠️ Doğrulama yok → YEDEK yol kapalı (idare_id / DETSIS / OKAS "
                    "kataloğu tazelenmez). Çözüm: tarayıcıda doğrulayıp "
                    "`manage.py ekap_dogrula --cookie …`"))

        # 1) Son çalışmalar — görev başına en son kayıt
        self._baslik("Son senkron çalışmaları")
        for task in ("sync_recent", "backfill", "refresh_stale", "sync_contractors"):
            run = SyncRun.objects.filter(task=task).order_by("-started_at").first()
            if not run:
                self.stdout.write(f"  {task:20s} — hiç çalışmamış")
                continue
            sure = (run.finished_at - run.started_at) if run.finished_at else None
            self.stdout.write(
                f"  {task:20s} {run.status:6s} items={run.items or 0:<6d} "
                f"errors={run.errors or 0:<4d} {_yas(now, run.started_at)}"
                + (f" ({int(sure.total_seconds())} sn)" if sure else " (BİTMEDİ)")
            )
            if run.note:
                self.stdout.write(f"  {'':20s} not: {run.note[:150]}")

        # 2) Asıl soru: ihale DB'ye girdi mi, DETAYI geldi mi?
        # ⚠️ İkisi ayrı halka — liste senkronu `ok` iken detay borcu birikebilir.
        self._baslik("İhale akışı (liste → detay → ilan_tarihi)")
        for gun in range(o["gun"]):
            g = timezone.localdate() - timedelta(days=gun)
            bas, bit = local_day_range(g)
            eklenen = Tender.objects.filter(created_at__gte=bas, created_at__lt=bit)
            ilanli = Tender.objects.filter(ilan_tarihi__gte=bas, ilan_tarihi__lt=bit)
            detaysiz = eklenen.filter(detail_synced_at__isnull=True).count()
            self.stdout.write(
                f"  {g}  DB'ye eklenen={eklenen.count():<6d} "
                f"(detayı eksik={detaysiz:<6d})   ilan_tarihi bu güne ait={ilanli.count()}"
            )
        self.stdout.write(
            f"  en yeni ilan_tarihi : {Tender.objects.aggregate(m=Max('ilan_tarihi'))['m']}"
        )
        self.stdout.write(
            f"  detayı hiç gelmemiş : {Tender.objects.filter(detail_synced_at__isnull=True).count()}"
        )

        # 3) Kuyruklar — detay borcu eriyor mu?
        # ⚠️ Broker DB **1**'dir (CELERY_BROKER_URL .../1), cache DB 0. Varsayılan
        # DB'ye bakan bir ölçüm daima 0 döner ve "kuyruk boş" yanılgısı üretir.
        self._baslik("Celery kuyrukları (broker DB)")
        try:
            import redis
            from django.conf import settings

            r = redis.Redis.from_url(settings.CELERY_BROKER_URL)
            for q in ("ekap_oncelik", "ekap", "celery"):
                self.stdout.write(f"  {q:15s} {r.llen(q)}")
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  kuyruk okunamadı: {e}"))

        # 4) Beat gerçekten tetikledi mi? (kuyrukta beklemek ≠ tetiklenmemek)
        self._baslik("Beat görevleri (last_run_at)")
        try:
            from django_celery_beat.models import PeriodicTask

            for pt in PeriodicTask.objects.filter(enabled=True).order_by("name"):
                if not any(k in pt.task for k in ("ekap", "tenders", "assistant")):
                    continue
                # ⚠️ `last_run_at` gelecekte olabilir (beat yeniden başladığında ilk
                # zamanlamayı ileriye yazar) → çıplak çıkarma "-1 saat önce" basardı.
                yas = _yas(now, pt.last_run_at)
                self.stdout.write(f"  {pt.name:38s} {yas}")
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  okunamadı: {e}"))

        # 5) Bildirim üretildi mi?
        self._baslik("Bugün üretilen bildirimler")
        try:
            from tenders.models import Notification

            rows = (Notification.objects.filter(created_at__gte=bugun_bas)
                    .values("type").annotate(n=Count("id")).order_by("-n"))
            if not rows:
                self.stdout.write("  (bugün hiç bildirim üretilmemiş)")
            for row in rows:
                self.stdout.write(f"  {row['type']:12s} {row['n']}")
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  okunamadı: {e}"))

        # 6) Bildirim görevlerinin baktığı küme — abonelikler kaç kişide açık?
        self._baslik("Alarmlı abonelikler (Pro filtresinden ÖNCE)")
        try:
            from tenders.models import FavoriteAuthority, SavedFilter
            from tenders.tasks import _alarm_enabled

            # ⚠️ `SavedFilter.alarm` JSONField'dir (bool DEĞİL) — `filter(alarm=True)`
            # dict biçimindeki kayıtları kaçırır. Görevin kendi yardımcısı kullanılır.
            alarmli = sum(1 for a in SavedFilter.objects.values_list("alarm", flat=True)
                          if _alarm_enabled(a))
            self.stdout.write(f"  alarmlı kayıtlı filtre : {alarmli}")
            self.stdout.write(
                f"  alarmlı favori idare   : {FavoriteAuthority.objects.filter(alarm=True).count()}"
            )
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  okunamadı: {e}"))
        self.stdout.write("")
