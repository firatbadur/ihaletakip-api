"""
assistant Celery görevleri.

DİKKAT: generate_profile_map_task ve assistant_chat_task sonuçları mevcut
`GET /ai/tasks/{task_id}/` (AnalyzeStatusView) üzerinden sorgulanır. O view
SUCCESS'te yalnızca `analysis` ve `usage` anahtarlarını iletir; hata durumu
`{"success": False, "error": "..."}` olmalıdır. Bu sözleşmeyi bozma.
"""
import json
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("ihaletakip")


@shared_task(name="assistant.tasks.generate_profile_map_task")
def generate_profile_map_task(user_id):
    """Firma profilinden AI profil haritası üretir ve kaydeder."""
    from ai.services.claude import AnalysisError
    from assistant.models import CompanyProfile
    from assistant.services.profile_map import generate_profile_map

    profile = CompanyProfile.objects.filter(user_id=user_id).first()
    if not profile:
        return {"success": False, "error": "Firma profili bulunamadı."}

    try:
        profile_map, usage = generate_profile_map(profile)
    except AnalysisError as e:
        return {"success": False, "error": e.message}

    profile.profile_map = profile_map
    profile.profile_map_generated_at = timezone.now()
    profile.save(update_fields=["profile_map", "profile_map_generated_at", "updated_at"])

    return {"success": True, "analysis": profile_map, "usage": usage}


@shared_task(name="assistant.tasks.assistant_chat_task")
def assistant_chat_task(user_id, message_id):
    """Kullanıcı mesajına asistan yanıtı üretir ve kaydeder."""
    from ai.services.claude import AnalysisError
    from assistant.models import ChatMessage, CompanyProfile, TenderRecommendation
    from assistant.serializers import ChatMessageSerializer
    from assistant.services.chat import build_chat_messages, chat_completion
    from assistant.services.matching import tender_card
    from assistant.services.profile_map import parse_json_output

    profile = CompanyProfile.objects.filter(user_id=user_id).first()
    if not profile:
        return {"success": False, "error": "Firma profili bulunamadı."}

    user_msg = ChatMessage.objects.filter(user_id=user_id, id=message_id).first()
    if not user_msg:
        return {"success": False, "error": "Sohbet mesajı bulunamadı."}
    conversation = user_msg.conversation

    # Günün önerileri → değişken system bloğu (cache breakpoint SONRASI)
    today = timezone.localdate()
    recs = list(
        TenderRecommendation.objects.filter(user_id=user_id, date=today)
        .select_related("tender")
        .order_by("-score")[:10]
    )
    context_lines = [f"Bugünün tarihi: {today.strftime('%d.%m.%Y')}"]
    if recs:
        context_lines.append("\n## BUGÜNKÜ ÖNERİLEN İHALELER")
        for r in recs:
            t = r.tender
            context_lines.append(
                f"- İKN {t.ikn} | {t.ihale_adi[:120]} | {t.idare_adi[:80]} | "
                f"{t.ihale_il_adi} | İhale tarihi: {t.ihale_tarih_saat or '-'} | "
                f"Eşleşme nedenleri: {', '.join(r.reasons)}"
            )
    else:
        context_lines.append(
            "\n## BUGÜNKÜ ÖNERİLEN İHALELER\n(Bugün için öneri yok — card_iknler boş kalmalı.)"
        )
    context_text = "\n".join(context_lines)

    # Bağlam yalnızca bu konuşmanın mesajlarından kurulur
    messages = build_chat_messages(profile.user, conversation=conversation)
    if not messages:
        return {"success": False, "error": "Sohbet mesajı bulunamadı."}

    try:
        result = chat_completion(profile.profile_map, context_text, messages)
    except AnalysisError as e:
        return {"success": False, "error": e.message}

    # Model çıktısını parse et — bozuk JSON'da düz metin fallback (asla hata verme)
    reply_text = result["analysis"]
    card_iknler = []
    try:
        parsed = parse_json_output(reply_text)
        reply_text = parsed.get("reply") or reply_text
        card_iknler = [str(x) for x in parsed.get("card_iknler") or []]
    except (ValueError, json.JSONDecodeError):
        logger.warning("assistant_chat_task: model çıktısı JSON değil, düz metin kullanılıyor")

    # Kartları yalnızca bugünkü önerilerden çöz (model uydurmasına karşı güvenlik)
    rec_by_ikn = {r.tender.ikn: r.tender for r in recs}
    tender_cards = [tender_card(rec_by_ikn[ikn]) for ikn in card_iknler if ikn in rec_by_ikn]

    assistant_msg = ChatMessage.objects.create(
        user_id=user_id,
        conversation=conversation,
        role=ChatMessage.Role.ASSISTANT,
        content=reply_text,
        payload={"kind": "text", "tender_cards": tender_cards},
    )
    if conversation:
        conversation.save(update_fields=["updated_at"])  # geçmiş listesinde öne taşı

    return {
        "success": True,
        "analysis": ChatMessageSerializer(assistant_msg).data,
        "usage": result.get("usage"),
    }


@shared_task(name="assistant.tasks.match_recommendations")
def match_recommendations():
    """
    Günlük eşleştirme: her aktif profil için dünden beri ilan edilen açık
    ihaleleri skorlar; öneri + bildirim + digest sohbet mesajı üretir.
    """
    from datetime import timedelta

    from assistant.models import (
        ChatConversation,
        ChatMessage,
        CompanyProfile,
        TenderRecommendation,
    )
    from assistant.services.matching import match_tenders_for_profile, tender_card
    from tenders.models import Notification

    today = timezone.localdate()
    since = timezone.now() - timedelta(days=1)

    profiles = CompanyProfile.objects.filter(is_active=True).exclude(profile_map__isnull=True)
    total_recs = 0

    for profile in profiles.iterator():
        try:
            matches = match_tenders_for_profile(profile, since=since)
        except Exception:
            logger.exception("match_recommendations: profil %s eşleştirme hatası", profile.id)
            continue

        if not matches:
            continue

        # Daha önce önerilenleri tekrar önerme (unique constraint + ön kontrol)
        existing = set(
            TenderRecommendation.objects.filter(
                user=profile.user, tender__in=[t.id for t, _, _ in matches]
            ).values_list("tender_id", flat=True)
        )
        fresh = [(t, s, r) for t, s, r in matches if t.id not in existing]
        if not fresh:
            continue

        TenderRecommendation.objects.bulk_create(
            [
                TenderRecommendation(
                    user=profile.user, tender=t, score=s, reasons=r, date=today
                )
                for t, s, r in fresh
            ],
            ignore_conflicts=True,
        )
        total_recs += len(fresh)

        top = fresh[:5]
        top_titles = "\n".join(f"• {t.ihale_adi[:80]}" for t, _, _ in top[:3])

        Notification.objects.create(
            user=profile.user,
            type=Notification.Type.TENDER,
            title=f"İhale Asistanı: {len(fresh)} yeni öneri",
            body=top_titles,
            tender_ikn=top[0][0].ikn if top else None,
            tender_title=top[0][0].ihale_adi[:500] if top else None,
        )

        # Digest kendi konuşmasında yaşar → geçmiş sohbetler listesinde görünür,
        # kullanıcı içinden devam edip soru sorabilir.
        digest_conv = ChatConversation.objects.create(
            user=profile.user,
            title=f"Günlük Öneriler · {today.strftime('%d.%m.%Y')}",
            kind=ChatConversation.Kind.DIGEST,
        )
        ChatMessage.objects.create(
            user=profile.user,
            conversation=digest_conv,
            role=ChatMessage.Role.ASSISTANT,
            content=(
                f"Günaydın! Bugün profilinize uygun {len(fresh)} yeni ihale buldum. "
                "Öne çıkanları aşağıda listeledim — detayları sorabilirsiniz."
            ),
            payload={
                "kind": "digest",
                "tender_cards": [tender_card(t) for t, _, _ in top],
            },
        )

    logger.info("match_recommendations: %s profil işlendi, %s öneri üretildi", profiles.count(), total_recs)
    return {"profiles": profiles.count(), "recommendations": total_recs}
