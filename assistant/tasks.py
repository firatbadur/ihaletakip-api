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


# "Takip ettiğim / kayıtlı ihaleler" niyeti — kayıtlı ihaleler YALNIZCA bu niyette
# gösterilir (aksi halde alakasız sorularda öne çıkıp kafa karıştırıyordu).
_SAVED_TRIGGERS = (
    "takip etti",
    "takip ettiğim",
    "takibimdeki",
    "takip listem",
    "kayıtlı ihale",
    "kaydettiğim",
    "favori",
)


def _wants_saved(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _SAVED_TRIGGERS)


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
    from tenders.models import SavedTender

    profile = CompanyProfile.objects.filter(user_id=user_id).first()
    if not profile:
        return {"success": False, "error": "Firma profili bulunamadı."}

    user_msg = ChatMessage.objects.filter(user_id=user_id, id=message_id).first()
    if not user_msg:
        return {"success": False, "error": "Sohbet mesajı bulunamadı."}
    conversation = user_msg.conversation
    today = timezone.localdate()
    text = user_msg.content or ""

    # Asistan mesajını kaydedip standart sonucu döndüren yardımcılar
    def _save(reply, cards, usage=None):
        msg = ChatMessage.objects.create(
            user_id=user_id,
            conversation=conversation,
            role=ChatMessage.Role.ASSISTANT,
            content=reply,
            payload={"kind": "text", "tender_cards": cards or []},
        )
        if conversation:
            conversation.save(update_fields=["updated_at"])
        return {"success": True, "analysis": ChatMessageSerializer(msg).data, "usage": usage}

    def _run_llm(context_text, card_pool):
        """LLM'e sorar, İKN'leri havuzdan karta çözer, mesajı kaydeder."""
        messages = build_chat_messages(profile.user, conversation=conversation)
        if not messages:
            return {"success": False, "error": "Sohbet mesajı bulunamadı."}
        try:
            result = chat_completion(profile.profile_map, context_text, messages)
        except AnalysisError as e:
            return {"success": False, "error": e.message}

        reply = result["analysis"]
        iknler = []
        try:
            parsed = parse_json_output(reply)
            reply = parsed.get("reply") or reply
            iknler = [str(x) for x in parsed.get("card_iknler") or []]
        except (ValueError, json.JSONDecodeError):
            logger.warning("assistant_chat_task: model çıktısı JSON değil, düz metin")
        # Güvenlik ağı: yanıta gömülü havuz İKN'lerini de karta çevir (Haiku formata
        # uymayabilir); yalnızca havuzdaki (gerçek) İKN'ler kart olur → uydurma yok.
        for ikn in IKN_RE.findall(reply):
            if ikn in card_pool and ikn not in iknler:
                iknler.append(ikn)
        seen, cards = set(), []
        for ikn in iknler:
            if ikn in card_pool and ikn not in seen:
                seen.add(ikn)
                cards.append(card_pool[ikn])
        return _save(reply, cards, usage=result.get("usage"))

    # ── 1) BELİRLİ BİR İHALE HAKKINDA (seçili ihale veya mesajda İKN) ──
    # Kullanıcı bir ihaleyi seçmiş VEYA mesajında İKN geçiyorsa: o ihaleyi DB'den
    # çöz, detayını LLM'e ver, analiz ettir + tıklanabilir kart döndür.
    tender_obj = (
        Tender.objects.filter(ikn=conversation.tender_ikn).first()
        if (conversation and conversation.tender_ikn)
        else None
    )
    mentioned = list(dict.fromkeys(IKN_RE.findall(text)))  # sıralı, tekrarsız
    asked = list(Tender.objects.filter(ikn__in=mentioned)) if mentioned else []

    focus = tender_obj or (asked[0] if (len(asked) == 1 and not tender_obj) else None)
    if focus:
        return _run_llm(
            _selected_tender_context(focus, today),
            {focus.ikn: tender_card(focus)},
        )

    # Mesajda birden fazla İKN çözüldü → hepsini kart olarak getir (LLM yok)
    if asked:
        cards = [tender_card(t) for t in asked][:8]
        return _save(f"Sorduğunuz {len(cards)} ihaleyi getirdim. Detay için dokunun 👇", cards)

    # Mesajda İKN geçti ama sistemde bulunamadı → bilgilendir
    if mentioned:
        return _save(
            f"{mentioned[0]} numaralı İKN'yi sistemde bulamadım. İKN'yi kontrol edebilir "
            "ya da 'İhaleler' sekmesinden arayıp ihaleyi seçerek bana sorabilirsiniz.",
            [],
        )

    # ── 2) TAKİP ETTİĞİM / KAYITLI İHALELER (yalnızca açıkça sorulunca) ──
    if _wants_saved(text):
        saved = list(SavedTender.objects.filter(user_id=user_id).order_by("-saved_at")[:8])
        s_ikns = [s.tender_ikn for s in saved if s.tender_ikn]
        ekap_by_ikn = (
            {t.ikn: t for t in Tender.objects.filter(ikn__in=s_ikns)} if s_ikns else {}
        )
        cards = []
        for s in saved:
            if not s.tender_ikn:
                continue
            ekt = ekap_by_ikn.get(s.tender_ikn)
            cards.append(
                tender_card(ekt)
                if ekt
                else {
                    "ikn": s.tender_ikn, "ekap_id": s.tender_id or "",
                    "ihale_adi": s.tender_title or "", "idare_adi": s.institution or "",
                    "il": s.tender_city or "", "ihale_tarihi": s.tender_date or "",
                    "ihale_tip": None,
                }
            )
        reply = (
            f"Takip ettiğiniz {len(cards)} ihale 👇"
            if cards
            else "Takip listenizde henüz ihale yok. Bir ihaleyi kaydettiğinizde burada görürsünüz."
        )
        return _save(reply, cards)

    # ── 3) ÖNERİ / LİSTELEME (kural tabanlı, LLM YOK) ──
    if _wants_tender_listing(text):
        recs = list(
            TenderRecommendation.objects.filter(user_id=user_id, date=today)
            .select_related("tender").order_by("-score")[:10]
        )
        context_items = [(r.tender, r.reasons) for r in recs]
        if not context_items:  # beat çalışmadıysa/profil yeni ise CANLI eşleştir
            from datetime import timedelta

            from assistant.services.matching import match_tenders_for_profile

            pm = profile.profile_map or {}
            strong = bool(pm.get("keywords") or pm.get("okas_prefixes"))
            try:
                context_items = [
                    (t, r)
                    for t, s, r in match_tenders_for_profile(
                        profile, since=timezone.now() - timedelta(days=14),
                        limit=10, min_score=3.0 if strong else 1.0,
                    )
                ]
            except Exception:
                logger.exception("assistant_chat_task: canlı eşleştirme hatası")

        cards = [tender_card(t) for t, _ in context_items][:8]
        reply = (
            f"Profilinize uygun {len(cards)} ihale buldum. İncelemek için dokunun 👇"
            if cards
            else "Şu an profilinize uygun bir eşleşme bulamadım. Profilinizdeki il/tür/anahtar "
            "kelimeleri güncelleyebilir ya da bir ihaleyi seçip onun hakkında bana sorabilirsiniz."
        )
        return _save(reply, cards)

    # ── 4) GENEL SORU-CEVAP (LLM; ihale kartı gerekmez) ──
    # Profil zaten persona'da (cache'li); ağır öneri/kayıtlı listesi bağlama konmaz.
    return _run_llm(f"Bugünün tarihi: {today.strftime('%d.%m.%Y')}", {})


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
