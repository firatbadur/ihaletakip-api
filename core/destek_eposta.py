"""
Destek yazışmasının e-postaları — iki yön, tek modül.

1. **Ekibe** (`ekibe_gonder`): kullanıcı yeni talep açınca ya da talebine yazınca
   destek ekibine haber. Alıcılar `alicilar()`: admin → Uygulama Ayarları →
   `destek_bildirim_epostalari` (virgüllü); satır yoksa `SUPPORT_NOTIFY_EMAILS` (env).
   ⚠️ Satır VARSA değeri bağlayıcıdır — boş değer "kapalı" demektir (env'e düşmez),
   böylece bildirim admin'den deploy'suz kapatılabilir.
2. **Kullanıcıya** (`kullaniciya_gonder`): destek ekibi yanıt verince yanıtın tam metni.
   ⚠️ İşlemsel (transactional) bir e-postadır: kullanıcı soru sordu, cevabı bekliyor →
   `email_notifications` (günlük bülten tercihi) ona UYGULANMAZ; doğrulama kodu ile aynı
   sınıf. Engelli adres (bounce/şikâyet) yine gönderim katmanında elenir.

Gönderimler Celery'de (`core.tasks`): SMTP 15 sn bekleyebilir, admin'in "Kaydet"i ya da
kullanıcının isteği bunu beklememeli. Hata yutulmaz → görev yeniden dener.
⚠️ Kullanıcı e-postası mesaj başına BİR KEZ (`cache.add` kilidi); gönderim düşerse kilit
silinir ki yeniden deneme gerçekten gönderebilsin (bülten kilidiyle aynı desen).
⚠️ Ekip e-postasında Reply-To YOK: yanıt panelden yazılmalı ki yazışmaya girsin ve
kullanıcıya bildirim/push gitsin; e-postayla yanıt vermek yazışmanın dışında kalırdı.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

AYAR_ANAHTARI = "destek_bildirim_epostalari"
GECMIS_MESAJ = 5          # ekip e-postasında gösterilen önceki mesaj sayısı
_KILIT_TTL = 7 * 24 * 3600
_META_ETIKET = (
    ("platform", "Platform"), ("app_version", "Uygulama sürümü"), ("build", "Derleme"),
    ("os_version", "İşletim sistemi"), ("device_model", "Cihaz"),
)


def _ayir(metin: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\s]+", metin or "") if p.strip()]


def alicilar() -> list[str]:
    """Destek ekibinin adresleri (geçersiz adresler loglanıp atılır)."""
    from core.models import AppSetting

    try:
        satir = AppSetting.objects.filter(key=AYAR_ANAHTARI).values_list("value", flat=True).first()
    except Exception:  # noqa: BLE001 — DB hatası bildirimi düşürmesin, env'e düş
        logger.exception("destek alıcı ayarı okunamadı")
        satir = None
    adaylar = _ayir(satir) if satir is not None else [
        a for a in getattr(settings, "SUPPORT_NOTIFY_EMAILS", []) if a]
    gecerli = []
    for adres in adaylar:
        try:
            validate_email(adres)
        except ValidationError:
            logger.warning("destek alıcı adresi geçersiz, atlandı: %r", adres)
            continue
        if adres.lower() not in {g.lower() for g in gecerli}:
            gecerli.append(adres)
    return gecerli


def _yerel(dt) -> str:
    return timezone.localtime(dt).strftime("%d.%m.%Y %H:%M") if dt else ""


def _kullanici_bilgisi(ticket) -> list[tuple[str, str]]:
    u = ticket.user
    satirlar = []
    if u is not None:
        ad = (f"{u.first_name} {u.last_name}".strip() or u.display_name or u.username)
        satirlar.append(("Kullanıcı", f"{ad} (#{u.pk})"))
        uyelik = "Pro" if u.is_premium else "Free"
        if u.is_premium and u.subscription_expires_at:
            uyelik += f" · bitiş {_yerel(u.subscription_expires_at)}"
        satirlar.append(("Üyelik", uyelik))
        satirlar.append(("Kayıt", _yerel(u.date_joined)))
    else:
        satirlar.append(("Kullanıcı", "Anonim"))
    satirlar.append(("E-posta", ticket.email or (u.email if u else "") or "—"))
    if ticket.phone:
        satirlar.append(("Telefon", ticket.phone))
    meta = ticket.meta or {}
    for anahtar, etiket in _META_ETIKET:
        if meta.get(anahtar):
            satirlar.append((etiket, str(meta[anahtar])))
    return satirlar


def ekip_epostasi(ticket, body: str, *, yeni: bool, gecmis=None, to=None,
                  connection=None) -> EmailMultiAlternatives:
    """`gecmis` verilmezse yazışmanın önceki mesajları DB'den okunur (yanıt e-postasında)."""
    from core.eposta import marka_baglami
    from core.support import onizleme

    kategori = ticket.get_category_display()
    tur = "Yeni destek talebi" if yeni else "Kullanıcı yanıtı"
    konu = f"[Destek #{ticket.pk}] {tur} · {kategori} — {onizleme(body, 60)}"
    if gecmis is None:
        gecmis = []
        if not yeni and ticket.pk:
            onceki = list(ticket.messages.order_by("-created_at", "-pk")[1:GECMIS_MESAJ + 1])
            gecmis = [{"kim": "Destek ekibi" if m.sender == "staff" else "Kullanıcı",
                       "zaman": _yerel(m.created_at), "metin": m.body}
                      for m in reversed(onceki)]
    panel = settings.PUBLIC_BASE_URL.rstrip("/")
    b = {
        **marka_baglami(),
        "belge_basligi": konu,
        "baslik_ust": f"Destek · {tur}",
        "tarih": _yerel(timezone.now()),
        "baslik": f"#{ticket.pk} · {kategori}",
        "alt_baslik": ticket.email or "Anonim kullanıcı",
        "on_metin": onizleme(body),
        "bilgiler": _kullanici_bilgisi(ticket) + [("Durum", ticket.status)],
        "mesaj": body,
        "gecmis": gecmis,
        "panel_url": f"{panel}/admin/core/supportticket/{ticket.pk}/change/",
        "neden": "destek ekibi bildirim listesinde olduğunuz için",
    }
    html = render_to_string("eposta/destek_ekip.html", b)
    duz = "\n".join(
        [f"{tur} — #{ticket.pk} · {kategori}", ""]
        + [f"{e}: {d}" for e, d in b["bilgiler"]]
        + ["", "Mesaj:", body]
        + ([""] + ["Önceki mesajlar:"] + [f"[{g['zaman']}] {g['kim']}: {g['metin']}"
                                          for g in gecmis] if gecmis else [])
        + ["", f"Yanıtla (admin): {b['panel_url']}"]
    )
    mesaj = EmailMultiAlternatives(
        subject=konu, body=duz, from_email=settings.DEFAULT_FROM_EMAIL,
        to=to or alicilar(), connection=connection,
    )
    mesaj.attach_alternative(html, "text/html")
    return mesaj


def ekibe_gonder(ticket_id: int, body: str, yeni: bool) -> str:
    from core.models import SupportTicket

    to = alicilar()
    if not to:
        return "alici_yok"
    ticket = SupportTicket.objects.select_related("user").filter(pk=ticket_id).first()
    if ticket is None:
        return "talep_yok"
    gonderilen = ekip_epostasi(ticket, body, yeni=yeni, to=to).send(fail_silently=False)
    logger.info("destek ekibine e-posta: ticket=%s yeni=%s alici=%s gonderilen=%s",
                ticket_id, yeni, len(to), gonderilen)
    return "gonderildi" if gonderilen else "engelli"


# ── Kullanıcıya: destek yanıtı ─────────────────────────────────────────────
def kullanici_adresi(ticket) -> str:
    """Talepteki iletişim adresi (eski istemci ayrı adres yazabiliyordu), yoksa hesabınki."""
    return (ticket.email or (ticket.user.email if ticket.user else "") or "").strip()


def yanit_epostasi(mesaj, *, to: str, connection=None) -> EmailMultiAlternatives:
    from core.eposta import marka_baglami
    from core.support import onizleme

    ticket = mesaj.ticket
    konu = f"Destek talebiniz yanıtlandı (#{ticket.pk})"
    ilk = ticket.message or ""
    b = {
        **marka_baglami(),
        "belge_basligi": konu,
        "baslik_ust": "Yardım & Destek",
        "tarih": _yerel(mesaj.created_at),
        "baslik": "Destek ekibimiz yanıtladı",
        "alt_baslik": f"Talep #{ticket.pk} · {ticket.get_category_display()}",
        "on_metin": onizleme(mesaj.body),
        "yanit": mesaj.body,
        "talep_ozeti": onizleme(ilk, 200),
        "kapandi": ticket.status == "Kapandı",
        "email": to,
        "neden": "İhale Takip'te açtığınız destek talebi yanıtlandığı için",
    }
    html = render_to_string("eposta/destek_yanit.html", b)
    duz = "\n\n".join([
        f"Destek ekibimiz talebinizi yanıtladı (#{ticket.pk} · {ticket.get_category_display()}).",
        mesaj.body,
        f"Talebiniz: {b['talep_ozeti']}",
        "Yanıt vermek için İhale Takip uygulamasındaki Yardım & Destek bölümünü kullanın; "
        "yazışmanın tamamı orada. Bu e-postaya verilen yanıtlar bize ulaşmaz.",
    ])
    e = EmailMultiAlternatives(
        subject=konu, body=duz, from_email=settings.DEFAULT_FROM_EMAIL, to=[to],
        connection=connection,
    )
    e.attach_alternative(html, "text/html")
    return e


def kullaniciya_gonder(mesaj_id: int) -> str:
    """Destek yanıtını kullanıcıya bir kez e-postalar; dönüş durum metni."""
    from core.models import SupportMessage

    mesaj = (SupportMessage.objects.select_related("ticket", "ticket__user")
             .filter(pk=mesaj_id).first())
    if mesaj is None:
        return "mesaj_yok"
    if mesaj.sender != SupportMessage.SENDER_STAFF:
        return "destek_yaniti_degil"
    to = kullanici_adresi(mesaj.ticket)
    if not to:
        return "adres_yok"
    kilit = f"destek:yanit_eposta:{mesaj.pk}"
    if not cache.add(kilit, 1, _KILIT_TTL):
        return "zaten_gonderildi"
    try:
        gonderilen = yanit_epostasi(mesaj, to=to).send(fail_silently=False)
    except Exception:
        cache.delete(kilit)   # yeniden deneme gerçekten gönderebilsin
        raise
    logger.info("destek yanıtı e-postası: ticket=%s mesaj=%s gonderilen=%s",
                mesaj.ticket_id, mesaj.pk, gonderilen)
    return "gonderildi" if gonderilen else "engelli"
