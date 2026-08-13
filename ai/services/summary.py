"""
Rapor sesli özeti — veri toplama + budama (HTTP ve Celery'den bağımsız).

`POST /ai/summary/` ucu ve `ai.tasks.run_summary_task` bunu çağırır.

⚠️ **Bu modülün döndürdüğü veri MASKESİZDİR.** `profil()`, `genel_bakis()`,
`grup_detayi()` ve `benchmark()` ham değerleri verir; maskeleme view katmanında
yapılır (`ekap.views._market_maskele`, `TenderBenchmarkView._KILITLI`). Bu yüzden
çağıran uç **`require_premium` kapısını atlayamaz** — aksi hâlde Free kullanıcıya
maskeli tutarlar özet metni üzerinden sızar.
"""
import hashlib
import json
import re

# Prompt'u şişirmemek için liste alanlarından alınacak satır sayısı.
# (Aynı gerekçe `ai/services/claude.py`'deki karakter budamasında da var.)
_LISTE_LIMIT = 5
# Para serilerinde kaç yıl geriye bakılacağı — TL enflasyonu yüzünden daha eskisi
# zaten karşılaştırılamıyor (prompt de model'e bunu yasaklıyor).
_YIL_LIMIT = 5

GECERLI_TURLER = ("authority", "market", "benchmark")

# ── Sesli okuma temizliği ─────────────────────────────────────────────────────
# ⚠️ **Prompt kuralı TEK BAŞINA YETMEZ.** Model "markdown kullanma" talimatına rağmen
# başlık atabiliyor ve çıktı TTS'e gidince kullanıcı "**kare** sesli özet" duyuyor
# (üretimde yaşandı: model metne `# Sesli Özet` başlığı ekledi). Prompt tavsiyedir,
# kod garantidir — bu yüzden metin dönmeden önce burada temizlenir.
_MD_BASLIK = re.compile(r"^\s{0,3}#{1,6}\s*.*$", re.M)          # # Başlık
_MD_MADDE = re.compile(r"^\s{0,3}(?:[-*+•·]|\d{1,2}[.)])\s+", re.M)  # - madde / 1. madde
_MD_VURGU = re.compile(r"[*_`~]+")                               # **kalın**, _italik_, `kod`
_BOSLUK = re.compile(r"[ \t]+")

# Sembol → sözcük. TTS bunları ya atlıyor ya da adını okuyor ("yüzde işareti").
# ⚠️ "TL" buraya DÜZ STRING olarak konmaz: "ATLAS" → "A lira AS" olurdu.
# Sözcük sınırıyla ayrı ele alınır (aşağıda).
_SEMBOL = (
    ("₺", " lira"), ("$", " dolar"), ("€", " euro"),
    ("≈", " yaklaşık "), ("&", " ve "),
    ("→", " "), ("|", " "), (">", " "), ("<", " "),
)


def sesli_temizle(metin: str) -> str:
    """
    LLM çıktısını **sesli okumaya** hazırlar: markdown, sembol ve satır yapısını atar.

    ⚠️ Bu fonksiyon prompt kurallarının YEDEĞİdir, alternatifi değil. Model uyduğunda
    hiçbir şey değiştirmez; uymadığında kullanıcı bozuk ses duymaz.
    """
    if not metin:
        return ""
    metin = _MD_BASLIK.sub("", metin)
    metin = _MD_MADDE.sub("", metin)
    # ⚠️ Sayı önündeki tilde markdown DEĞİL, "yaklaşık" demektir — vurgu temizliğinden
    # ÖNCE çevrilmeli, yoksa `_MD_VURGU` onu silip anlamı düşürür ("~500" → "500").
    # Bu kod tabanında sayıyı olduğundan farklı aktarmak kabul edilmez.
    metin = re.sub(r"~\s*(?=\d)", "yaklaşık ", metin)
    metin = _MD_VURGU.sub("", metin)
    for sembol, karsilik in _SEMBOL:
        metin = metin.replace(sembol, karsilik)
    metin = re.sub(r"\bTL\b", "lira", metin)
    # "%21" → "yüzde 21" (sembolden SONRA sayı gelen hâli), sonra kalan tekil "%"
    metin = re.sub(r"%\s*(\d)", r"yüzde \1", metin)
    metin = metin.replace("%", " yüzde ")
    # Satırları tek akıcı paragrafa indir — TTS satır sonunu duraklama sanmaz
    metin = " ".join(satir.strip() for satir in metin.splitlines() if satir.strip())
    metin = _BOSLUK.sub(" ", metin)
    # Temizlik sonrası oluşan " ." / " ," gibi boşlukları topla
    metin = re.sub(r"\s+([,.;:!?])", r"\1", metin)
    return metin.strip()


def _kirp(veri, anahtar, limit=_LISTE_LIMIT):
    """Sözlükteki liste alanını ilk `limit` satıra indirger (yerinde)."""
    liste = veri.get(anahtar)
    if isinstance(liste, list) and len(liste) > limit:
        veri[anahtar] = liste[:limit]


def _buda(kind, veri):
    """
    LLM'e gitmeden önce hacimli liste alanlarını kırpar.

    Özet için ilk birkaç satır yeterlidir; tamamı prompt'u şişirir ve token maliyetini
    görev başına katlar. Sayısal özet alanlarına (ortalama, örneklem sayısı, güven)
    **dokunulmaz** — dürüstlük kuralları onlara dayanıyor.
    """
    if not isinstance(veri, dict):
        return veri
    veri = json.loads(json.dumps(veri, default=str))  # derin kopya + Decimal → str

    for anahtar in ("benzer_ihaleler", "iller", "firmalar", "is_gruplari"):
        _kirp(veri, anahtar)
    for anahtar in ("yillara_gore", "yillar"):
        liste = veri.get(anahtar)
        if isinstance(liste, list) and len(liste) > _YIL_LIMIT:
            veri[anahtar] = liste[-_YIL_LIMIT:]

    dagilim = veri.get("dagilim")
    if isinstance(dagilim, dict):
        for anahtar in list(dagilim):
            _kirp(dagilim, anahtar)
    yuklenici = veri.get("yuklenici_dagilim")
    if isinstance(yuklenici, dict):
        _kirp(yuklenici, "liste")
    return veri


def veri_topla(kind, params):
    """
    `kind`'a göre ilgili rapor verisini üretir. Döner: `(veri, hata)`.

    ⚠️ Kapsam çözümü `authority` için `ekap.authority_profile.kapsam_coz` ile
    **aynıdır** (`en_ust_idare_kod` > `idare_id` > `idare_detsis`) — iki yerde
    ayrışmasın diye o fonksiyon doğrudan kullanılıyor, kopyalanmıyor.
    """
    if kind == "authority":
        from ekap.authority_profile import profil

        return profil(params, detay=True)

    if kind == "market":
        from ekap import market as market_mod

        bucket = (params.get("okas_bucket") or "").strip()
        yil = params.get("yil")
        if bucket:
            return market_mod.grup_detayi(bucket, yil=yil, limit=10)
        return market_mod.genel_bakis(yil, limit=10)

    if kind == "benchmark":
        from ekap.benchmark import benchmark
        from ekap.views import _tender_by_key

        tender = _tender_by_key((params.get("ekap_id") or "").strip())
        if tender is None:
            return None, "İhale bulunamadı."
        return benchmark(tender, yil_geri=5)

    return None, "Geçersiz özet türü."


def cache_anahtari(kind, params):
    """
    `ai:summary:{kind}:{sha1(normalize parametreler)}`

    ⚠️ Anahtar **yalnızca sonucu değiştiren** parametrelerden üretilir ve sıralı
    JSON ile hash'lenir (aynı desen `ekap.views._cached_count`'ta). Kullanıcıya
    özel bir şey girmez: özet kullanıcıdan bağımsızdır, aynı rapor herkese aynı
    metni verir.
    """
    ilgili = {k: v for k, v in sorted(params.items()) if v not in (None, "")}
    ham = json.dumps({"kind": kind, **ilgili}, sort_keys=True, ensure_ascii=False)
    return f"ai:summary:{kind}:{hashlib.sha1(ham.encode()).hexdigest()}"


def ozet_uret(kind, params):
    """
    Veriyi toplar, budar ve Claude'dan sesli okumaya uygun özet ister.

    Döner: `{"analysis": str, "usage": dict}`. Hata durumunda `AnalysisError` yükselir
    (çağıran görev onu `{"success": False, "error": ...}`'e çevirir).
    """
    from django.conf import settings

    from ai.prompts import PROMPTS
    from ai.services.claude import AnalysisError, call_claude, get_api_key

    veri, hata = veri_topla(kind, params)
    if hata:
        raise AnalysisError(hata, status=422)
    if not veri:
        raise AnalysisError("Özetlenecek veri bulunamadı.", status=422)

    sablon = PROMPTS.get(f"ozet_{kind}")
    if not sablon:
        raise AnalysisError("Geçersiz özet türü.", status=400)

    gövde = json.dumps(_buda(kind, veri), ensure_ascii=False, default=str)
    # ⚠️ Model **haiku** (`CLAUDE_CHAT_MODEL`): görev kısa ve şablonlu, kalite
    # öncelikli `CLAUDE_MODEL` gereksiz maliyet olurdu (bkz. CLAUDE.md model ayrımı).
    sonuc = call_claude(
        get_api_key(),
        [],
        sablon + gövde,
        max_tokens=700,
        model=settings.CLAUDE_CHAT_MODEL,
    )
    # ⚠️ Temizlik ŞART — bkz. `sesli_temizle`. Model başlık/madde işareti eklediğinde
    # TTS bunları sesli okuyor ("kare sesli özet").
    sonuc["analysis"] = sesli_temizle(sonuc.get("analysis", ""))
    return sonuc
