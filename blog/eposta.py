"""
Günlük blog yazısı e-postası — beat her gün 12:00 (`blog.tasks.gunluk_blog_epostasi`).

Son `BLOG_EPOSTA_PENCERE_SAAT` (24) saatte yayımlanmış ve henüz e-postayla gönderilmemiş
EN YENİ yazı, tüm kullanıcılara toplu gönderim altyapısıyla (`core/toplu.py`, kategori
`blog`) gider: kuyruk, hız sınırı, engelli adres, ilerleme ve admin'deki Toplu Gönderim
ekranı aynıdır.

**Kimler alır**: aktif, e-postası olan, `email_notifications=True` VE `blog_eposta=True`.
Blog e-postasındaki abonelik bağlantısı yalnızca blogdan çıkarır (ihale özeti sürer).

⚠️ Pencere arşivin topluca gönderilmesini engeller: ilk açılışta ya da görev bir gün
koşmazsa eski yazılar e-postaya DÖNÜŞMEZ. Aynı gün birden çok yazı yayımlanırsa yalnızca
en yenisi gider (günde tek blog e-postası).
⚠️ `eposta_gonderildi_at` koşullu güncellenir: görev iki kez tetiklense bile yazı bir kez gider.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import BlogPost, mutlak_medya_url

logger = logging.getLogger("ihaletakip")


def yazi_url(post) -> str:
    taban = settings.EPOSTA_LINK_TABANI.rstrip("/")
    return f"{taban}/blog/{post.slug}?utm_source=eposta&utm_medium=blog&utm_campaign={post.slug}"


def icerik_html(post) -> str:
    return render_to_string("eposta/blog_icerik.html", {
        "post": post,
        "kapak": mutlak_medya_url(post.cover_image.name) if post.cover_image else None,
        "url": yazi_url(post),
        "kategori": post.category.name if post.category_id else "",
    })


def kampanya_verisi(post) -> dict:
    return {
        "kanal": "eposta", "kategori": "blog", "konu": post.title[:200],
        "eposta_baslik": post.title[:200], "on_metin": (post.summary or "")[:200],
        "icerik_turu": "html", "icerik": icerik_html(post),
        "dugme_metni": "Yazının devamını okuyun", "dugme_url": yazi_url(post),
        "hedef": {"segment": "hepsi"},
    }


def gonderilecek_yazi(simdi=None):
    simdi = simdi or timezone.now()
    pencere = timedelta(hours=getattr(settings, "BLOG_EPOSTA_PENCERE_SAAT", 24))
    return (BlogPost.objects.yayinda(simdi)
            .filter(eposta_gonderildi_at__isnull=True, published_at__gte=simdi - pencere)
            .select_related("category").order_by("-published_at").first())


def gonder(post, olusturan=None):
    """Yazıyı toplu gönderime çevirir; yazı zaten gönderildiyse None."""
    from core import toplu

    with transaction.atomic():
        if not BlogPost.objects.filter(pk=post.pk, eposta_gonderildi_at__isnull=True).update(
                eposta_gonderildi_at=timezone.now()):
            return None
        g = toplu.olustur(kampanya_verisi(post), olusturan)
    logger.info("blog e-postası kuyruğa alındı: %s → toplu #%s (%s alıcı)", post.slug, g.pk, g.toplam)
    return g


def gunluk() -> dict:
    if not getattr(settings, "BLOG_EPOSTA_ENABLED", True):
        return {"atlandi": "kapali"}
    # Günde tek blog e-postası: son 20 saatte bir yazı gittiyse (aynı gün ikinci tetik ya
    # da elle gönderim) bekleyen eski yazı da GİTMEZ — ertesi gün pencereden düşer.
    if BlogPost.objects.filter(eposta_gonderildi_at__gte=timezone.now() - timedelta(hours=20)).exists():
        return {"atlandi": "bugun_gonderildi"}
    post = gonderilecek_yazi()
    if post is None:
        return {"atlandi": "yeni_yazi_yok"}
    g = gonder(post)
    return {"yazi": post.slug, "gonderim": g.pk if g else None}
