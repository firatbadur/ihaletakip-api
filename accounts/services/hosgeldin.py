"""
Hoş geldin e-postası — yeni kayıt olan kullanıcıya bir kez (`accounts.signals`).

Tetik `User` oluşturulmasıdır (e-posta kaydı, Google, Apple — hepsi aynı kapıdan);
gönderim Celery'de, kayıt isteğini bekletmez. `User.hosgeldin_eposta_at` tek seferi
garantiler (koşullu UPDATE: iki görev yarışırsa yalnızca biri gönderir).

**İçerik**: ilk adımlar (Free'de neler var) + Pro avantajları + fiyat. Fiyat ve deneme
süresi mağaza kaydından okunur (`AppSetting("magaza_fiyatlari")`, gece güncellenir) —
koda gömülmez; kayıt yoksa fiyat satırı hiç gösterilmez (uydurma fiyat yazılmaz).

⚠️ Pro maddeleri ürünün GERÇEK kapılarıyla aynı olmalı (bkz. CLAUDE.md "Abonelik /
Premium"); Free'de olan bir şeyi Pro diye anlatmak ya da tersi kullanıcıyı yanıltır.
⚠️ Kimler ALMAZ: e-postası boş, `email_notifications=False`, personel (staff/superuser —
admin komutları ve testler), engelli adres (gönderim katmanı).
"""
from __future__ import annotations

import json
import logging
import re

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger("ihaletakip")

KONU = "İhale Takip'e hoş geldiniz"

ADIMLAR = [
    ("Filtrenizi kaydedin",
     "Sektör, il ve idare seçip aramanızı kaydedin; uyan yeni ihaleler her sabah bildirim ve "
     "e-postayla gelsin."),
    ("İhaleleri kaydedin",
     "İlgilendiğiniz ihaleleri ve doğrudan temin alımlarını klasörlere ayırın, tek ekrandan izleyin."),
    ("Her yerden ulaşın",
     "Aynı hesapla mobil uygulamayı ve ihaletakip.com'u birlikte kullanın."),
]

PRO_AVANTAJLARI = [
    ("İhale alarmları", "Takip ettiğiniz ihalenin teklif günü gelince ve ihale sonuçlanınca anında haber alın."),
    ("İhale Asistanı", "Yapay zekâ her sabah firmanıza uygun ihaleleri seçsin, sorularınızı yanıtlasın."),
    ("Doküman analizi", "İdari ve teknik şartnameyi yapay zekâ dakikalar içinde özetlesin."),
    ("Fiyat analizi", "Benzer işlerin kazanan bedellerini ve indirim oranlarını görün, teklifinizi buna göre verin."),
    ("Rakip ve idare takibi", "Takip ettiğiniz firma yeni iş aldığında, favori idareniz ihale açtığında bildirim alın."),
    ("Beklenen ihaleler", "Her yıl tekrarlanan işleri ilan çıkmadan görün, hazırlığa erken başlayın."),
    ("Gelişmiş arama", "Tutar, rekabet ve indirim filtreleri; sınırsız sonuç sayfası."),
]


def _tl(deger) -> str:
    """`"2499.99"` → `"2.499,99 TL"`."""
    try:
        sayi = float(deger)
    except (TypeError, ValueError):
        return ""
    metin = f"{sayi:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{metin} TL"


def _sure(iso: str | None) -> str:
    m = re.fullmatch(r"P(\d+)([DWMY])", iso or "")
    if not m:
        return ""
    n, b = int(m.group(1)), m.group(2)
    return f"{n} {({'D': 'gün', 'W': 'hafta', 'M': 'ay', 'Y': 'yıl'})[b]}"


def fiyat_bilgisi() -> dict | None:
    """Mağaza kaydından aylık/yıllık fiyat ve deneme süresi; kayıt yoksa None."""
    from core.models import AppSetting

    try:
        kayit = json.loads(AppSetting.get("magaza_fiyatlari", "") or "{}")
    except (ValueError, TypeError):
        return None
    sonuc = {}
    for p in kayit.get("paketler") or []:
        magaza = (p.get("magazalar") or {})
        m = magaza.get("app_store") or magaza.get("play_store") or {}
        if not m.get("fiyat"):
            continue
        sonuc[p.get("kod")] = {"fiyat": _tl(m["fiyat"]), "ham": m["fiyat"],
                               "deneme": _sure(m.get("deneme"))}
    if "aylik" not in sonuc:
        return None
    yillik = sonuc.get("yillik")
    if yillik:
        try:
            yillik["aylik_karsilik"] = _tl(float(yillik["ham"]) / 12)
        except (TypeError, ValueError):
            pass
    return {"aylik": sonuc["aylik"], "yillik": yillik,
            "deneme": sonuc["aylik"]["deneme"] or (yillik or {}).get("deneme", "")}


def baglam(user, tema: str | None = None) -> dict:
    from core.eposta import marka_baglami
    from tenders.services.eposta_bulten import abonelik_url

    site = settings.EPOSTA_LINK_TABANI.rstrip("/")
    utm = "utm_source=eposta&utm_medium=hosgeldin"
    ad = ((user.first_name or user.display_name or "").strip().split(" ") or [""])[0]
    return {
        **marka_baglami(tema),
        "belge_basligi": KONU,
        "baslik_ust": "Hoş geldiniz",
        "baslik": f"Aramıza hoş geldiniz, {ad}!" if ad else "Aramıza hoş geldiniz!",
        "alt_baslik": "Türkiye'deki tüm kamu ihalelerini tek yerden takip etmeye hazırsınız.",
        "on_metin": "Hesabınız hazır. İlk adımlar ve Pro ile neler yapabileceğiniz bu e-postada.",
        "adimlar": ADIMLAR,
        "pro": PRO_AVANTAJLARI,
        "fiyat": fiyat_bilgisi(),
        "kesfet_url": f"{site}/ihaleler?{utm}",
        "pro_url": f"{site}/pro?{utm}",
        "email": user.email,
        "abonelik": abonelik_url(user),
        "ayarlar": f"{site}/hesabim",
        "neden": "İhale Takip'e kayıt olduğunuz için",
    }


def eposta_olustur(user, tema: str | None = None, connection=None) -> EmailMultiAlternatives:
    b = baglam(user, tema)
    html = render_to_string("eposta/hosgeldin.html", b)
    duz = "\n\n".join([
        b["baslik"], b["alt_baslik"],
        "İlk adımlar:\n" + "\n".join(f"- {a}: {m}" for a, m in ADIMLAR),
        "Pro ile:\n" + "\n".join(f"- {a}: {m}" for a, m in PRO_AVANTAJLARI),
        f"İhaleleri keşfedin: {b['kesfet_url']}\nPro'yu inceleyin: {b['pro_url']}",
        f"E-postalardan çıkmak için: {b['abonelik']}",
    ])
    mesaj = EmailMultiAlternatives(
        subject=KONU, body=strip_tags(duz), from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email], connection=connection,
        headers={"List-Unsubscribe": f"<{b['abonelik']}>",
                 "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
    mesaj.attach_alternative(html, "text/html")
    return mesaj


def gonder(user_id: int) -> str:
    """Bir kez gönderir; dönüş durum metni (görev sonucu / log için)."""
    from accounts.models import User

    user = User.objects.filter(pk=user_id).first()
    if user is None or not user.is_active:
        return "kullanici_yok"
    if user.is_staff or user.is_superuser:
        return "personel"
    if not user.email or not user.email_notifications:
        return "eposta_yok_ya_da_kapali"
    if user.hosgeldin_eposta_at:
        return "zaten_gonderildi"
    # Koşullu damga: aynı kullanıcı için iki görev yarışırsa yalnızca biri geçer.
    if not User.objects.filter(pk=user.pk, hosgeldin_eposta_at__isnull=True).update(
            hosgeldin_eposta_at=timezone.now()):
        return "zaten_gonderildi"
    try:
        eposta_olustur(user).send(fail_silently=False)
    except Exception:
        # Gönderim düştü → damgayı geri al, görev yeniden denesin.
        User.objects.filter(pk=user.pk).update(hosgeldin_eposta_at=None)
        raise
    logger.info("hoş geldin e-postası gönderildi: uid=%s", user.pk)
    return "gonderildi"
