"""
Toplu e-posta / push bildirimi — admin → Toplu Gönderim (`core/admin_toplu.py`).

**Akış**: admin gönderir → `olustur()` alıcıları `TopluGonderimAlici` satırlarına döker
(kim alacağı o an DONDURULUR; gönderim sırasında segmente giren/çıkan etkilemez) →
`core.tasks.toplu_gonderim_isle` alıcıları `PARTI`lik gruplarla işler.

**Kuyruk / sıkışma**: görev tek seferde en çok `TOPLU_TUR_SN` saniye çalışır, sonra
kendini yeniden kuyruğa atar → `celery` kuyruğundaki diğer işler (bildirim görevleri,
asistan, destek e-postası) araya girer, hiçbir şey saatlerce beklemez. Aynı gönderimi iki
worker birlikte işlemez (Redis kilidi). Worker çökerse alıcılar `bekliyor` kalır ve
"Devam ettir" kaldığı yerden sürer; `(gonderim, user)` tekil olduğu için kimseye iki kez
gitmez.

**Hız**: e-posta SES hız sınırının (14/sn) altında tutulur (`TOPLU_EPOSTA_SANIYE`).

**Kim ATLANIR** (sebep satıra yazılır, sayılar ekranda görünür):
- e-posta: adres boş · engelli adres (`EpostaEngeli`) · e-posta bildirimleri kapalı
  (`User.email_notifications=False` — abonelikten çıkan kişiye toplu e-posta gitmez);
- push: kayıtlı cihaz yok · push tercihi kapalı · sessiz saat / günlük limit
  (`sessiz_saat_yok` seçilmediyse).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import linebreaks, strip_tags, urlize

from .models import TopluGonderim, TopluGonderimAlici

logger = logging.getLogger("ihaletakip")

PARTI = 25
SEGMENTLER = {
    "hepsi": "Tüm aktif kullanıcılar",
    "pro": "Pro üyeler",
    "free": "Free üyeler",
    "aktif30": "Son 30 günde aktif olanlar",
    "secili": "Seçili kullanıcılar",
}
AZAMI_SECILI = 5000


def _tur_sn() -> int:
    return int(getattr(settings, "TOPLU_TUR_SN", 200))


def _eposta_hizi() -> float:
    return float(getattr(settings, "TOPLU_EPOSTA_SANIYE", 8))


# ── Hedef ────────────────────────────────────────────────────────────────────
def hedef_qs(hedef: dict):
    from core.dashboard import pro_q

    User = get_user_model()
    qs = User.objects.filter(is_active=True)
    seg = (hedef or {}).get("segment")
    if seg == "secili":
        ids = [int(x) for x in (hedef.get("user_ids") or []) if str(x).isdigit()]
        return qs.filter(pk__in=ids[:AZAMI_SECILI])
    if seg == "pro":
        return qs.filter(pro_q())
    if seg == "free":
        return qs.exclude(pro_q())
    if seg == "aktif30":
        return qs.filter(last_seen_at__gte=timezone.now() - timedelta(days=30))
    if seg == "hepsi":
        return qs
    return qs.none()


def hedef_ozet(hedef: dict) -> str:
    seg = (hedef or {}).get("segment")
    if seg == "secili":
        return f"Seçili {len(hedef.get('user_ids') or [])} kullanıcı"
    return SEGMENTLER.get(seg, "—")


def sayim(hedef: dict) -> dict:
    """Ekranın "kaç kişiye gidecek" kutusu: toplam ve kanal başına ulaşılabilir sayı."""
    from accounts.models import EpostaEngeli
    from django.db.models import Exists, OuterRef
    from django.db.models.functions import Lower

    qs = hedef_qs(hedef)
    engelli = Exists(EpostaEngeli.objects.filter(aktif=True, email=Lower(OuterRef("email"))))
    return {
        "toplam": qs.count(),
        "eposta": qs.exclude(email="").filter(email_notifications=True)
                    .exclude(engelli).count(),
        "bildirim": qs.filter(push_devices__isnull=False).distinct().count(),
    }


# ── İçerik ───────────────────────────────────────────────────────────────────
def _ad(user) -> str:
    return ((getattr(user, "first_name", "") or getattr(user, "display_name", "") or "")
            .strip().split(" ")[0] if user else "")


def kisisellestir(metin: str, user) -> str:
    """`{ad}` → kullanıcının adı (yoksa "Değerli kullanıcımız")."""
    return (metin or "").replace("{ad}", _ad(user) or "Değerli kullanıcımız")


def eposta_icerik_html(g, user=None) -> str:
    metin = kisisellestir(g.icerik, user)
    if g.icerik_turu == TopluGonderim.IcerikTuru.HTML:
        return metin
    # Düz metin → paragraflar + tıklanabilir bağlantılar (kaçışlı).
    return urlize(linebreaks(metin, autoescape=True), autoescape=False)


def eposta_olustur(g, user, *, tema=None, connection=None):
    """Kullanıcıya özel (adı, abonelikten çıkma bağlantısı) e-posta mesajı."""
    from django.core.mail import EmailMultiAlternatives

    from core.eposta import marka_baglami
    from tenders.services.eposta_bulten import abonelik_url

    konu = kisisellestir(g.konu, user)
    icerik = eposta_icerik_html(g, user)
    blog = g.kategori == TopluGonderim.Kategori.BLOG
    abonelik = abonelik_url(user, "blog" if blog else "") if user and user.pk else ""
    baglam = {
        **marka_baglami(tema or g.tema or None),
        "belge_basligi": konu,
        "baslik_ust": "İhale Takip Blog" if blog else "İhale Takip",
        "baslik": kisisellestir(g.eposta_baslik or g.konu, user),
        "on_metin": kisisellestir(g.on_metin, user) or strip_tags(icerik)[:140],
        "icerik": icerik,
        "dugme_metni": g.dugme_metni,
        "dugme_url": g.dugme_url,
        "email": getattr(user, "email", ""),
        "abonelik": abonelik,
        "ayarlar": f"{settings.EPOSTA_LINK_TABANI.rstrip('/')}/hesabim",
        "neden": "İhale Takip hesabınız olduğu için",
        "abonelik_metni": "Blog e-postalarından çık" if blog else "",
    }
    html = render_to_string("eposta/toplu.html", baglam)
    duz = strip_tags(icerik).strip()
    if g.dugme_metni and g.dugme_url:
        duz += f"\n\n{g.dugme_metni}: {g.dugme_url}"
    duz += "\n\n— İhale Takip\n" + (f"E-postalardan çıkmak için: {abonelik}\n" if abonelik else "")
    basliklar = {}
    if abonelik:
        basliklar = {"List-Unsubscribe": f"<{abonelik}>",
                     "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    mesaj = EmailMultiAlternatives(
        subject=konu[:180], body=duz, from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email], connection=connection, headers=basliklar)
    mesaj.attach_alternative(html, "text/html")
    return mesaj, html


# ── Gönderim ─────────────────────────────────────────────────────────────────
def olustur(veri: dict, olusturan) -> TopluGonderim:
    """Gönderimi ve alıcı satırlarını yazar, işlemeyi transaction bitince kuyruğa atar."""
    from core.eposta import aktif_tema

    with transaction.atomic():
        g = TopluGonderim.objects.create(
            kanal=veri["kanal"], kategori=veri.get("kategori") or TopluGonderim.Kategori.GENEL,
            konu=veri["konu"], eposta_baslik=veri.get("eposta_baslik", ""),
            on_metin=veri.get("on_metin", ""), icerik_turu=veri.get("icerik_turu", "metin"),
            icerik=veri["icerik"], dugme_metni=veri.get("dugme_metni", ""),
            dugme_url=veri.get("dugme_url", ""), uygulama_ici=veri.get("uygulama_ici", True),
            sessiz_saat_yok=veri.get("sessiz_saat_yok", False), hedef=veri["hedef"],
            hedef_ozet=hedef_ozet(veri["hedef"]), olusturan=olusturan,
            tema=aktif_tema() if veri["kanal"] == TopluGonderim.Kanal.EPOSTA else "")
        ids = list(hedef_qs(veri["hedef"]).order_by("pk").values_list("pk", flat=True))
        TopluGonderimAlici.objects.bulk_create(
            [TopluGonderimAlici(gonderim=g, user_id=u) for u in ids],
            batch_size=1000, ignore_conflicts=True)
        g.toplam = len(ids)
        g.save(update_fields=["toplam"])
        transaction.on_commit(lambda: kuyruga_al(g.pk))
    logger.info("toplu gönderim oluşturuldu: #%s %s → %s alıcı", g.pk, g.kanal, g.toplam)
    return g


def kuyruga_al(gonderim_id: int, gecikme: int = 0) -> None:
    from core.tasks import toplu_gonderim_isle

    toplu_gonderim_isle.apply_async((gonderim_id,), countdown=gecikme)


def _eposta_gonder(g, user, connection, engelli) -> tuple[str, str]:
    from accounts.services.eposta_engel import normalize

    if not user.email:
        return TopluGonderimAlici.Durum.ATLANDI, "e-posta adresi yok"
    if normalize(user.email) in engelli:
        return TopluGonderimAlici.Durum.ATLANDI, "engelli adres (geri dönüş/şikâyet)"
    if not user.email_notifications:
        return TopluGonderimAlici.Durum.ATLANDI, "e-posta bildirimleri kapalı"
    if g.kategori == TopluGonderim.Kategori.BLOG and not user.blog_eposta:
        return TopluGonderimAlici.Durum.ATLANDI, "blog e-postaları kapalı"
    mesaj, _ = eposta_olustur(g, user, connection=connection)
    if not mesaj.send():
        return TopluGonderimAlici.Durum.ATLANDI, "gönderim katmanı reddetti"
    return TopluGonderimAlici.Durum.GONDERILDI, ""


def _push_gonder(g, user) -> tuple[str, str]:
    from tenders.models import Notification
    from tenders.services.notify import _push_pref_enabled, push_to_user, record_notification

    baslik, govde = kisisellestir(g.konu, user), kisisellestir(g.icerik, user)
    if g.uygulama_ici:
        record_notification(user, type=Notification.Type.INFO, title=baslik[:255], body=govde)
    if not user.push_devices.exists():
        return TopluGonderimAlici.Durum.ATLANDI, "kayıtlı cihaz yok"
    if not _push_pref_enabled(user):
        return TopluGonderimAlici.Durum.ATLANDI, "push bildirimleri kapalı"
    ok = push_to_user(user, title=baslik, body=govde,
                      data={"type": "info", "toplu": str(g.pk)},
                      idem_key=f"toplu:{g.pk}:{user.pk}", transactional=g.sessiz_saat_yok)
    if not ok:
        return TopluGonderimAlici.Durum.ATLANDI, "gönderilemedi (sessiz saat / limit / cihaz)"
    return TopluGonderimAlici.Durum.GONDERILDI, ""


def sayaclari_yaz(g) -> None:
    sayi = dict(g.alicilar.values_list("durum").annotate(n=Count("pk")))
    g.gonderilen = sayi.get(TopluGonderimAlici.Durum.GONDERILDI, 0)
    g.atlanan = sayi.get(TopluGonderimAlici.Durum.ATLANDI, 0)
    g.hatali = sayi.get(TopluGonderimAlici.Durum.HATALI, 0)
    g.save(update_fields=["gonderilen", "atlanan", "hatali", "son_hata"])


def isle(gonderim_id: int) -> dict:
    """Bir tur işler; iş kaldıysa kendini yeniden kuyruğa atar."""
    from django.core.mail import get_connection

    from accounts.services.eposta_engel import tum_engelliler

    kilit = f"toplu:kilit:{gonderim_id}"
    if not cache.add(kilit, 1, _tur_sn() + 120):
        return {"atlandi": "baska_tur_isliyor"}
    devam = False
    try:
        g = TopluGonderim.objects.filter(pk=gonderim_id).first()
        if not g or g.durum in (TopluGonderim.Durum.TAMAMLANDI, TopluGonderim.Durum.IPTAL):
            return {"atlandi": "bitmis"}
        if g.durum == TopluGonderim.Durum.KUYRUKTA:
            g.durum, g.baslangic = TopluGonderim.Durum.GONDERILIYOR, timezone.now()
            g.save(update_fields=["durum", "baslangic"])
        eposta = g.kanal == TopluGonderim.Kanal.EPOSTA
        engelli = tum_engelliler() if eposta else set()
        baglanti = get_connection() if eposta else None
        aralik = 1.0 / max(_eposta_hizi(), 0.1)
        bas = time.monotonic()
        try:
            while time.monotonic() - bas < _tur_sn():
                if TopluGonderim.objects.filter(pk=g.pk, durum=TopluGonderim.Durum.IPTAL).exists():
                    break
                parti = list(g.alicilar.filter(durum=TopluGonderimAlici.Durum.BEKLIYOR)
                             .select_related("user").order_by("pk")[:PARTI])
                if not parti:
                    break
                for a in parti:
                    t0 = time.monotonic()
                    try:
                        if eposta:
                            a.durum, a.sebep = _eposta_gonder(g, a.user, baglanti, engelli)
                        else:
                            a.durum, a.sebep = _push_gonder(g, a.user)
                    except Exception as e:  # noqa: BLE001 — tek alıcı turu düşürmesin
                        logger.exception("toplu gönderim #%s alıcı %s", g.pk, a.user_id)
                        a.durum, a.sebep = TopluGonderimAlici.Durum.HATALI, str(e)[:200]
                        g.son_hata = f"{a.user_id}: {e}"[:500]
                        if baglanti is not None:  # bozuk SMTP oturumuyla devam edilmesin
                            baglanti.close()
                            baglanti = get_connection()
                    a.islem_at = timezone.now()
                    if eposta and a.durum == TopluGonderimAlici.Durum.GONDERILDI:
                        kalan = aralik - (time.monotonic() - t0)
                        if kalan > 0:
                            time.sleep(kalan)
                TopluGonderimAlici.objects.bulk_update(parti, ["durum", "sebep", "islem_at"])
                sayaclari_yaz(g)
        finally:
            if baglanti is not None:
                baglanti.close()
        g.refresh_from_db()
        sayaclari_yaz(g)
        bekleyen = g.alicilar.filter(durum=TopluGonderimAlici.Durum.BEKLIYOR)
        if g.durum == TopluGonderim.Durum.IPTAL:
            bekleyen.update(durum=TopluGonderimAlici.Durum.ATLANDI, sebep="gönderim iptal edildi",
                            islem_at=timezone.now())
            sayaclari_yaz(g)
        elif bekleyen.exists():
            devam = True
        else:
            g.durum, g.bitis = TopluGonderim.Durum.TAMAMLANDI, timezone.now()
            g.save(update_fields=["durum", "bitis"])
            logger.info("toplu gönderim #%s bitti: %s gönderildi, %s atlandı, %s hatalı",
                        g.pk, g.gonderilen, g.atlanan, g.hatali)
        return {"gonderim": g.pk, "gonderilen": g.gonderilen, "atlanan": g.atlanan,
                "hatali": g.hatali, "devam": devam}
    finally:
        cache.delete(kilit)
        if devam:
            kuyruga_al(gonderim_id, gecikme=2)


def iptal_et(g) -> None:
    TopluGonderim.objects.filter(
        pk=g.pk, durum__in=[TopluGonderim.Durum.KUYRUKTA, TopluGonderim.Durum.GONDERILIYOR]
    ).update(durum=TopluGonderim.Durum.IPTAL, bitis=timezone.now())
    # İşleyen tur yoksa bekleyenleri hemen kapat (varsa tur kendisi kapatır).
    if cache.get(f"toplu:kilit:{g.pk}") is None:
        g.alicilar.filter(durum=TopluGonderimAlici.Durum.BEKLIYOR).update(
            durum=TopluGonderimAlici.Durum.ATLANDI, sebep="gönderim iptal edildi",
            islem_at=timezone.now())
        g.refresh_from_db()
        sayaclari_yaz(g)


def devam_ettir(g) -> None:
    """Yarım kalmış (worker düşmüş) gönderimi yeniden kuyruğa atar."""
    if g.durum in (TopluGonderim.Durum.KUYRUKTA, TopluGonderim.Durum.GONDERILIYOR):
        kuyruga_al(g.pk)


def ozet(g) -> dict:
    return {
        "id": g.pk, "kanal": g.kanal, "kanal_ad": g.get_kanal_display(), "konu": g.konu,
        "durum": g.durum, "durum_ad": g.get_durum_display(), "hedef": g.hedef_ozet,
        "toplam": g.toplam, "gonderilen": g.gonderilen, "atlanan": g.atlanan,
        "hatali": g.hatali, "islenen": g.islenen, "son_hata": g.son_hata,
        "olusturan": getattr(g.olusturan, "email", "") or getattr(g.olusturan, "username", ""),
        "olusturma": timezone.localtime(g.created_at).strftime("%d.%m.%Y %H:%M"),
        "bitis": timezone.localtime(g.bitis).strftime("%d.%m.%Y %H:%M") if g.bitis else "",
        "tema": g.tema,
    }


def atlanma_ozeti(g) -> list[dict]:
    """Atlanan/hatalı alıcıların sebep dağılımı (ekranda "neden gitmedi")."""
    return list(g.alicilar.filter(~Q(durum__in=[TopluGonderimAlici.Durum.GONDERILDI,
                                                 TopluGonderimAlici.Durum.BEKLIYOR]))
                .values("durum", "sebep").annotate(n=Count("pk")).order_by("-n")[:10])
