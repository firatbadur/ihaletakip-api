"""Firma profil haritası üretimi — Claude'dan yapılandırılmış JSON çıkarır."""
import hashlib
import json
import logging

from ai.services.claude import AnalysisError, call_claude, get_api_key

from . import company_intel

logger = logging.getLogger("ihaletakip")

TENDER_TYPE_LABELS = {1: "Mal Alımı", 2: "Yapım", 3: "Hizmet", 4: "Danışmanlık"}

# ⚠️ 1500 İDİ ve çıktıyı kesiyordu → "geçersiz AI çıktısı" hatası.
# `CLAUDE_MODEL` (Sonnet 5) **adaptive thinking'i varsayılan olarak açık** çalıştırır
# ve düşünme token'ları da bu bütçeden düşer. Firma sözleşme geçmişi + web sitesi
# metni prompt'a eklendikten sonra model daha uzun düşünüyor, JSON yarıda kalıyordu.
# Bütçeyi düşürmeden önce `stop_reason` loglarına bak.
MAX_TOKENS = 8000


def parse_json_output(text: str) -> dict:
    """Model çıktısından JSON çıkarır: kod bloğu çitlerini temizler, ilk { .. son } arasını parse eder."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # ```json ... ``` çitlerini kaldır
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSON bulunamadı")
    return json.loads(cleaned[start : end + 1])


def _profile_text(profile) -> str:
    """
    CompanyProfile → prompt'a eklenecek okunabilir firma bilgisi.

    ⚠️ `profile_input_digest` bu metnin özetini alır: prompt'a giren HER ŞEY buradan
    geçmeli, aksi halde alan değişince harita yeniden üretilmez. Tek istisna web
    sitesi İÇERİĞİ — burada yalnızca URL geçer (içerik her üretimde canlı okunur,
    site değişti diye ücretli çağrı tetiklemek istemiyoruz).
    """
    # ⚠️ İl adları statik seed'den okunur, `ekap.models.City` tablosundan DEĞİL:
    # o tablo boş olabilir (bkz. ekap/views.py `_dagilim` aynı gerekçeyle böyle yapar).
    # Eskiden City sorgulanıyordu ve tablo boşken prompt'a "İlgilenilen iller: Tümü"
    # yazılıyordu — kullanıcı il seçmiş olsa bile.
    from ekap.constants import CITIES

    il_adlari = {ekap_il_id: ad for ekap_il_id, _plaka, ad, _big in CITIES}
    city_names = [il_adlari[i] for i in (profile.cities or []) if i in il_adlari]
    type_labels = [TENDER_TYPE_LABELS.get(t, str(t)) for t in (profile.tender_types or [])]

    lines = [f"- Firma adı: {profile.company_name}"]
    if profile.contractor_id:
        lines.append(f"- EKAP yüklenici kaydı: VAR (id: {profile.contractor_id})")
    if profile.website:
        lines.append(f"- Web sitesi: {profile.website}")
    if profile.il_id:
        lines.append(f"- Firmanın bulunduğu il: {il_adlari.get(profile.il_id, profile.il_id)}")
    lines += [
        f"- Sektör: {profile.sector or 'Belirtilmedi'}",
        f"- Faaliyet alanları: {profile.activity_areas or 'Belirtilmedi'}",
        f"- İlgilenilen iller: {', '.join(city_names) or 'Tümü'} (id: {profile.cities or []})",
        f"- İlgilenilen ihale türleri: {', '.join(type_labels) or 'Tümü'} (id: {profile.tender_types or []})",
    ]
    if profile.budget_min or profile.budget_max:
        lines.append(f"- Bütçe aralığı: {profile.budget_min or '-'} — {profile.budget_max or '-'} TL")
    if profile.past_works:
        lines.append("- Geçmiş işler (kullanıcının elle eklediği):")
        for w in profile.past_works:
            if isinstance(w, dict):
                # İKN aramalı format: {ikn, title, city, type}
                parts = [w.get("title") or ""]
                extra = " · ".join(x for x in [w.get("ikn"), w.get("city"), w.get("type")] if x)
                lines.append(f"  * {parts[0]}" + (f" ({extra})" if extra else ""))
            else:
                lines.append(f"  * {w}")  # eski düz metin format

    # EKAP sözleşme geçmişi — sihirbaz artık geçmiş iş / il / tür SORMUYOR, firma
    # bağlıysa bilgi buradan gelir.
    if profile.contractor_id and profile.contractor:
        lines.append("")
        lines.append(company_intel.contractor_text(profile.contractor))

    return "\n".join(lines)


def derive_from_contractor(profile) -> bool:
    """
    Firma EKAP kaydına bağlıysa `cities` / `tender_types` alanlarını sözleşme
    geçmişinden doldurur. Değişiklik yapıldıysa True döner (çağıran kaydeder).

    Neden gerekli: kural tabanlı eşleştirme bu iki alanı indeksli ÖN FİLTRE olarak
    kullanıyor (`services/matching.py`). Sihirbaz artık sormuyor → boş kalırsa
    eşleştirme tüm Türkiye'yi tarar.

    Kullanıcı elle bir şey seçtiyse (manuel akış) DOKUNULMAZ.
    """
    if not profile.contractor_id or not profile.contractor:
        return False
    if profile.cities and profile.tender_types:
        return False

    iller, turler = company_intel.contractor_facets(profile.contractor)
    degisti = False
    if not profile.cities and iller:
        profile.cities = iller
        degisti = True
    if not profile.tender_types and turler:
        profile.tender_types = turler
        degisti = True
    return degisti


def profile_input_digest(profile) -> str:
    """
    Haritayı üreten prompt girdisinin sha256 özeti.

    Girdi olarak **`_profile_text` çıktısı** kullanılır, ham model alanları değil: prompt'a
    gerçekten giren metin budur. Alan eklenip prompt'a yansımıyorsa özet de değişmemeli,
    yansıyorsa değişmeli — ikisi otomatik olarak senkron kalır.
    """
    return hashlib.sha256(_profile_text(profile).encode("utf-8")).hexdigest()


def generate_profile_map(profile) -> tuple[dict, dict]:
    """
    Firma profilinden Claude ile profil haritası üretir.
    Dönen: (profil_haritası, usage). Hata: AnalysisError.
    """
    from assistant.prompts import PROFILE_MAP_PROMPT

    api_key = get_api_key()
    prompt = PROFILE_MAP_PROMPT + _profile_text(profile)

    # Web sitesi: kullanıcı girdiyse kısaca okunur. Erişilemezse boş döner ve
    # prompt'a hiç eklenmez — harita üretimi site yüzünden başarısız OLMAZ.
    site = company_intel.website_text(profile.website)
    if site:
        prompt += (
            "\n\n## FİRMANIN WEB SİTESİNDEN ALINAN METİN (ham, gürültülü olabilir)\n"
            "Bu metin firmanın kendi tanıtımıdır; EKAP verisi kadar güvenilir değildir. "
            "Yalnızca faaliyet alanı/uzmanlık sinyali olarak kullan, sayısal iddialarını "
            "doğrulanmış gerçek sayma.\n" + site
        )

    last_error = None
    kesildi = False
    for attempt in range(2):  # bozuk JSON'da 1 kez yeniden dene
        istek = prompt
        if attempt:
            # İkinci denemede modele ne yanlış gittiğini söyle — aynı prompt'u
            # aynen tekrarlamak çoğu zaman aynı bozuk çıktıyı üretiyor.
            istek += (
                "\n\nÖNEMLİ: Önceki yanıtın geçerli JSON değildi. Bu kez SADECE "
                "yukarıdaki şemaya uyan tek bir JSON nesnesi döndür; açıklama, "
                "markdown ya da kod bloğu yazma. Kısa tut ki yanıt kesilmesin."
            )
        result = call_claude(api_key, [], istek, max_tokens=MAX_TOKENS)
        kesildi = result.get("stop_reason") == "max_tokens"
        try:
            profile_map = parse_json_output(result["analysis"])
        except (ValueError, json.JSONDecodeError) as e:
            # Ham çıktının başını logla: "geçersiz AI çıktısı" tek başına hiçbir şey
            # anlatmıyordu, sunucu logundan sebebi görebilmek gerek.
            logger.warning(
                "profile_map JSON parse hatası (deneme %s, stop_reason=%s, %s token): %s | çıktı: %.400s",
                attempt + 1,
                result.get("stop_reason"),
                (result.get("usage") or {}).get("output_tokens"),
                e,
                result.get("analysis") or "(boş)",
            )
            last_error = e
            continue
        return profile_map, result.get("usage")

    if kesildi:
        raise AnalysisError(
            "Profil haritası üretilemedi (AI yanıtı token sınırında kesildi).", status=502
        ) from last_error
    raise AnalysisError("Profil haritası üretilemedi (geçersiz AI çıktısı).", status=502) from last_error
