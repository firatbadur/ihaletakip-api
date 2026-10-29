"""
Destek yazışması — talep açma, mesaj ekleme ve bildirimler TEK yerden.

API (`core.views`) ve admin (`core.admin`) aynı fonksiyonları çağırır; durum geçişi,
okunmamış bayrağı ve bildirim kuralı iki yerde ayrışmasın diye.

Durum geçişleri:
- kullanıcı yazdı      → "Bekliyor" (kapalıysa yeniden açılır)
- destek ekibi yazdı   → "Yanıtlandı" + `user_unread` + uygulama-içi bildirim + push
- kapat                → "Kapandı"

⚠️ Bildirim `transaction.on_commit` ile gider: admin kaydı geri alınırsa kullanıcıya
"yanıt geldi" denip boş bir yazışma açılmasın.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SupportMessage, SupportTicket

logger = logging.getLogger("ihaletakip")

MESAJ_UZUNLUK_SINIRI = 4000
_ONIZLEME = 140


def onizleme(metin: str, sinir: int = _ONIZLEME) -> str:
    tek_satir = " ".join((metin or "").split())
    return tek_satir if len(tek_satir) <= sinir else tek_satir[: sinir - 1].rstrip() + "…"


@transaction.atomic
def talep_ac(user, *, message: str, category: str = "diger", email: str = "",
             phone: str = "", meta: dict | None = None) -> SupportTicket:
    simdi = timezone.now()
    ticket = SupportTicket.objects.create(
        user=user,
        email=email or getattr(user, "email", "") or "",
        phone=phone or "",
        category=category or "diger",
        message=message,
        meta=meta or {},
        status="Bekliyor",
        last_message_at=simdi,
    )
    SupportMessage.objects.create(
        ticket=ticket, sender=SupportMessage.SENDER_USER, body=message, created_at=simdi
    )
    transaction.on_commit(lambda: _ekibe_haber_ver(ticket.pk, message, yeni=True))
    return ticket


@transaction.atomic
def kullanici_yaniti(ticket: SupportTicket, body: str) -> SupportMessage:
    simdi = timezone.now()
    mesaj = SupportMessage.objects.create(
        ticket=ticket, sender=SupportMessage.SENDER_USER, body=body, created_at=simdi
    )
    SupportTicket.objects.filter(pk=ticket.pk).update(
        status="Bekliyor", last_message_at=simdi, updated_at=simdi
    )
    ticket.refresh_from_db()
    transaction.on_commit(lambda: _ekibe_haber_ver(ticket.pk, body, yeni=False))
    return mesaj


@transaction.atomic
def destek_yaniti(ticket: SupportTicket, body: str, *, author=None,
                  status: str = "Yanıtlandı") -> SupportMessage:
    """Destek ekibinin yanıtı. `status` admin'in seçtiği son durum (ör. Kapandı)."""
    simdi = timezone.now()
    mesaj = SupportMessage.objects.create(
        ticket=ticket, sender=SupportMessage.SENDER_STAFF, author=author, body=body,
        created_at=simdi,
    )
    SupportTicket.objects.filter(pk=ticket.pk).update(
        status=status, last_message_at=simdi, user_unread=True, updated_at=simdi
    )
    ticket.refresh_from_db()
    transaction.on_commit(lambda: kullaniciya_bildir(mesaj))
    return mesaj


def kapat(ticket: SupportTicket) -> None:
    SupportTicket.objects.filter(pk=ticket.pk).update(status="Kapandı", updated_at=timezone.now())


def kullaniciya_bildir(mesaj: SupportMessage) -> bool:
    """Destek yanıtını uygulama-içi bildirim + push olarak iletir. Push sonucu döner.

    ⚠️ Asla istisna fırlatmaz: admin'in "Kaydet"i bildirim altyapısı yüzünden 500
    vermemeli — yanıt zaten kaydedildi, kullanıcı uygulamada görür.
    """
    from tenders.services.notify import push_to_user, record_notification

    ticket = mesaj.ticket
    user = ticket.user
    if user is None:
        return False
    baslik = "Destek ekibi yanıtladı"
    govde = onizleme(mesaj.body)
    try:
        record_notification(
            user, type="support", title=baslik, body=govde, support_ticket_id=ticket.pk
        )
    except Exception:
        logger.exception("destek bildirimi yazılamadı ticket=%s", ticket.pk)
    try:
        return push_to_user(
            user,
            title=baslik,
            body=govde,
            data={"type": "support", "supportTicketId": ticket.pk},
            idem_key=f"support_reply:{mesaj.pk}",
            transactional=True,
        )
    except Exception:
        logger.exception("destek push'u gönderilemedi ticket=%s", ticket.pk)
        return False


def _ekibe_haber_ver(ticket_id: int, body: str, *, yeni: bool) -> None:
    """Adres tanımlıysa e-postayı Celery'ye bırakır (adres yoksa kuyruğa hiç gitmez)."""
    if not [a for a in getattr(settings, "SUPPORT_NOTIFY_EMAILS", []) if a]:
        return
    try:
        from .tasks import destek_ekibine_eposta

        destek_ekibine_eposta.delay(ticket_id, body, yeni)
    except Exception:
        logger.exception("destek e-postası kuyruğa alınamadı ticket=%s", ticket_id)


def ekibe_eposta_gonder(ticket: SupportTicket, body: str, *, yeni: bool) -> None:
    """`SUPPORT_NOTIFY_EMAILS` adreslerine yeni talep / kullanıcı yanıtı e-postası."""
    alicilar = [a for a in getattr(settings, "SUPPORT_NOTIFY_EMAILS", []) if a]
    if not alicilar:
        return
    from django.core.mail import send_mail

    konu = (
        f"Yeni destek talebi #{ticket.pk}" if yeni else f"Destek talebine yanıt #{ticket.pk}"
    )
    try:
        send_mail(
            konu,
            f"{ticket.email or 'Anonim'} · {ticket.get_category_display()}\n\n{body}\n\n"
            f"Yanıtla: {settings.PUBLIC_BASE_URL}/admin/core/supportticket/{ticket.pk}/change/",
            settings.DEFAULT_FROM_EMAIL,
            alicilar,
            fail_silently=True,
        )
    except Exception:
        logger.exception("destek e-postası gönderilemedi ticket=%s", ticket.pk)
