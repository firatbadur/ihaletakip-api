"""
assistant Celery görevleri.

DİKKAT: generate_profile_map_task ve assistant_chat_task sonuçları mevcut
`GET /ai/tasks/{task_id}/` (AnalyzeStatusView) üzerinden sorgulanır. O view
SUCCESS'te yalnızca `analysis` ve `usage` anahtarlarını iletir; hata durumu
`{"success": False, "error": "..."}` olmalıdır. Bu sözleşmeyi bozma.

Ayrıca o view sonucu **yalnızca görevi başlatan kullanıcıya** verir → dönüş
payload'u `user_id` taşımalıdır (bkz. `_stamp_owner`).
"""
import functools
import json
import logging
import re

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("ihaletakip")


def _stamp_owner(fn):
    """
    Görevin dönüş sözlüğüne `user_id` damgalar (ilk parametre `user_id` olmalı).

    ⚠️ Dekoratör kullanılıyor çünkü bu görevlerin **çok sayıda dönüş noktası** var
    (`assistant_chat_task`'ta 5 tane); tek tek eklemek birini atlamaya açık ve atlanan
    dal `AnalyzeStatusView`'da ya sızıntı ya da 404 üretir. Damgalama tek yerde.
    """

    @functools.wraps(fn)
    def wrapper(user_id, *args, **kwargs):
        result = fn(user_id, *args, **kwargs)
        if isinstance(result, dict):
            result.setdefault("user_id", user_id)
        return result

    return wrapper


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
@_stamp_owner
def generate_profile_map_task(user_id):
    """Firma profilinden AI profil haritası üretir ve kaydeder."""
    from ai.services.claude import AnalysisError
    from assistant.models import CompanyProfile
    from assistant.services.profile_map import (
        derive_from_contractor,
        generate_profile_map,
        profile_input_digest,
    )

    profile = CompanyProfile.objects.select_related("contractor").filter(user_id=user_id).first()
    if not profile:
        return {"success": False, "error": "Firma profili bulunamadı."}

    # Sihirbaz artık il/tür sormuyor: firma EKAP kaydına bağlıysa bunlar sözleşme
    # geçmişinden türetilir. Digest'ten ÖNCE yapılır — türetilen alanlar prompt'a
    # girdiği için hash'i etkilemeli.
    if derive_from_contractor(profile):
        profile.save(update_fields=["cities", "tender_types", "updated_at"])

    # ── Maliyet kapısı ────────────────────────────────────────────────────────
    # `PUT /assistant/profile/` her çağrıldığında ücretli bir Sonnet isteği gidiyordu;
    # kullanıcı hiçbir alanı değiştirmemiş olsa bile. Prompt girdisi aynıysa LLM'e HİÇ
    # gitme, mevcut haritayı döndür. Atlama **görevde** yapılır (view'da değil) ki uç
    # her zaman `202 {task_id}` dönmeye devam etsin — mobil sözleşmesi bozulmaz, istemci
    # bir kez yoklar ve `completed` alır.
    digest = profile_input_digest(profile)
    if profile.profile_map and profile.profile_map_kaynak_hash == digest:
        logger.info("generate_profile_map_task: profil degismedi, LLM atlandi (user=%s)", user_id)
        return {"success": True, "analysis": profile.profile_map, "usage": None, "cached": True}

    try:
        profile_map, usage = generate_profile_map(profile)
    except AnalysisError as e:
        return {"success": False, "error": e.message}

    profile.profile_map = profile_map
    profile.profile_map_generated_at = timezone.now()
    profile.profile_map_kaynak_hash = digest
    profile.save(update_fields=[
        "profile_map", "profile_map_generated_at", "profile_map_kaynak_hash", "updated_at",
    ])

    return {"success": True, "analysis": profile_map, "usage": usage}


def ctx_mesaj_id(save_sonucu):
    """`_save` dönüşünden kaydedilen ChatMessage id'sini çıkarır."""
    return ((save_sonucu or {}).get("analysis") or {}).get("id")


def _eylem_idleri(bloklar):
    """
    Blok listesinden onay kartı (`action`) kimliklerini süzer.

    Ayrı fonksiyon: `bloklar` karışık tipli bir listedir (`action`, `bar_chart`,
    `suggestions`) ve hepsinde `action_id` olduğunu varsaymak üretimde görevi
    çökerten bir hataydı. Tip süzgeci tek yerde ve test edilebilir olsun.
    """
    return [
        b["action_id"]
        for b in (bloklar or [])
        if isinstance(b, dict) and b.get("type") == "action" and b.get("action_id")
    ]


def _agent_yaniti(profile, user_msg, conversation, today):
    """
    Araç kullanan yanıt yolu. Dönen: `(reply_metni, kartlar, usage)`.

    ⚠️ Anahtar kelime yönlendirmesi (eski `_wants_tender_listing` / `_wants_saved`)
    BURADA YOK ve geri eklenmemeli: model hangi aracı çağıracağına kendi karar veriyor.
    Substring tetikleyicileri modelle çelişir — kullanıcı "bugünkü ihaleler neler" derken
    aslında kayıtlılarını kastediyorsa tetikleyici yanlış dalı seçer, model seçmez.
    """
    from assistant.services.agent import sohbet_turu
    from assistant.services.chat import build_chat_messages
    from assistant.tools.context import ToolContext
    from ekap.models import Tender

    ctx = ToolContext(
        user=profile.user,
        premium=bool(getattr(profile.user, "is_premium", False)),
        conversation=conversation,
    )

    baglam = [f"Bugünün tarihi: {today.strftime('%d.%m.%Y')}"]

    # Seçili ihale (ihale odaklı sohbet): detayı ÖN ÇAĞRIYLA bağlama koy — modelin
    # ilk turu bunu aramakla harcamasın (tur = en pahalı değişken).
    if conversation and conversation.tender_ikn:
        t = Tender.objects.filter(ikn=conversation.tender_ikn).first()
        if t:
            baglam.append(_selected_tender_context(t, today))
            ctx.kart_ekle(t)

    # Mesajda geçen İKN'ler: modele ipucu olarak verilir, araç çağrısını o yapar.
    gecen = [i for i in dict.fromkeys(IKN_RE.findall(user_msg.content or ""))][:5]
    if gecen:
        baglam.append(
            "Kullanıcının mesajında şu İKN'ler geçiyor: "
            + ", ".join(gecen)
            + ". Bunları `ihale_detay` ile çöz."
        )

    # Takip soruları ("peki İstanbul'dakiler?", "sadece yapım işleri") için son arama.
    # ⚠️ Sohbet geçmişi metin-only olduğu için model önceki filtresini HATIRLAYAMAZ;
    # bu enjeksiyon olmadan aramayı sıfırdan kurar ve konu filtresi sessizce düşer.
    if conversation and conversation.son_arama:
        import json as _json

        baglam.append(
            "SON ARAMANIN PARAMETRELERİ: "
            + _json.dumps(conversation.son_arama, ensure_ascii=False)
            + "\nKullanıcı bu aramayı DARALTIYOR ya da genişletiyorsa (\"peki "
            "İstanbul'dakiler\", \"sadece yapım\", \"geçen yıl\") bu parametrelerin "
            "TAMAMINI yeniden gönder, üstüne değişikliği ekle. Konuyu açıkça "
            "değiştirdiyse bunları KULLANMA."
        )

    messages = build_chat_messages(profile.user, conversation=conversation)
    if not messages:
        return None, [], None

    sonuc = sohbet_turu(ctx, profile.profile_map, "\n\n".join(baglam), messages)
    reply = sonuc["metin"]

    # Kart seçimi: önce cevap metninde ANILAN İKN'ler (model kastettiğini söylemiş),
    # yoksa son araç turunun ürettiği kartlar. Her iki durumda da YALNIZCA havuzdaki
    # (yani gerçekten bir araçtan gelmiş) İKN'ler karta dönüşür → uydurma imkânsız.
    anilan = [i for i in dict.fromkeys(IKN_RE.findall(reply)) if i in ctx.card_pool]
    secilen = anilan or [i for i in ctx.son_grup if i in ctx.card_pool]
    kartlar = [ctx.card_pool[i] for i in secilen[:8]]

    # Onay kartları (kaydet / alarm / filtre önerileri) — mobil `payload.blocks`'tan okur.
    from assistant.views import _eylem_karti

    # Sıra ÖNEMLİ: önce görsel bloklar (grafik), sonra onay kartları — kullanıcı
    # önce veriyi görüp sonra kararı verir.
    bloklar = ctx.gorsel_bloklar[:2] + [_eylem_karti(e) for e in ctx.oneriler[:3]]

    # Soru önerileri EN SONA: kullanıcı önce cevabı ve kartları görsün, "sıradaki adım"
    # en altta dursun. Deterministik üretilir (assistant/tools/oneri.py) — model
    # yapamayacağı bir şeyi öneremez.
    from assistant.tools.oneri import oneri_blogu

    oneri_bloku = oneri_blogu(ctx, sonuc.get("arac_izi"))
    if oneri_bloku:
        bloklar.append(oneri_bloku)

    # Bu turdaki SON başarılı ihale aramasını konuşmaya yaz → sonraki tur daraltabilsin.
    # Arama yapılmadıysa öncekini SİLME: kullanıcı araya "geçici teminat nedir?" gibi
    # bir soru sıkıştırıp sonra "peki İstanbul'dakiler?" diyebilir.
    if conversation:
        aramalar = [a for a in (sonuc.get("arac_izi") or [])
                    if a.get("arac") == "ihale_ara" and a.get("ok")]
        if aramalar:
            conversation.son_arama = aramalar[-1].get("param") or None
            conversation.save(update_fields=["son_arama"])

    return reply, kartlar, sonuc.get("usage"), bloklar


# ⚠️ soft_time_limit ŞART: hard limit (CELERY_TASK_TIME_LIMIT=300) SIGKILL demek →
# mesaj kaydedilmeden ölür, kullanıcı boş sohbet görür. Soft limit exception fırlatır,
# aşağıdaki yakalayıcı elimizdekiyle bir mesaj kaydeder.
@shared_task(name="assistant.tasks.assistant_chat_task", soft_time_limit=240)
@_stamp_owner
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
    def _save(reply, cards, usage=None, blocks=None):
        # `usage` DB'ye yazılır (maliyet izleme, bkz. ChatMessage.usage) ama
        # ChatMessageSerializer'da YOK → mobile sızmaz, iç veridir.
        msg = ChatMessage.objects.create(
            user_id=user_id,
            conversation=conversation,
            role=ChatMessage.Role.ASSISTANT,
            content=reply,
            payload={
                "kind": "text",
                # ⚠️ `tender_cards` KORUNUR: eski mesajlar ve güncellenmemiş mobil
                # sürümler bu alandan okuyor. `blocks` üstüne eklenir, yerine değil.
                "tender_cards": cards or [],
                **({"blocks": blocks} if blocks else {}),
            },
            usage=usage or None,
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

    # ── 0) ARAÇ KULLANAN YOL (varsayılan) ──────────────────────────────────
    # Kapatmak için ASSISTANT_AGENT_ENABLED=False → aşağıdaki eski anahtar kelime
    # akışı devreye girer (kademeli çıkış / acil geri dönüş anahtarı).
    if settings.ASSISTANT_AGENT_ENABLED:
        try:
            reply, kartlar, usage, bloklar = _agent_yaniti(
                profile, user_msg, conversation, today
            )
        except SoftTimeLimitExceeded:
            logger.warning("assistant_chat_task süre aşımı (user=%s)", user_id)
            return _save(
                "Bu soruyu yanıtlamak beklenenden uzun sürdü. Biraz daha dar bir "
                "soruyla tekrar dener misiniz?",
                [],
            )
        except AnalysisError as e:
            return {"success": False, "error": e.message}
        except Exception:
            logger.exception("assistant_chat_task araç yolu çöktü (user=%s)", user_id)
            return {"success": False, "error": "Asistan şu an yanıt veremiyor."}
        if reply is None:
            return {"success": False, "error": "Sohbet mesajı bulunamadı."}
        msg_sonuc = _save(reply, kartlar, usage=usage, blocks=bloklar)
        # Öneriler mesaja bağlanır: kart hangi mesajda gösterildi, izlenebilir olsun.
        # ⚠️ YALNIZCA `action` blokları — `bloklar` artık grafik ve soru önerisi de
        # taşıyor, onlarda `action_id` YOK. Burada körlemesine `b["action_id"]`
        # okumak KeyError veriyordu ve bu satır try/except'in DIŞINDA olduğu için
        # görev çöküp kullanıcıya jenerik "Analiz sırasında bir hata oluştu."
        # dönüyordu (üretimde yaşandı).
        # ⚠️ Bu blok DEFTER TUTMADIR ve mesaj ZATEN KAYDEDİLDİ. Burada bir hata
        # görevi çökertirse istemci "başarısız" görür ve kullanıcı, aslında üretilmiş
        # ve veritabanına yazılmış cevabı kaybeder (üretimde tam olarak bu yaşandı).
        # Cevabın teslimi, izlenebilirlik kaydından daha önemlidir.
        try:
            eylem_idleri = _eylem_idleri(bloklar)
            if eylem_idleri and ctx_mesaj_id(msg_sonuc):
                from assistant.models import AssistantAction

                AssistantAction.objects.filter(id__in=eylem_idleri).update(
                    message_id=ctx_mesaj_id(msg_sonuc)
                )
        except Exception:
            logger.exception("eylem-mesaj bağlama başarısız (user=%s)", user_id)
        return msg_sonuc

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
            from assistant.services.matching import match_tenders_for_profile

            pm = profile.profile_map or {}
            strong = bool(pm.get("keywords") or pm.get("okas_prefixes"))
            try:
                # since=None → tüm açık + teklifi geçmemiş uygun ihaleler (ilan_tarihi kısıtı yok)
                context_items = [
                    (t, r)
                    for t, s, r in match_tenders_for_profile(
                        profile, since=None, limit=10, min_score=3.0 if strong else 1.0,
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


def _repair_missing_profile_maps(limit: int = 20) -> int:
    """
    Profil haritası olmayan profiller için üretimi yeniden dener.

    ⚠️ Neden gerekli: harita `PUT /assistant/profile/` sırasında TEK SEFER üretiliyor.
    O çağrı başarısız olursa (ör. AI yanıtı token sınırında kesildi) profil haritasız
    kalıyor ve `match_recommendations` aşağıdaki `exclude(profile_map__isnull=True)`
    ile onu SONSUZA DEK atlıyordu → kullanıcı hiçbir öneri almıyordu, üstelik mobil
    ona "daha sonra otomatik denenecek" demişti. Deneme burada yapılır.

    `limit`: her turda en çok kaç profil denenir (ücretli LLM çağrısı — kalıcı bir
    arıza durumunda maliyet sınırsız büyümesin).
    """
    from assistant.models import CompanyProfile
    from assistant.services.profile_map import (
        derive_from_contractor,
        generate_profile_map,
        profile_input_digest,
    )

    onarilan = 0
    profiles = (
        CompanyProfile.objects.filter(is_active=True, profile_map__isnull=True)
        .select_related("contractor")
        .order_by("-updated_at")[:limit]
    )
    for profile in profiles:
        try:
            if derive_from_contractor(profile):
                profile.save(update_fields=["cities", "tender_types", "updated_at"])
            profile_map, _usage = generate_profile_map(profile)
        except Exception:
            logger.exception("profil haritası onarımı başarısız (profil=%s)", profile.id)
            continue
        profile.profile_map = profile_map
        profile.profile_map_generated_at = timezone.now()
        profile.profile_map_kaynak_hash = profile_input_digest(profile)
        profile.save(update_fields=[
            "profile_map", "profile_map_generated_at", "profile_map_kaynak_hash", "updated_at",
        ])
        onarilan += 1

    if onarilan:
        logger.info("profil haritası onarıldı: %s profil", onarilan)
    return onarilan


@shared_task(name="assistant.tasks.match_recommendations")
def match_recommendations(since_days=1):
    """
    Günlük eşleştirme: her aktif profil için **yalnızca ilan_tarihi BUGÜN olan** açık ihaleleri
    skorlar; öneri + bildirim + digest sohbet mesajı üretir (dün/eski yayınlananlar DEĞİL).

    since_days: elle tetiklerken geniş pencere için artırılabilir (bkz.
    `manage.py run_assistant_match --days N`). `since_days>1` verilirse "bugün" katı filtresi
    yerine son N günün gevşek penceresi (`since`) kullanılır (backfill/test için).
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
    from tenders.services import notify

    today = timezone.localdate()
    # Varsayılan (beat, since_days=1): yalnızca BUGÜN yayınlananlar (katı).
    # Elle geniş pencere (since_days>1): eski gevşek `since` (backfill/test).
    published_on = today if since_days <= 1 else None
    since = None if since_days <= 1 else (timezone.now() - timedelta(days=since_days))

    # Haritası eksik kalan profilleri önce onar — yoksa aşağıdaki exclude onları
    # kalıcı olarak eşleştirme dışında bırakır (bkz. _repair_missing_profile_maps).
    _repair_missing_profile_maps()

    profiles = (
        CompanyProfile.objects.filter(is_active=True)
        .exclude(profile_map__isnull=True)
        .select_related("user")
    )
    total_recs = 0
    skipped_free = 0

    for profile in profiles.iterator():
        # İhale Asistanı bildirimleri Pro'ya özeldir → Free üyeye öneri/digest/push YOK.
        if not profile.user.is_premium:
            skipped_free += 1
            continue
        try:
            matches = match_tenders_for_profile(profile, since=since, published_on=published_on)
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

        # Digest kendi konuşmasında yaşar → geçmiş sohbetler listesinde görünür,
        # kullanıcı içinden devam edip soru sorabilir. Bildirim bu sohbete bağlanır.
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

        # type=CHAT → mobilde tıklanınca ihale detayı değil, digest sohbeti açılır.
        # Uygulama-içi bildirim satırı + (pacing'li) tek push. Kullanıcı başına günde
        # tek öneri push'u (idem `digest:{uid}:{date}`) → bombardıman yok.
        notify.notify_and_push(
            profile.user,
            type=Notification.Type.CHAT,
            title=f"İhale Asistanı: {len(fresh)} yeni öneri",
            body=top_titles,
            tender_ikn=top[0][0].ikn if top else None,
            tender_title=top[0][0].ihale_adi[:500] if top else None,
            conversation_id=digest_conv.id,
            idem_key=f"digest:{profile.user_id}:{today.isoformat()}",
        )

    logger.info(
        "match_recommendations: %s profil işlendi, %s öneri üretildi, %s free atlandı",
        profiles.count(), total_recs, skipped_free,
    )
    return {
        "profiles": profiles.count(),
        "recommendations": total_recs,
        "skipped_free": skipped_free,
    }


@shared_task(name="assistant.tasks.expire_actions")
def expire_actions():
    """
    Süresi dolmuş önerileri işaretler ve eskileri siler.

    Uç zaten istek anında süreyi kontrol ediyor; bu görev kullanıcının hiç dokunmadığı
    kartların DURUMUNU düzeltir (mobil kartı pasif gösterebilsin) ve tabloyu şişmekten
    korur — kart başına bir satır, sohbet başına birkaç kart birikir.
    """
    from datetime import timedelta

    from assistant.models import AssistantAction

    simdi = timezone.now()
    doldu = AssistantAction.objects.filter(
        durum=AssistantAction.Durum.BEKLIYOR, expires_at__lt=simdi
    ).update(durum=AssistantAction.Durum.SURESI_DOLDU)
    silindi, _ = AssistantAction.objects.filter(created_at__lt=simdi - timedelta(days=90)).delete()
    if doldu or silindi:
        logger.info("expire_actions: %s süresi doldu, %s eski kayıt silindi", doldu, silindi)
    return {"suresi_doldu": doldu, "silinen": silindi}
