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


def _digest_context(conversation):
    """
    Günlük öneri (digest) sohbetinde modele verilecek öneri listesi bağlamı.
    Dönen: `(metin, ikn_listesi)` ya da digest değilse / kart yoksa `None`.

    ⚠️ Bu olmadan model önerileri HİÇ görmez: öneriler asistan mesajının
    `payload.tender_cards`'ındadır, geçmiş ise yalnızca metindir ve
    `build_chat_messages` baştaki asistan mesajını (digest mesajı tam orada) atar.
    Kullanıcı "2. ihale uygun mu?" deyince model hangi ihaleden söz edildiğini
    bilmiyordu (2026-10-14).
    """
    from assistant.models import ChatConversation, ChatMessage

    if not conversation or conversation.kind != ChatConversation.Kind.DIGEST:
        return None
    msg = (
        ChatMessage.objects.filter(
            conversation=conversation,
            role=ChatMessage.Role.ASSISTANT,
            payload__kind="digest",
        )
        .order_by("created_at")
        .first()
    )
    kartlar = [k for k in ((msg.payload or {}).get("tender_cards") or []) if k.get("ikn")] if msg else []
    if not kartlar:
        return None

    satirlar = ["## BU SOHBETTEKİ GÜNLÜK ÖNERİLER (sohbetin başında kullanıcıya gösterildi)"]
    for i, k in enumerate(kartlar, 1):
        parcalar = [k.get("ikn"), k.get("ihale_adi"), k.get("idare_adi"), k.get("il")]
        satir = f"{i}. " + " · ".join(p for p in parcalar if p)
        if k.get("ihale_tarihi"):
            satir += f" · ihale tarihi {k['ihale_tarihi']}"
        if k.get("gerekce"):
            satir += f"\n   Seçilme gerekçesi: {k['gerekce']}"
        satirlar.append(satir)
    satirlar.append(
        "Kullanıcı bunlara sıra numarasıyla (\"2. ihale\"), adıyla, idaresiyle, iliyle "
        "ya da İKN'siyle atıf yapabilir. Hangisini kastettiğini bu listeden çöz; "
        "ayrıntı gerekiyorsa o İKN'yi `ihale_detay` ile getir. Cevapta söz ettiğin "
        "ihalenin İKN'sini yaz."
    )
    return "\n".join(satirlar), [k["ikn"] for k in kartlar]


def _selected_tender_context(tender, today, ek_acik=False) -> str:
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
        f"- e-İhale: {'Evet' if tender.e_ihale else 'Hayır'}",
    ]
    if okas:
        lines.append(f"- OKAS ihtiyaç kalemleri: {okas}")
    # Kazanan firma: "kim aldı / kim yapıyor" ihale odaklı sohbetin en sık sorusu.
    from assistant.tools.read import _ihale_sozlesmeleri

    sozlesmeler = _ihale_sozlesmeleri(tender)
    if sozlesmeler:
        lines.append("- İmzalanmış sözleşmeler (kazanan firma):")
        for s in sozlesmeler:
            lines.append(
                f"  • {s['yuklenici'] or '-'} | bedel {s['sozlesme_bedeli'] or 'veri yok'} ₺"
                f" | tarih {s['sozlesme_tarihi'] or '-'}"
            )
    else:
        lines.append("- İmzalanmış sözleşme: henüz yok (ihale sonuçlanmamış ya da sonuç ilanı gelmemiş)")
    lines.append(
        "\nKullanıcı bu ihale hakkında soru soruyor (uygunluk, maliyet/keşif yaklaşımı, "
        "yeterlilik, teklif stratejisi vb.). Yukarıdaki bilgilere ve firmanın profiline "
        "göre yanıtla. Kesin rakam TAAHHÜT ETME; maliyet için genel yaklaşım ver ve "
        "detaylı hesap için uygulamanın 'Maliyet Analizi' özelliğine yönlendir. İhale "
        "metninde OLMAYAN bilgiyi uydurma; eksikse "
        + ("kullanıcı ilgili dokümanı mesaj kutusunun solundaki + düğmesiyle sohbete "
           "ekleyebilir (EKAP'a DEĞİL). " if ek_acik else
           "uygulamadaki Dokümanlar ekranına yönlendir (EKAP'a DEĞİL). ")
        + 
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
    Araç kullanan yanıt yolu. Dönen: `(reply_metni, kartlar, usage, bloklar)`.

    ⚠️ Anahtar kelime yönlendirmesi (eski `_wants_tender_listing` / `_wants_saved`)
    BURADA YOK ve geri eklenmemeli: model hangi aracı çağıracağına kendi karar veriyor.
    Substring tetikleyicileri modelle çelişir — kullanıcı "bugünkü ihaleler neler" derken
    aslında kayıtlılarını kastediyorsa tetikleyici yanlış dalı seçer, model seçmez.
    """
    from assistant.services.agent import sohbet_turu
    from assistant.services.chat import build_chat_messages
    from assistant.tools.context import ToolContext
    from ekap.models import Tender

    from assistant.services.ekler import ek_etkin, kullanilabilir_mi, sohbet_ekleri

    yetenekler = (user_msg.payload or {}).get("yetenekler") or []
    ek_acik = "ek" in yetenekler and ek_etkin(profile.user)
    ctx = ToolContext(
        user=profile.user,
        premium=bool(getattr(profile.user, "is_premium", False)),
        conversation=conversation,
        ek_acik=ek_acik,
    )

    baglam = [f"Bugünün tarihi: {today.strftime('%d.%m.%Y')}",
              f"EK GÖNDERME: {'AÇIK' if ek_acik else 'KAPALI'}"]

    # Sohbet ekleri (docs/plan-asistan-ekler.md 1.6). Süresi dolanlar modele gitmez,
    # yalnızca not düşülür — model "okuyamıyorum" desin, uydurmasın.
    tum_ekler = list(sohbet_ekleri(conversation)) if conversation else []
    if tum_ekler and not ek_etkin(profile.user):
        # Kapatma anahtarı (`ASSISTANT_EK_KAPSAM=kapali`) BAĞLI ekleri de durdurur: geri
        # alma "dosya modele gitmesin" demektir (ör. hazırlama hatası fark edildi).
        ekler, dolan = [], []
        baglam.append("Bu sohbetteki dosya ekleri şu an kapalı; içeriklerini okuyamazsın. "
                      "Sorulursa bunu söyle, içerik uydurma.")
    else:
        ekler = [e for e in tum_ekler if kullanilabilir_mi(e)]
        dolan = [e for e in tum_ekler if e not in ekler]
    ctx.ekler = [{"sira": e.sira, "ad": e.ad, "tur": e.tur, "parca": e.parca} for e in ekler]
    if dolan:
        baglam.append(_dolan_ek_notu(dolan))

    # Seçili ihale (ihale odaklı sohbet): detayı ÖN ÇAĞRIYLA bağlama koy — modelin
    # ilk turu bunu aramakla harcamasın (tur = en pahalı değişken).
    if conversation and conversation.tender_ikn:
        t = Tender.objects.filter(ikn=conversation.tender_ikn).first()
        if t:
            baglam.append(_selected_tender_context(t, today, ek_acik=ek_acik))
            ctx.kart_ekle(t)

    # Günlük öneri sohbeti: önerileri bağlama koy ve kart havuzuna ekle — cevapta
    # anılan öneri araç çağrısı olmadan da karta dönüşür (havuz garantisi korunur).
    digest = _digest_context(conversation)
    if digest:
        metin, ikn_listesi = digest
        baglam.append(metin)
        tenders = {t.ikn: t for t in Tender.objects.filter(ikn__in=ikn_listesi)}
        for ikn in ikn_listesi:
            if ikn in tenders:
                ctx.kart_ekle(tenders[ikn])
        # Havuz kartları "son grup" olarak da işaretlenir; model araç çağırmadan
        # genel bir cevap verirse 5 kartı tekrar basmayalım.
        ctx.son_grup = [i for i in ctx.son_grup if i not in ikn_listesi]

    # Önceki oturumlarda konuşulan ihaleler (hafif hafıza).
    onceki = _onceki_oturum_baglami(profile.user, conversation)
    if onceki:
        metin, onceki_ihaleler = onceki
        baglam.append(metin)
        for t in onceki_ihaleler:
            ctx.kart_ekle(t)
        # Model araç çağırmadan genel cevap verirse bu kartları yeniden basmayalım.
        anahtarlar = {t.ikn for t in onceki_ihaleler}
        ctx.son_grup = [i for i in ctx.son_grup if i not in anahtarlar]

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

    etiketler = {}
    for e in tum_ekler:
        if e.message_id and e.sira:
            etiketler.setdefault(e.message_id, []).append(f"Ek {e.sira}")
    messages = build_chat_messages(
        profile.user, conversation=conversation,
        ek_etiketleri={m: f"[Bu mesajla gönderilen ekler: {', '.join(v)}]"
                       for m, v in etiketler.items()},
    )
    if not messages:
        # ⚠️ 4 değer: çağıran (`assistant_chat_task`) dört değer açıyor; eskiden burada
        # 3 değer dönüyordu ve bu dal ValueError ile "çökme" oluyordu.
        return None, [], None, []

    # Hitap CACHE'Lİ bloğa gider (`sistem_ek`): kullanıcı başına sabittir, turdan
    # tura değişmez — değişken bağlamla (tarih, son arama) karıştırılırsa her
    # mesajda cache önekini bozardı.
    hitap = profile.hitap_metni()
    sistem_ek = (
        f"## KULLANICIYA HİTAP\nKullanıcıya “{hitap}” diye hitap et."
        if hitap else ""
    )

    from assistant.services.agent import EkDosyaHatasi

    try:
        sonuc = sohbet_turu(ctx, profile.profile_map,
                            *_ekli_istek(messages, ekler, baglam), sistem_ek=sistem_ek)
    except EkDosyaHatasi:
        # Süresi dolmuş/silinmiş Files API dosyası: BİR KEZ onlarsız dene.
        from assistant.models import AssistantAttachment

        kesin = _bozuk_ekler(ekler)
        # Yalnızca Files API'de gerçekten bulunamayan işaretlenir; geçici bir ağ hatası
        # sağlam bir eki "süresi doldu" yapmasın. Tespit edilemezse dosyalı ekler bu tur
        # için dışarıda kalır (aynı hata tekrar etmesin) ama işaretlenmez.
        disarida = kesin or [e for e in ekler if e.anthropic_file_id]
        if kesin:
            AssistantAttachment.objects.filter(id__in=[e.id for e in kesin]).update(
                durum="suresi_doldu", metin="", anthropic_file_id="")
        ekler = [e for e in ekler if e not in disarida]
        ctx.ekler = [x for x in ctx.ekler if x["sira"] not in {e.sira for e in disarida}]
        baglam.append(_dolan_ek_notu(disarida))
        sonuc = sohbet_turu(ctx, profile.profile_map,
                            *_ekli_istek(messages, ekler, baglam), sistem_ek=sistem_ek)
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
    from assistant.tools.write import AZAMI_KART

    # `dokuman_sec` kartı kendi yuvasında: grafik sınırına (2) takılmasın.
    secim = [b for b in ctx.gorsel_bloklar if b.get("type") == "dokuman_sec"][:1]
    gorsel = [b for b in ctx.gorsel_bloklar if b.get("type") != "dokuman_sec"][:2]
    bloklar = gorsel + secim + [_eylem_karti(e) for e in ctx.oneriler[:AZAMI_KART]]

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

    # Araç izi usage'a yazılır (iç veri, serializer'da YOK) → hangi soruda hangi
    # aracın boş/başarısız döndüğü sonradan ölçülebilsin (`manage.py asistan_rapor`).
    usage = dict(sonuc.get("usage") or {})
    usage["araclar"] = _iz_ozeti(sonuc.get("arac_izi"))
    if ekler:
        # Maliyet ölçümü: hangi ek, hangi modda, kaç token (cache okuma oranıyla birlikte okunur).
        usage["ekler"] = [{"id": str(e.id), "mod": e.mod, "token": e.token_sayisi} for e in ekler]
    return reply, kartlar, usage, bloklar


def _dolan_ek_notu(ekler) -> str:
    adlar = ", ".join(e.etiket() for e in ekler)
    return (f"Şu eklerin saklama süresi doldu, içerikleri artık okunamaz: {adlar}. "
            "Bu dosyalar sorulursa içeriğini UYDURMA; kullanıcıdan dosyayı yeniden "
            "eklemesini iste.")


def _ekli_istek(messages, ekler, baglam):
    """
    `sohbet_turu`'na giden (context_text, messages) çifti.

    Ek yoksa bugünkü gibi: bağlam system[1]'de, mesajlar metin. Ek varsa:
    - ek blokları `messages[0]`'ın BAŞINA (son ekte önbellek noktası). Önek araçlar +
      system + eklerdir; 12 mesajlık pencere kaysa da değişmez.
    - ⚠️ değişken bağlam system'den ÇIKARILIR ve son kullanıcı mesajında, eklerden SONRA
      ve sorudan hemen ÖNCE durur (`ASSISTANT_EK_BAGLAM_MESAJDA`). system'de kalsaydı
      değiştiği her soruda (son arama, İKN ipucu) doküman tam fiyatla yeniden işlenirdi.
      İlk turda `messages[0]` ile son mesaj aynıdır; bağlam yine eklerin ARKASINA gelir.
    """
    from assistant.services.agent import onbellek_isareti
    from assistant.services.attachments import ek_bloklari

    baglam_metni = "\n\n".join(baglam)
    if not ekler:
        return baglam_metni, messages
    mesajlar = [dict(m) for m in messages]
    mesajlar[0]["content"] = [*ek_bloklari(ekler, onbellek_isareti()),
                              {"type": "text", "text": mesajlar[0]["content"]}]
    if not settings.ASSISTANT_EK_BAGLAM_MESAJDA:
        return baglam_metni, mesajlar
    son = mesajlar[-1]
    baglam_blogu = {"type": "text", "text": f"<bağlam>\n{baglam_metni}\n</bağlam>"}
    if isinstance(son["content"], list):
        son["content"] = [*son["content"][:-1], baglam_blogu, son["content"][-1]]
    else:
        son["content"] = [baglam_blogu, {"type": "text", "text": son["content"]}]
    return "", mesajlar


def _bozuk_ekler(ekler) -> list:
    """Files API'de KESİN olarak bulunmayan ya da süresi geçmiş ekler (404 / geçmiş `expires_at`)."""
    import anthropic

    from assistant.services import attachments as ek_servis

    bozuk = []
    try:
        istemci = ek_servis._istemci()
        for e in (x for x in ekler if x.anthropic_file_id):
            try:
                meta = istemci.files.retrieve_metadata(e.anthropic_file_id)
            except anthropic.NotFoundError:
                bozuk.append(e)
                continue
            except Exception:
                continue
            bitis = getattr(meta, "expires_at", None)
            if bitis is not None and bitis <= timezone.now():
                bozuk.append(e)
    except Exception:
        logger.warning("ek dosyaları doğrulanamadı")
    return bozuk


ONCEKI_OTURUM_SAAT = 48
ONCEKI_OTURUM_IHALE = 5


def _onceki_oturum_baglami(user, conversation):
    """
    Kullanıcının SON 48 SAATTE başka oturumlarda konuştuğu ihaleler + son sorusu.

    Mobil her açılışta yeni sohbet başlatıyor; kullanıcı "az önceki konuya dönemiyor
    musun", "ambar işi var yazmıştın" diyordu ve asistan "hiç mesajlaşmadık" cevabı
    veriyordu (sohbet analizi 2026-11-04). Bir kullanıcı aynı ihaleyi günlerce ~15
    kez yeniden sordu. İhaleler kart havuzuna da girer → "onu kaydet" çalışır.

    ⚠️ Değişken bağlamdır (cache'li sistem bloğuna konmaz). Dönen: `(metin, [Tender])`
    ya da None. Hata sohbeti asla düşürmez.
    """
    from datetime import timedelta

    from assistant.models import ChatMessage
    from ekap.models import Tender

    try:
        qs = ChatMessage.objects.filter(
            user=user, created_at__gte=timezone.now() - timedelta(hours=ONCEKI_OTURUM_SAAT),
        )
        if conversation is not None:
            qs = qs.exclude(conversation=conversation)
        iknler, son_soru = [], None
        for m in qs.order_by("-created_at").only("role", "content", "payload")[:40]:
            if m.role == ChatMessage.Role.USER and son_soru is None:
                son_soru = (m.content or "").strip()[:150]
            # Günlük öneri mesajı "konuşulan" değildir (her gün 5 kart) — gürültü olurdu.
            if (m.payload or {}).get("kind") == "digest":
                continue
            for k in (m.payload or {}).get("tender_cards") or []:
                ikn = k.get("ikn") if isinstance(k, dict) else None
                if ikn and ikn not in iknler:
                    iknler.append(ikn)
        iknler = iknler[:ONCEKI_OTURUM_IHALE]
        if not iknler and not son_soru:
            return None
        sozluk = {t.ikn: t for t in Tender.objects.filter(ikn__in=iknler).defer(
            "detail_raw", "list_raw")}
        ihaleler = [sozluk[i] for i in iknler if i in sozluk]
        satirlar = ["## ÖNCEKİ OTURUMLAR (son 48 saat, başka sohbet pencereleri)"]
        if son_soru:
            satirlar.append(f"- Kullanıcının son sorusu: “{son_soru}”")
        for t in ihaleler:
            satirlar.append(f"- {t.ikn} · {(t.ihale_adi or '')[:90]} · {(t.idare_adi or '')[:60]}")
        satirlar.append(
            "Kullanıcı \"az önceki\", \"daha önce yazmıştın\", \"o ihale\" derse bunlara bak. "
            "Konu bunlarla ilgili değilse bu bölümü YOK SAY ve kendiliğinden anma."
        )
        return "\n".join(satirlar), ihaleler
    except Exception:
        logger.exception("önceki oturum bağlamı kurulamadı (user=%s)", getattr(user, "pk", None))
        return None


def _hatayi_isaretle(user_msg, kod):
    """
    Cevapsız kalan kullanıcı mesajını işaretler (`payload.hata`).

    Hata dalı asistan mesajı YAZMADIĞI için, işaret olmadan bu mesajlar "cevap
    üretilmedi" diye ayırt edilemiyordu (2026-11-04 sohbet analizi elle yapıldı).
    Defter tutmadır — kendi hatası görevi asla düşürmez.
    """
    try:
        user_msg.payload = {**(user_msg.payload or {}), "hata": kod}
        user_msg.save(update_fields=["payload"])
    except Exception:
        logger.exception("hata işareti yazılamadı (mesaj=%s)", getattr(user_msg, "pk", None))


def _iz_ozeti(arac_izi):
    """Araç izini usage'a sığacak hâle getirir (uzun metin parametreleri kesilir)."""
    ozet = []
    for a in arac_izi or []:
        param = {
            k: (v[:80] if isinstance(v, str) else v)
            for k, v in (a.get("param") or {}).items()
            if not isinstance(v, (dict, list)) or len(str(v)) <= 200
        }
        ozet.append({"arac": a.get("arac"), "ok": a.get("ok"), "param": param,
                     **({"n": a["n"]} if a.get("n") is not None else {})})
    return ozet


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
            _hatayi_isaretle(user_msg, "sure_asimi")
            return _save(
                "Bu soruyu yanıtlamak beklenenden uzun sürdü. Biraz daha dar bir "
                "soruyla tekrar dener misiniz?",
                [],
            )
        except AnalysisError as e:
            _hatayi_isaretle(user_msg, f"ai_{e.status or 'hata'}")
            return {"success": False, "error": e.message}
        except Exception:
            logger.exception("assistant_chat_task araç yolu çöktü (user=%s)", user_id)
            _hatayi_isaretle(user_msg, "cokme")
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
def match_recommendations(since_days=1, kuru=False):
    """
    Günlük İhale Asistanı önerisi — **09:00, yalnızca Pro**.

    ⚠️⚠️ 2026-10-06'dan beri aday kümesi **o sabahki filtre bildiriminin bulduğu
    ihalelerdir** (kullanıcı kararı); yapay zekâ bu kümeden firma profiline en uygun
    olanları seçer (`assistant/services/oneri_secim.py`). Eskiden 07:00'de bugün
    yayımlanan TÜM ihaleler kural tabanlı puanlanıyordu; kontrolde iki hata çıkmıştı:
    kelime parçası eşleşmesi ("köprü" → "Uzunköprü", "Kron Köprü ve Protez") ve
    "8 yeni öneri" deyip 5 kart göstermek.

    Filtre bildirimi yoksa (filtresi yok / o gün eşleşme yok) → **bildirim yok**.
    Bildirimdeki sayı = sohbetteki kart sayısı.

    `kuru=True`: hiçbir şey yazmaz, kullanıcı başına seçimi döndürür (doğrulama).
    `since_days` geriye uyumluluk için duruyor, kullanılmaz.
    """
    from assistant.models import (
        ChatConversation,
        ChatMessage,
        CompanyProfile,
        TenderRecommendation,
    )
    from assistant.services import oneri_secim
    from assistant.services.matching import tender_card
    from tenders.models import Notification
    from tenders.services import notify

    today = timezone.localdate()
    if not kuru:
        _repair_missing_profile_maps()

    profiles = (
        CompanyProfile.objects.filter(is_active=True)
        .exclude(profile_map__isnull=True)
        .select_related("user")
    )
    sayac = {"profiles": 0, "recommendations": 0, "skipped_free": 0,
             "filtre_yok": 0, "secim_yok": 0, "yedek_kural": 0}
    rapor = []

    for profile in profiles.iterator():
        sayac["profiles"] += 1
        user = profile.user
        # İhale Asistanı bildirimleri Pro'ya özeldir.
        if not user.is_premium:
            sayac["skipped_free"] += 1
            continue
        if not oneri_secim.profil_yeterli(profile):
            sayac["profil_eksik"] = sayac.get("profil_eksik", 0) + 1
            continue
        try:
            bildirim = oneri_secim.bugunku_filtre_bildirimi(user, today)
            if bildirim is None:
                sayac["filtre_yok"] += 1
                continue
            filtre_idler, pencere = bildirim
            onceki = TenderRecommendation.objects.filter(user=user).values_list(
                "tender_id", flat=True)
            aday = oneri_secim.adaylar(user, filtre_idler, pencere,
                                       haric_ihale_idleri=onceki)
            secim, yontem = oneri_secim.sec(profile, aday)
            if yontem == "kural":
                sayac["yedek_kural"] += 1
            rapor.append({"uid": user.id, "aday": len(aday), "yontem": yontem,
                          "secim": [(t.ikn, t.ihale_adi[:70], g) for t, g in secim]})
            if not secim:
                sayac["secim_yok"] += 1
                continue
            if kuru:
                continue

            TenderRecommendation.objects.bulk_create(
                [
                    TenderRecommendation(
                        user=user, tender=t, score=float(len(secim) - i),
                        reasons=[g] if g else [], date=today,
                    )
                    for i, (t, g) in enumerate(secim)
                ],
                ignore_conflicts=True,
            )
            sayac["recommendations"] += len(secim)
            n = len(secim)

            # ⚠️ Kartlar = seçilenlerin TAMAMI; başlıktaki sayı da bu (eskiden 8 deyip 5
            # kart gösteriyordu, kalan 3 hiçbir yerde görünmüyordu).
            kartlar = []
            for t, g in secim:
                kart = tender_card(t)
                if g:
                    kart["gerekce"] = g
                kartlar.append(kart)
            digest_conv = ChatConversation.objects.create(
                user=user,
                title=f"Günlük Öneriler · {today.strftime('%d.%m.%Y')}",
                kind=ChatConversation.Kind.DIGEST,
            )
            ChatMessage.objects.create(
                user=user,
                conversation=digest_conv,
                role=ChatMessage.Role.ASSISTANT,
                content=(
                    f"Günaydın! Bugün filtrelerinize uyan ihalelerden firmanıza en uygun "
                    f"{n} tanesini seçtim. Detaylarını sorabilirsiniz."
                ),
                payload={"kind": "digest", "tender_cards": kartlar},
            )
            # type=CHAT → mobilde digest sohbeti açılır (değişmedi).
            notify.notify_and_push(
                user,
                type=Notification.Type.CHAT,
                title=f"İhale Asistanı: {n} öneri",
                body="\n".join(f"• {t.ihale_adi[:80]}" for t, _ in secim[:3]),
                tender_ikn=secim[0][0].ikn,
                tender_title=secim[0][0].ihale_adi[:500],
                conversation_id=digest_conv.id,
                idem_key=f"digest:{user.id}:{today.isoformat()}",
            )
        except Exception:
            logger.exception("match_recommendations: kullanıcı %s işlenemedi", user.id)
            continue

    logger.info("match_recommendations: %s", sayac)
    if kuru:
        sayac["rapor"] = rapor
    return sayac


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


# ── Asistan ekleri (docs/plan-asistan-ekler.md 1.5, 1.10) ─────────────────────
# ⚠️ Üçü de AYRI `assistant_ek` kuyruğunda (`assistant-ek-worker`): hazırlama 90 sn'ye
# kadar sürer ve alt süreç çalıştırır; sohbet kuyruğunda kalsaydı kullanıcının
# sohbetini bekletirdi. Ham dosya dizinini (`ASSISTANT_EK_DIR`) yalnızca web ve bu
# worker görür.

def _ek_dizininde_mi(yol) -> bool:
    import os

    if not yol:
        return False
    kok = os.path.realpath(settings.ASSISTANT_EK_DIR)
    return os.path.realpath(yol).startswith(kok + os.sep)


def ham_dosyayi_sil(yol) -> None:
    """Ek dizinindeki ham dosyayı siler; dizin dışındaki bir yola ASLA dokunmaz."""
    import os

    if not _ek_dizininde_mi(yol):
        return
    try:
        os.remove(yol)
    except FileNotFoundError:
        pass


@shared_task(name="assistant.tasks.ek_hazirla_task",
             soft_time_limit=settings.ASSISTANT_EK_SURE_SN,
             time_limit=settings.ASSISTANT_EK_SURE_SN + 30)
def ek_hazirla_task(ek_id):
    """
    Ham dosyayı metne çevirir ya da Files API'ye yükler; sonucu satıra yazar.

    Görev ÇÖKMEZ: her hata `durum="hata"` + `hata_kodu` olarak yazılır. Bizden
    kaynaklanan hatada (`okunamadi`, zaman aşımı) günlük hak iade edilir.
    ⚠️ Doküman içeriği loglanmaz. Ham dosya başarıda da hatada da silinir.
    """
    from assistant.models import AssistantAttachment
    from assistant.services import attachments as ek_servis

    ek = AssistantAttachment.objects.filter(id=ek_id).first()
    if not ek or ek.durum != AssistantAttachment.Durum.HAZIRLANIYOR:
        return {"atlandi": "yok_ya_da_hazirlanmis"}
    canli = AssistantAttachment.objects.filter(id=ek.id, durum="hazirlaniyor")

    def _hata(kod, sayfa=None, iade=False):
        from assistant.services.ekler import kota_iade

        if iade and canli.filter(kota_sayildi=True).update(kota_sayildi=False):
            kota_iade(ek.user_id, ek.tur, ek.created_at)
        alanlar = {"durum": "hata", "hata_kodu": kod, "metin": "", "depo_yolu": ""}
        if sayfa:
            alanlar["sayfa_sayisi"] = sayfa
        canli.update(**alanlar)
        return {"durum": "hata", "hata_kodu": kod}

    try:
        alanlar = ek_servis.hazirla(ek)
    except ek_servis.EkHatasi as e:
        return _hata(e.kod, e.sayfa_sayisi, iade=e.bizden)
    except SoftTimeLimitExceeded:
        logger.warning("ek hazırlama süre aşımı (ek=%s)", ek_id)
        return _hata("okunamadi", iade=True)
    except Exception:
        logger.exception("ek hazırlama çöktü (ek=%s)", ek_id)
        return _hata("okunamadi", iade=True)
    finally:
        ham_dosyayi_sil(ek.depo_yolu)

    # ⚠️ `save()` DEĞİL koşullu UPDATE: kullanıcı ek hazırlanırken tepsiden çıkardıysa
    # satır silinmiştir; `save()` onu yeniden yaratırdı.
    guncellendi = canli.update(**alanlar, durum="hazir", hata_kodu="", depo_yolu="",
                               hazir_at=timezone.now())
    if not guncellendi and alanlar.get("anthropic_file_id"):
        try:
            ek_servis.dosya_sil(ek_servis._istemci(), alanlar["anthropic_file_id"])
        except Exception:
            logger.warning("silinen ekin Files API dosyası kaldırılamadı (ek=%s)", ek_id)
    return {"durum": "hazir" if guncellendi else "silindi", "mod": alanlar.get("mod")}


@shared_task(name="assistant.tasks.ek_dosyalarini_sil_task", bind=True, max_retries=3)
def ek_dosyalarini_sil_task(self, file_ids, yollar):
    """
    Silinen eklerin ham dosyalarını ve Files API dosyalarını kaldırır.

    ⚠️ Bir Files API dosyasını başka bir satır (tekilleştirme kopyası) hâlâ
    kullanıyorsa SİLİNMEZ.
    """
    from assistant.models import AssistantAttachment
    from assistant.services import attachments as ek_servis

    for yol in yollar or []:
        ham_dosyayi_sil(yol)
    silinecek = [f for f in (file_ids or [])
                 if f and not AssistantAttachment.objects.filter(anthropic_file_id=f).exists()]
    if not silinecek:
        return {"silinen": 0}
    try:
        istemci = ek_servis._istemci()
        for f in silinecek:
            ek_servis.dosya_sil(istemci, f)
    except Exception as e:
        logger.warning("ek Files API silme başarısız, yeniden denenecek: %s", type(e).__name__)
        raise self.retry(exc=e, countdown=300)
    return {"silinen": len(silinecek)}


def takili_hazirlamalari_kapat(simdi=None) -> int:
    """
    30 dakikadan uzun "hazırlanıyor" kalan ek → hata + hak iadesi + ham dosya silinir.

    Görev sert süre sınırında SIGKILL ile ölürse (`finally` çalışmaz) ya da worker
    düşerse satır askıda kalır, hakkı yer ve istemci boşuna yoklar. Kuyruk beklemesi
    payı için eşik görev süresinin çok üstünde.
    """
    from datetime import timedelta

    from assistant.models import AssistantAttachment
    from assistant.services.ekler import kota_iade

    simdi = simdi or timezone.now()
    takili = list(AssistantAttachment.objects.filter(
        durum="hazirlaniyor", created_at__lt=simdi - timedelta(minutes=30)))
    for ek in takili:
        ham_dosyayi_sil(ek.depo_yolu)
        if ek.kota_sayildi:
            kota_iade(ek.user_id, ek.tur, ek.created_at)
    return AssistantAttachment.objects.filter(
        id__in=[e.id for e in takili], durum="hazirlaniyor"
    ).update(durum="hata", hata_kodu="okunamadi", depo_yolu="", kota_sayildi=False)


def sahipsiz_ham_dosyalari_sil(simdi=None) -> int:
    """Ek dizininde 1 günden eski ve hiçbir satırın göstermediği ham dosyaları siler."""
    import os
    import time as _time

    from assistant.models import AssistantAttachment

    kok = settings.ASSISTANT_EK_DIR
    if not os.path.isdir(kok):
        return 0
    bilinen = {os.path.realpath(y) for y in AssistantAttachment.objects.exclude(depo_yolu="")
               .values_list("depo_yolu", flat=True)}
    esik = _time.time() - 86400
    silinen = 0
    for ad in os.listdir(kok):
        yol = os.path.join(kok, ad)
        try:
            if (os.path.isfile(yol) and os.path.getmtime(yol) < esik
                    and os.path.realpath(yol) not in bilinen):
                ham_dosyayi_sil(yol)
                silinen += 1
        except OSError:
            continue
    return silinen


@shared_task(name="assistant.tasks.ek_takilanlari_temizle")
def ek_takilanlari_temizle():
    """Beat 10 dakikada bir: takılı hazırlamalar (günlük temizliği beklemesin)."""
    n = takili_hazirlamalari_kapat()
    if n:
        logger.warning("ek_takilanlari_temizle: %s takılı hazırlama kapatıldı", n)
    return {"takili": n}


@shared_task(name="assistant.tasks.ek_temizligi", soft_time_limit=240, time_limit=280)
def ek_temizligi(sure_sn=200):
    """
    Günlük (beat 03:40):
    0) 1 saatten uzun "hazırlanıyor" kalan ek (worker düştü) → hata + hak iadesi;
    1) süresi dolan ek → Files API dosyası açıkça SİLİNİR (süre dolumu metadata'yı 30 gün
       daha listede tutar), metin boşaltılır, `suresi_doldu`; satır kalır;
    2) 24 saati geçen bağlanmamış ek → satır silinir (sinyal dosyaları siler);
    3) yetim Files API dosyaları → YALNIZCA `ihaletakip-ek-` önekli, 24 saatten eski ve
       hiçbir satıra bağlı olmayanlar.
       ⚠️ Öneksiz ya da yeni dosyaya dokunulmaz: Files API workspace kapsamlıdır ve
       aynı API anahtarı başka dosyalar da tutabilir (ayrı anahtar açılmadı, kullanıcı
       kararı 2026-11-11); yaş eşiği yükleme ile kaydın yazılması arasındaki yarışı kapatır.
    """
    import time
    from datetime import timedelta

    from assistant.models import AssistantAttachment
    from assistant.services import attachments as ek_servis

    basla = time.monotonic()
    simdi = timezone.now()
    sonuc = {"takili": 0, "suresi_doldu": 0, "bagsiz": 0, "yetim": 0, "ham": 0}

    sonuc["takili"] = takili_hazirlamalari_kapat(simdi)
    sonuc["ham"] = sahipsiz_ham_dosyalari_sil(simdi)

    istemci = None
    dolan = list(AssistantAttachment.objects.filter(durum="hazir", expires_at__lte=simdi))
    file_ids = {e.anthropic_file_id for e in dolan if e.anthropic_file_id}
    sonuc["suresi_doldu"] = AssistantAttachment.objects.filter(
        id__in=[e.id for e in dolan]).update(durum="suresi_doldu", metin="", anthropic_file_id="")
    canli = set(AssistantAttachment.objects.filter(anthropic_file_id__in=file_ids)
                .values_list("anthropic_file_id", flat=True))
    try:
        for f in file_ids - canli:
            istemci = istemci or ek_servis._istemci()
            ek_servis.dosya_sil(istemci, f)
    except Exception as e:
        logger.warning("süresi dolan ek dosyaları silinemedi: %s", type(e).__name__)

    sonuc["bagsiz"], _ = AssistantAttachment.objects.filter(
        conversation__isnull=True, created_at__lt=simdi - timedelta(hours=24)).delete()

    if not settings.ASSISTANT_EK_YETIM_SUPURME:
        # ⚠️ Yalnızca üretimde açık: süpürme "bizim DB'mizin bilmediği önekli dosya =
        # yetim" der. Aynı anahtarla beat koşan başka bir ortam (yerel geliştirme)
        # açık olsaydı üretimin CANLI eklerini silerdi. Kapalıyken de dosyalar
        # yüklemedeki `expires_in_seconds` ile kendiliğinden düşer.
        if any(sonuc.values()):
            logger.info("ek_temizligi: %s", sonuc)
        return sonuc
    try:
        istemci = istemci or ek_servis._istemci()
        bilinen = set(AssistantAttachment.objects.exclude(anthropic_file_id="")
                      .values_list("anthropic_file_id", flat=True))
        esik = simdi - timedelta(hours=24)
        for f in istemci.files.list(limit=1000):
            if time.monotonic() - basla > sure_sn:
                break
            if not (getattr(f, "filename", "") or "").startswith(ek_servis.DOSYA_ONEKI):
                continue
            if f.created_at is None or f.created_at > esik or f.id in bilinen:
                continue
            ek_servis.dosya_sil(istemci, f.id)
            sonuc["yetim"] += 1
    except Exception as e:
        logger.warning("yetim ek süpürmesi yarım kaldı: %s", type(e).__name__)

    if any(sonuc.values()):
        logger.info("ek_temizligi: %s", sonuc)
    return sonuc
