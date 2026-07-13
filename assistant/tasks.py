"""
assistant Celery görevleri.

DİKKAT: generate_profile_map_task ve assistant_chat_task sonuçları mevcut
`GET /ai/tasks/{task_id}/` (AnalyzeStatusView) üzerinden sorgulanır. O view
SUCCESS'te yalnızca `analysis` ve `usage` anahtarlarını iletir; hata durumu
`{"success": False, "error": "..."}` olmalıdır. Bu sözleşmeyi bozma.
"""
import json
import logging
import re

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

# Türkiye İKN biçimi: 2026/1234567
IKN_RE = re.compile(r"\b\d{4}/\d{4,}\b")

# "Bana ihaleleri göster/öner" tipi niyet — bu sorularda öneriler KURAL TABANLI
# (LLM'siz) döner; kartlar doğrudan eşleştirmeden gelir. Genel sohbet soruları
# (ör. "geçici teminat nedir?") LLM'e gider.
_TENDER_LIST_TRIGGERS = (
    "uygun ihale",
    "bana uygun",
    "ihale var mı",
    "ihale var mi",
    "ihaleler var mı",
    "bugünkü ihale",
    "bugunku ihale",
    "bugün ihale",
    "bugun ihale",
    "bugünkü ihaleler",
    "yeni ihale",
    "hangi ihale",
    "ihaleleri göster",
    "ihaleleri listele",
    "ihaleleri getir",
    "ihale öner",
    "ihale oner",
    "ihaleler neler",
    "ihaleleri neler",
    "fırsat var",
    "önerdiğin ihale",
)


def _wants_tender_listing(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _TENDER_LIST_TRIGGERS)


def _selected_tender_context(tender, today) -> str:
    """İhale odaklı sohbette LLM'e verilecek ihale detayı bağlamı."""
    okas = "; ".join(
        f"{i.kodu} {i.adi}".strip()[:70]
        for i in tender.okas_kalemleri.all()[:8]
        if (i.kodu or i.adi)
    )
    lines = [
        f"Bugünün tarihi: {today.strftime('%d.%m.%Y')}",
        "\n## KULLANICININ SEÇTİĞİ İHALE (sohbet bu ihale hakkında)",
        f"- İKN: {tender.ikn}",
        f"- İhale adı: {tender.ihale_adi}",
        f"- İdare: {tender.idare_adi}"
        + (f" | Üst idare: {tender.ust_idare}" if tender.ust_idare else ""),
        f"- Yer: {(tender.ihale_il_adi + ' ' + tender.ilce_adi).strip() or '-'}",
        f"- İhale türü: {tender.ihale_tipi_aciklama or tender.ihale_tip or '-'}",
        f"- İhale usulü: {tender.ihale_usul_aciklama or '-'}",
        f"- Durum: {tender.ihale_durum_aciklama or '-'}",
        f"- İhale tarihi/saati: {tender.ihale_tarih_saat or '-'}",
        f"- Kapsam: {tender.ihale_kapsam_aciklama or '-'}",
        f"- İşin/malın yapılacağı yer: {tender.isin_yapilacagi_yer or tender.ihale_yeri or '-'}",
        f"- İtirazen şikayet başvuru bedeli: {tender.itirazen_sikayet_basvuru_bedeli or '-'}",
        f"- e-İhale: {'Evet' if tender.e_ihale else 'Hayır'} | Doküman sayısı: {tender.dokuman_sayisi}",
    ]
    if okas:
        lines.append(f"- OKAS ihtiyaç kalemleri: {okas}")
    lines.append(
        "\nKullanıcı bu ihale hakkında soru soruyor (uygunluk, maliyet/keşif yaklaşımı, "
        "yeterlilik, teklif stratejisi vb.). Yukarıdaki bilgilere ve firmanın profiline "
        "göre yanıtla. Kesin rakam TAAHHÜT ETME; maliyet için genel yaklaşım ver ve "
        "detaylı hesap için uygulamanın 'Maliyet Analizi' özelliğine yönlendir. İhale "
        "metninde OLMAYAN bilgiyi uydurma; eksikse EKAP dokümanlarına yönlendir. "
        "card_iknler'e yalnızca bu ihalenin İKN'sini koyabilirsin."
    )
    return "\n".join(lines)


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
    from ekap.models import Tender
    from tenders.models import SavedFilter, SavedTender

    profile = CompanyProfile.objects.filter(user_id=user_id).first()
    if not profile:
        return {"success": False, "error": "Firma profili bulunamadı."}

    user_msg = ChatMessage.objects.filter(user_id=user_id, id=message_id).first()
    if not user_msg:
        return {"success": False, "error": "Sohbet mesajı bulunamadı."}
    conversation = user_msg.conversation
    today = timezone.localdate()

    # ── İHALE ODAKLI SOHBET ────────────────────────────────────────
    # Konuşma bir ihaleye bağlıysa: o ihalenin detaylarını bağlama koy ve LLM'e
    # analiz ettir (uygunluk, maliyet yaklaşımı, yeterlilik vb.). Deterministik
    # listeleme ve öneri bağlamı atlanır — sohbet tek ihaleye odaklıdır.
    tender_obj = None
    if conversation and conversation.tender_ikn:
        tender_obj = Tender.objects.filter(ikn=conversation.tender_ikn).first()

    if tender_obj:
        context_text = _selected_tender_context(tender_obj, today)
        card_by_ikn = {tender_obj.ikn: tender_card(tender_obj)} if tender_obj.ikn else {}

        messages = build_chat_messages(profile.user, conversation=conversation)
        if not messages:
            return {"success": False, "error": "Sohbet mesajı bulunamadı."}
        try:
            result = chat_completion(profile.profile_map, context_text, messages)
        except AnalysisError as e:
            return {"success": False, "error": e.message}

        reply_text = result["analysis"]
        card_iknler = []
        try:
            parsed = parse_json_output(reply_text)
            reply_text = parsed.get("reply") or reply_text
            card_iknler = [str(x) for x in parsed.get("card_iknler") or []]
        except (ValueError, json.JSONDecodeError):
            logger.warning("assistant_chat_task: ihale-odaklı çıktı JSON değil, düz metin")
        for ikn in IKN_RE.findall(reply_text):
            if ikn in card_by_ikn and ikn not in card_iknler:
                card_iknler.append(ikn)
        seen = set()
        tender_cards = []
        for ikn in card_iknler:
            if ikn in card_by_ikn and ikn not in seen:
                seen.add(ikn)
                tender_cards.append(card_by_ikn[ikn])

        assistant_msg = ChatMessage.objects.create(
            user_id=user_id,
            conversation=conversation,
            role=ChatMessage.Role.ASSISTANT,
            content=reply_text,
            payload={"kind": "text", "tender_cards": tender_cards},
        )
        conversation.save(update_fields=["updated_at"])
        return {
            "success": True,
            "analysis": ChatMessageSerializer(assistant_msg).data,
            "usage": result.get("usage"),
        }

    # ── GENEL SOHBET / ÖNERİ AKIŞI ─────────────────────────────────
    # Günün önerileri → değişken system bloğu (cache breakpoint SONRASI)
    recs = list(
        TenderRecommendation.objects.filter(user_id=user_id, date=today)
        .select_related("tender")
        .order_by("-score")[:10]
    )
    context_items = [(r.tender, r.reasons) for r in recs]

    # Depolanmış öneri yoksa CANLI eşleştir: günlük 07:00 beat'i çalışmamış olabilir
    # veya kullanıcı profilini yeni güncellemiş olabilir. Böylece asistan beat'e
    # bağımlı kalmaz ve profil değişiklikleri anında yansır (kural tabanlı, LLM'siz).
    if not context_items:
        from datetime import timedelta

        from assistant.services.matching import match_tenders_for_profile

        pm = profile.profile_map or {}
        # profile_map zayıf/null ise (anahtar kelime/OKAS yok) yalnızca il+tür ile
        # eşleşilebilsin diye eşiği düşür — aksi halde hiçbir şey 3 puana ulaşmaz.
        strong = bool(pm.get("keywords") or pm.get("okas_prefixes"))
        since = timezone.now() - timedelta(days=14)
        try:
            matches = match_tenders_for_profile(
                profile, since=since, limit=10, min_score=3.0 if strong else 1.0
            )
            context_items = [(t, r) for t, s, r in matches]
        except Exception:
            logger.exception("assistant_chat_task: canlı eşleştirme hatası")

    # Kart havuzu: İKN → kart. Model yalnızca bu havuzdaki İKN'leri kart yapabilir.
    card_by_ikn = {t.ikn: tender_card(t) for t, _ in context_items if t.ikn}

    # Kullanıcının kayıtlı ihaleleri ve kayıtlı aramaları (arama geçmişi) → ek bağlam.
    # Böylece asistan yalnızca günlük eşleşmelerden değil, kullanıcının takip ettiği
    # ihalelerden ve arama ilgi alanlarından da yararlanır.
    saved_tenders = list(SavedTender.objects.filter(user_id=user_id).order_by("-saved_at")[:10])
    saved_filters = list(SavedFilter.objects.filter(user_id=user_id).order_by("-created_at")[:8])
    # Kayıtlı ihaleleri gerçek EKAP kaydına bağla → doğru ekap_id (tıklanabilirlik) + tür
    saved_ikns = [st.tender_ikn for st in saved_tenders if st.tender_ikn]
    ekap_by_ikn = (
        {t.ikn: t for t in Tender.objects.filter(ikn__in=saved_ikns)} if saved_ikns else {}
    )
    for st in saved_tenders:
        ikn = st.tender_ikn
        if not ikn or ikn in card_by_ikn:
            continue
        ekt = ekap_by_ikn.get(ikn)
        if ekt:
            card_by_ikn[ikn] = tender_card(ekt)  # zengin kart + doğru ekap_id
        else:
            card_by_ikn[ikn] = {
                "ikn": ikn,
                "ekap_id": st.tender_id or "",
                "ihale_adi": st.tender_title or "",
                "idare_adi": st.institution or "",
                "il": st.tender_city or "",
                "ihale_tarihi": st.tender_date or "",
                "ihale_tip": None,
            }

    # ── DETERMİNİSTİK YOL (LLM YOK) ────────────────────────────────
    # "Bana uygun ihale / bugünkü ihaleler" gibi sorularda öneriler zaten kural
    # tabanlı eşleştirmeden geliyor; LLM'e sarmaya gerek yok. Kartları doğrudan
    # döndür → her zaman tıklanabilir kart + token yakmaz + güvenilir.
    if _wants_tender_listing(user_msg.content):
        rec_cards = [tender_card(t) for t, _ in context_items][:8]
        if rec_cards:
            reply_text = (
                f"Profilinize uygun {len(rec_cards)} ihale buldum. "
                "İncelemek için ihaleye dokunun 👇"
            )
            cards = rec_cards
        else:
            saved_cards = [
                card_by_ikn[st.tender_ikn]
                for st in saved_tenders
                if st.tender_ikn in card_by_ikn
            ][:8]
            if saved_cards:
                reply_text = (
                    "Şu an profilinize uygun yeni bir eşleşme bulamadım. "
                    f"Takip ettiğiniz {len(saved_cards)} ihaleyi aşağıya ekledim 👇"
                )
                cards = saved_cards
            else:
                reply_text = (
                    "Şu an size uygun bir ihale bulamadım. Profilinizdeki il/tür/anahtar "
                    "kelimeleri güncelleyerek daha iyi eşleşmeler alabilir ya da 'İhaleler' "
                    "sekmesinden arama yapabilirsiniz."
                )
                cards = []

        assistant_msg = ChatMessage.objects.create(
            user_id=user_id,
            conversation=conversation,
            role=ChatMessage.Role.ASSISTANT,
            content=reply_text,
            payload={"kind": "text", "tender_cards": cards},
        )
        if conversation:
            conversation.save(update_fields=["updated_at"])
        return {
            "success": True,
            "analysis": ChatMessageSerializer(assistant_msg).data,
            "usage": None,  # LLM çağrısı yok
        }

    # ── LLM YOLU (genel sohbet/soru-cevap) ─────────────────────────
    context_lines = [f"Bugünün tarihi: {today.strftime('%d.%m.%Y')}"]
    if context_items:
        context_lines.append("\n## ÖNERİLEN İHALELER")
        for t, reasons in context_items:
            context_lines.append(
                f"- İKN {t.ikn} | {t.ihale_adi[:120]} | {t.idare_adi[:80]} | "
                f"{t.ihale_il_adi} | İhale tarihi: {t.ihale_tarih_saat or '-'} | "
                f"Eşleşme nedenleri: {', '.join(reasons)}"
            )
    else:
        context_lines.append(
            "\n## ÖNERİLEN İHALELER\n(Şu an profile uygun yeni eşleşme yok.)"
        )

    if saved_tenders:
        context_lines.append("\n## KAYITLI İHALELERİNİZ (kullanıcının takip ettiği)")
        for st in saved_tenders:
            context_lines.append(
                f"- İKN {st.tender_ikn} | {(st.tender_title or '')[:120]} | "
                f"{(st.institution or '')[:80]} | {st.tender_city or '-'} | "
                f"İhale tarihi: {st.tender_date or '-'} | Durum: {st.tender_status or '-'}"
            )

    if saved_filters:
        aramalar = "; ".join(
            f"{sf.name}" + (f" [{', '.join(sf.tags)}]" if sf.tags else "")
            for sf in saved_filters
        )
        context_lines.append(
            "\n## KAYITLI ARAMALARINIZ (kullanıcının ilgi/arama geçmişi)\n" + aramalar
        )

    if not context_items and not saved_tenders:
        context_lines.append(
            "\n(Kart gösterecek ihale yok — card_iknler boş kalmalı.)"
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

    # Güvenlik ağı: model card_iknler'i unutup İKN'yi metne gömse bile, yanıtta
    # geçen ve havuzda olan İKN'leri karta çevir (Haiku her zaman formata uymuyor).
    for ikn in IKN_RE.findall(reply_text):
        if ikn in card_by_ikn and ikn not in card_iknler:
            card_iknler.append(ikn)

    # Kartları yalnızca context havuzundan çöz (öneriler + kayıtlı ihaleler) —
    # model uydurmasına karşı güvenlik. Sırayı koru, tekrarı ele.
    seen_ikn = set()
    tender_cards = []
    for ikn in card_iknler:
        if ikn in card_by_ikn and ikn not in seen_ikn:
            seen_ikn.add(ikn)
            tender_cards.append(card_by_ikn[ikn])

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
def match_recommendations(since_days=1):
    """
    Günlük eşleştirme: her aktif profil için son `since_days` günde ilan edilen açık
    ihaleleri skorlar; öneri + bildirim + digest sohbet mesajı üretir.

    since_days: elle tetiklerken geniş pencere için artırılabilir (bkz.
    `manage.py run_assistant_match --days N`).
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
    since = timezone.now() - timedelta(days=since_days)

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
