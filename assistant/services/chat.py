"""
Asistan sohbet servisi — çok turlu Claude çağrısı.

System prompt iki blok:
  1. SABİT blok (persona + profil haritası) → cache_control breakpoint burada.
     İçinde tarih/değişken YOK; byte-stabil kalması için profil haritası
     sort_keys=True ile serileştirilir.
  2. DEĞİŞKEN blok (bugünün tarihi + günün önerilen ihaleleri) → breakpoint
     SONRASI; her gün değişir, cache'i bozmaz.
"""
import json
import logging

from django.conf import settings

from ai.services.claude import AnalysisError, get_api_key

logger = logging.getLogger("ihaletakip")


def chat_completion(profile_map: dict, context_text: str, messages: list, max_tokens: int = None) -> dict:
    """
    Claude'a çok turlu sohbet isteği gönderir.
    messages: [{"role": "user"|"assistant", "content": str}, ...] (ilk eleman user olmalı)
    Dönen: {"analysis": <model metni>, "usage": {...}}
    """
    import anthropic

    client = anthropic.Anthropic(api_key=get_api_key())

    from assistant.prompts import PERSONA_PROMPT

    system = [
        {
            "type": "text",
            "text": PERSONA_PROMPT
            + "\n\n## FİRMA PROFİLİ\n"
            + json.dumps(profile_map or {}, ensure_ascii=False, sort_keys=True),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": context_text},
    ]

    try:
        message = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=max_tokens or settings.CLAUDE_MAX_TOKENS,
            system=system,
            messages=messages,
        )
    except anthropic.APIError as e:
        code = getattr(e, "status_code", None) or "bağlantı"
        logger.warning("Asistan Claude API hatası (%s): %s", code, e)
        raise AnalysisError(f"AI servisi geçici olarak yanıt vermiyor ({code}).", status=502) from e

    cache_read = getattr(message.usage, "cache_read_input_tokens", None)
    if cache_read:
        logger.info("Asistan chat cache hit: %s token", cache_read)

    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    return {
        "analysis": text,
        "usage": {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        },
    }


def build_chat_messages(user, conversation=None, limit: int = 20) -> list:
    """
    Son mesajlardan Claude messages listesi kurar.
    - conversation verilirse bağlam o konuşmayla sınırlanır (faz 2: oturum bazlı sohbet).
    - Ardışık aynı-rol turları birleştirir.
    - Listenin user turu ile başlamasını garanti eder.
    """
    from assistant.models import ChatMessage

    qs = ChatMessage.objects.filter(user=user)
    if conversation is not None:
        qs = qs.filter(conversation=conversation)
    rows = list(qs.order_by("-created_at")[:limit])[::-1]  # kronolojik sıraya çevir

    messages = []
    for row in rows:
        if messages and messages[-1]["role"] == row.role:
            messages[-1]["content"] += "\n\n" + row.content
        else:
            messages.append({"role": row.role, "content": row.content})

    # İlk mesaj user olmalı (digest asistan mesajıyla başlayabilir)
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    return messages
