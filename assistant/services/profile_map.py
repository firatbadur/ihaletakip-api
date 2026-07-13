"""Firma profil haritası üretimi — Claude'dan yapılandırılmış JSON çıkarır."""
import json
import logging

from ai.services.claude import AnalysisError, call_claude, get_api_key

logger = logging.getLogger("ihaletakip")

TENDER_TYPE_LABELS = {1: "Mal Alımı", 2: "Yapım", 3: "Hizmet", 4: "Danışmanlık"}


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
    """CompanyProfile → prompt'a eklenecek okunabilir firma bilgisi."""
    from ekap.models import City

    city_names = list(
        City.objects.filter(ekap_il_id__in=profile.cities or []).values_list("ad", flat=True)
    )
    type_labels = [TENDER_TYPE_LABELS.get(t, str(t)) for t in (profile.tender_types or [])]

    lines = [
        f"- Firma adı: {profile.company_name}",
        f"- Sektör: {profile.sector or 'Belirtilmedi'}",
        f"- Faaliyet alanları: {profile.activity_areas or 'Belirtilmedi'}",
        f"- İlgilenilen iller: {', '.join(city_names) or 'Tümü'} (id: {profile.cities or []})",
        f"- İlgilenilen ihale türleri: {', '.join(type_labels) or 'Tümü'} (id: {profile.tender_types or []})",
    ]
    if profile.budget_min or profile.budget_max:
        lines.append(f"- Bütçe aralığı: {profile.budget_min or '-'} — {profile.budget_max or '-'} TL")
    if profile.past_works:
        lines.append("- Geçmiş işler:")
        lines.extend(f"  * {w}" for w in profile.past_works)
    return "\n".join(lines)


def generate_profile_map(profile) -> tuple[dict, dict]:
    """
    Firma profilinden Claude ile profil haritası üretir.
    Dönen: (profil_haritası, usage). Hata: AnalysisError.
    """
    from assistant.prompts import PROFILE_MAP_PROMPT

    api_key = get_api_key()
    prompt = PROFILE_MAP_PROMPT + _profile_text(profile)

    last_error = None
    for attempt in range(2):  # bozuk JSON'da 1 kez yeniden dene
        result = call_claude(api_key, [], prompt, max_tokens=1500)
        try:
            profile_map = parse_json_output(result["analysis"])
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("profile_map JSON parse hatası (deneme %s): %s", attempt + 1, e)
            last_error = e
            continue
        return profile_map, result.get("usage")

    raise AnalysisError("Profil haritası üretilemedi (geçersiz AI çıktısı).", status=502) from last_error
