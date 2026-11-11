"""
Araç kullanan asistan döngüsü.

`chat_completion` (tek atış) yerine geçer: model araçları çağırır, sonuçları okur,
gerekirse yeniden çağırır ve sonunda kullanıcıya bir cümle yazar.

Neden SDK'nın `tool_runner` yardımcısı değil de elle döngü: her araç çağrısında
premium kontrolü, kart havuzu yan etkisi, token bütçesi ve denetim kaydı gerekiyor;
bunlar turlar arasına giren kararlar ve elle döngüde tek yerde duruyor.

⚠️ Prompt cache: system blok 1 (persona + veri tuzakları + profil haritası) byte-stabil
olmalı — `sort_keys=True`, tarih/değişken YOK. `tools` bloğu system'den ÖNCE render
edildiği için breakpoint araç şemalarını da kapsar (bkz. assistant/tools/__init__.py).
"""
import json
import logging
import time

from django.conf import settings

from ai.services.claude import AnalysisError, get_api_key

logger = logging.getLogger("ihaletakip")


class EkDosyaHatasi(AnalysisError):
    """
    İstek, Files API'deki bir dosyaya (ek) atıf yaptığı için reddedildi.

    Files API'ye göre süresi dolmuş ya da silinmiş dosyaya atıf yapan istek
    "inference öncesi" düşer — 400 ya da 404 gelebilir. Bu ayrı sınıf, çağıranın
    (`assistant.tasks._agent_yaniti`) ilgili ekleri işaretleyip bir kez eksiz yeniden
    denemesini sağlar. Aksi hâlde kullanıcı 400'ü "yapılandırma sorunu", 404'ü
    "AI servisi geçici olarak yanıt vermiyor" diye görürdü.
    """


def onbellek_isareti() -> dict:
    """
    `cache_control` değeri — system bloğu, son ek bloğu ve istek düzeyi aynı TTL'i kullanır.

    ⚠️ Varsayılan (5 dk) hâlinde `ttl` anahtarı YAZILMAZ: system bloğu bugünkü gibi
    byte-aynı kalsın, mevcut önbellek bir kez bile kırılmasın. 1 saat seçilirse TÜM
    noktalar 1 saat olur: uzun TTL'li giriş kısa olandan ÖNCE gelmek zorunda (system
    5 dk + ek 1 sa → 400). Sondaki otomatik nokta da aynı TTL'i taşır.
    """
    ttl = getattr(settings, "ASSISTANT_EK_CACHE_TTL", "5m")
    return {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}


def _dosya_hatasi_mi(e) -> bool:
    """400/404 bir Files API dosyasını mı işaret ediyor? (mesajda `file` geçer)"""
    return getattr(e, "status_code", None) in (400, 404) and "file" in str(e).lower()


# Adaptive thinking + `output_config.effort` YALNIZCA 4.6 ve sonrası modellerde vardır.
# Haiku 4.5 / Sonnet 4.5 gibi eski modellerde ikisi de **400** döndürür — üstelik hata
# "AI servisi geçici olarak yanıt vermiyor" gibi görünür ve saatlerce yanlış yerde aranır.
# (Ölçüldü: ASSISTANT_AGENT_MODEL=claude-haiku-4-5 ile üç sorunun üçü de 400.)
# Yeni model ailesi eklerken buraya da ekle.
_MODERN_AILE = ("opus-5", "opus-4-8", "opus-4-7", "opus-4-6",
                "sonnet-5", "sonnet-4-6", "fable-5", "mythos-5")


def _modelin_yetenekleri(model: str) -> dict:
    """Modele göre gönderilebilecek opsiyonel parametreler."""
    if any(ad in model for ad in _MODERN_AILE):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}
    return {}


def _tool_result(blok_id, icerik: dict):
    """Araç sonucunu API'nin beklediği bloğa çevirir."""
    return {
        "type": "tool_result",
        "tool_use_id": blok_id,
        "content": json.dumps(icerik, ensure_ascii=False, default=str),
        "is_error": not icerik.get("ok", True),
    }


def _guvenli_calistir(ctx, ad, girdi):
    """
    Aracı çalıştırır; ne olursa olsun bir sözlük döner.

    Araçtan sızan bir exception tüm sohbeti öldürürdü — model bunun yerine hata
    metnini okuyup kullanıcıya açıklayabilmeli ya da başka bir yol deneyebilmeli.
    """
    from assistant.tools import TOOL_IMPL, TOOL_SPECS

    fn = TOOL_IMPL.get(ad)
    if fn is None:
        return {"ok": False, "hata": f"Bilinmeyen araç: {ad}"}
    if not isinstance(girdi, dict):
        return {"ok": False, "hata": "Araç parametreleri sözlük olmalı."}

    # ⚠️ Bilinmeyen parametre SESSİZCE YOK SAYILMAZ, reddedilir.
    # Araçlar `**_` ile fazlalığı yutuyor; model `il_id` yerine `il` yazarsa filtre hiç
    # uygulanmaz ve araç TÜM TÜRKİYE'yi döndürür — model de "Ankara'da 2013 ihale var"
    # der. Bu bir limit değil, limit kılığına girmiş bir DOĞRULUK hatasıdır (aynı gerekçe
    # `ekap/views.py::_PRO_PARAMS` kapısında da yazılı: parametreyi yok saymak yerine
    # hata döndürülür). Modele doğru adları söylersek kendini düzeltir.
    spec = next((t for t in TOOL_SPECS if t["name"] == ad), None)
    if spec:
        bilinen = set(spec["input_schema"].get("properties") or {})
        bilinmeyen = sorted(set(girdi) - bilinen)
        if bilinmeyen:
            return {
                "ok": False,
                "hata": (
                    f"Şu parametreler bu araçta yok: {', '.join(bilinmeyen)}. "
                    f"Geçerli parametreler: {', '.join(sorted(bilinen))}."
                ),
            }
    try:
        sonuc = fn(ctx, **girdi)
    except TypeError as e:
        # Model şemada olmayan bir parametre uydurmuş olabilir — düzeltebilsin.
        logger.warning("araç %s geçersiz parametre: %s | %s", ad, girdi, e)
        return {"ok": False, "hata": f"Geçersiz parametre: {e}"}
    except Exception:
        logger.exception("araç %s çöktü: %s", ad, girdi)
        return {"ok": False, "hata": "Araç çalışırken bir sorun oldu."}
    return sonuc if isinstance(sonuc, dict) else {"ok": True, "sonuc": sonuc}


def _usage_topla(hedef: dict, usage) -> dict:
    """Turlar boyunca token kullanımını biriktirir (maliyet izleme, ChatMessage.usage)."""
    for alan in ("input_tokens", "output_tokens",
                 "cache_read_input_tokens", "cache_creation_input_tokens"):
        deger = getattr(usage, alan, None)
        if deger:
            hedef[alan] = hedef.get(alan, 0) + deger
    return hedef


def sohbet_turu(ctx, profile_map, context_text, messages, sistem_ek=""):
    """
    Bir kullanıcı mesajını araç döngüsüyle yanıtlar.

    Dönen: `{"metin": str, "usage": dict, "turlar": int, "arac_izi": [...]}`.
    Hata: `AnalysisError`.
    """
    import anthropic

    from assistant.prompts import AGENT_PERSONA_PROMPT, VERI_TUZAKLARI
    from assistant.tools import TOOL_SPECS

    client = anthropic.Anthropic(api_key=get_api_key())

    sabit = (
        AGENT_PERSONA_PROMPT
        + "\n\n"
        + VERI_TUZAKLARI
        + "\n\n## FİRMA PROFİLİ\n"
        + json.dumps(profile_map or {}, ensure_ascii=False, sort_keys=True)
        + (("\n\n" + sistem_ek) if sistem_ek else "")
    )
    system = [{"type": "text", "text": sabit, "cache_control": onbellek_isareti()}]
    # Ekli sohbette değişken bağlam son kullanıcı mesajına taşınır (bkz. tasks.py) ve
    # burada boş gelir. ⚠️ Boş metin bloğu GÖNDERİLMEZ — API reddeder.
    if context_text:
        system.append({"type": "text", "text": context_text})

    messages = list(messages)
    usage = {}
    arac_izi = []
    tur = 0
    basladi = time.monotonic()
    harcanan = 0

    ek = _modelin_yetenekleri(settings.ASSISTANT_AGENT_MODEL)
    # Dosya hatası yorumu yalnızca istekte Files API kaynağı varsa: "file" alt dizesi
    # ("profile") eksiz bir 400'ü yanlışlıkla "ek dosyası okunamıyor"a çevirmesin.
    dosya_var = any(
        isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and (b.get("source") or {}).get("type") == "file"
                for b in m["content"])
        for m in messages
    )

    def _cagir(tools, tool_choice=None):
        try:
            return client.messages.create(
                model=settings.ASSISTANT_AGENT_MODEL,
                max_tokens=settings.ASSISTANT_MAX_TOKENS,
                system=system,
                messages=messages,
                tools=tools,
                # İstek düzeyi (otomatik) önbellek noktası: araç döngüsündeki her çağrı
                # bir öncekinin önekini okur. Bugüne kadar yalnızca system önbellekliydi
                # → 7 araç çağrısının her biri tüm mesajları tam fiyattan işliyordu.
                # Toplam nokta: system + son ek + bu = 3 (sınır 4).
                cache_control=onbellek_isareti(),
                **({"tool_choice": tool_choice} if tool_choice else {}),
                **ek,
            )
        except anthropic.APIError as e:
            code = getattr(e, "status_code", None)
            if dosya_var and _dosya_hatasi_mi(e):
                logger.warning("Asistan isteği bir ek dosyası yüzünden reddedildi (%s): %s", code, e)
                raise EkDosyaHatasi(
                    "Eklediğiniz dosyalardan biri artık okunamıyor.", status=410
                ) from e
            # ⚠️ 400 GEÇİCİ DEĞİLDİR — istekte bir hata var (desteklenmeyen parametre,
            # geçersiz model kimliği, bozuk mesaj dizisi). "Tekrar deneyin" demek
            # kullanıcıyı da bizi de yanlış yönlendirir; hatanın kendisini logla.
            if code == 400:
                logger.error(
                    "Asistan isteği reddedildi (400) — model=%s ek=%s: %s",
                    settings.ASSISTANT_AGENT_MODEL, list(ek), e,
                )
                raise AnalysisError(
                    "Asistan yapılandırmasında bir sorun var; ekibimiz bilgilendirildi.",
                    status=502,
                ) from e
            logger.warning("Asistan Claude API hatası (%s): %s", code or "bağlantı", e)
            raise AnalysisError(
                f"AI servisi geçici olarak yanıt vermiyor ({code or 'bağlantı'}).", status=502
            ) from e

    while True:
        resp = _cagir(TOOL_SPECS)
        _usage_topla(usage, resp.usage)

        if resp.stop_reason == "refusal":
            raise AnalysisError("İsteğinizi bu şekilde yanıtlayamıyorum.", status=422)

        # ⚠️ thinking blokları DAHİL, olduğu gibi geri konur; aksi hâlde model
        # kendi akıl yürütmesini kaybeder ve sonraki turda tutarsızlaşır.
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            break

        tur += 1
        bloklar = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]

        butce_doldu = (
            tur > settings.ASSISTANT_MAX_TURNS
            or (time.monotonic() - basladi) > settings.ASSISTANT_WALL_SECONDS
            or harcanan > settings.ASSISTANT_TOOL_TOKEN_BUDGET
        )

        # Her turda sıfırla: "son grup" = bu turda çağrılan araçların ürettiği kartlar.
        ctx.son_grup = []
        sonuclar = []
        for blok in bloklar:
            if butce_doldu:
                icerik = {
                    "ok": False,
                    "hata": (
                        "Bu soru için ayrılan arama hakkı doldu. Yeni araç çağırma; "
                        "elindeki bilgiyle cevabını tamamla. Kullanıcıya 'bütçe' ya da "
                        "'araç' deme — doğrulayamadığın kısmı 'şunu doğrulayamadım' diye söyle."
                    ),
                }
            else:
                icerik = _guvenli_calistir(ctx, blok.name, blok.input)
                # `param` de kaydedilir: `assistant/tasks.py` son başarılı aramayı
                # konuşmaya yazıp bir sonraki turda bağlama koyuyor (takip soruları).
                liste = icerik.get("liste")
                arac_izi.append({
                    "arac": blok.name,
                    "ok": bool(icerik.get("ok", True)),
                    "param": dict(blok.input or {}),
                    # Boş sonuç ≠ hata; "bulamadım" cevaplarının kaynağını ayırt eder.
                    "n": len(liste) if isinstance(liste, list) else None,
                })
            blok_json = _tool_result(blok.id, icerik)
            harcanan += len(blok_json["content"]) // 3
            sonuclar.append(blok_json)

        # ⚠️ TÜM tool_result'lar TEK user mesajında olmalı; bölünürse model paralel
        # araç kullanmayı bırakır (her turda tek araç çağırır, maliyet katlanır).
        messages.append({"role": "user", "content": sonuclar})

        if butce_doldu:
            # Kapanış turu: model elindekiyle cümleyi yazsın.
            # ⚠️ Parametreler döngüdeki çağrıyla BİREBİR AYNI (aynı araçlar, `tool_choice`
            # yok). Eskiden `tools=[]` gönderiliyordu → araç, system ve mesaj önbelleğinin
            # hepsi bozuluyordu. `tool_choice: none` da çözüm DEĞİL: araç + system
            # önbelleğini korur ama MESAJ önbelleğini (yani sohbete eklenen dokümanı)
            # geçersiz kılar. Son tool_result'lar zaten "yeni araç çağırma" diyor.
            resp = _cagir(TOOL_SPECS)
            _usage_topla(usage, resp.usage)
            if resp.stop_reason == "tool_use":
                # Nadir: model yine araç istedi. tool_use bloğu sonuçsuz geri konamaz
                # (API reddeder) → aynı mesajlarla, araç kullanımı kapalı ikinci kapanış.
                # Önbellek bedeli bu nadir durumda kabul edilir.
                resp = _cagir(TOOL_SPECS, tool_choice={"type": "none"})
                _usage_topla(usage, resp.usage)
            if resp.stop_reason == "refusal":
                raise AnalysisError("İsteğinizi bu şekilde yanıtlayamıyorum.", status=422)
            messages.append({"role": "assistant", "content": resp.content})
            break

    metin = "".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    usage["model"] = settings.ASSISTANT_AGENT_MODEL
    usage["tur_sayisi"] = tur
    if not metin:
        metin = "Yanıt üretemedim, sorunuzu biraz farklı sorabilir misiniz?"
    return {"metin": metin, "usage": usage, "turlar": tur, "arac_izi": arac_izi}
