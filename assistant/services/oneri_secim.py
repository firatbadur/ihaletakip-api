"""
Günlük asistan önerisi — filtre bildiriminin bulduğu ihalelerden yapay zekâ seçimi.

Akış (2026-10-06, kullanıcı kararı): 08:00 filtre özeti kullanıcının kayıtlı
filtrelerine uyan ihaleleri bulur; 09:00'da asistan **o kümeden** firma profiline en
uygun olanları seçer. Aday kümesi filtre bildirimiyle **aynı fonksiyondan**
(`ekap.views.kayitli_filtre_birlesimi`) ve **aynı pencereden** kurulur — bildirimdeki
sayı ile listenin üç kez ayrıştığı "iki kod yolu" hatası burada tekrarlanmasın.

⚠️ Prompt tavsiyedir, kod garantidir: modelin döndürdüğü id'ler aday kümesiyle
KESİŞTİRİLİR (uydurma id atılır), tekrarlar atılır, `AZAMI_SECIM`'de kesilir.
"""
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("ihaletakip")

AZAMI_ADAY = 60
AZAMI_SECIM = 5
_TIP = {1: "Mal", 2: "Yapım", 3: "Hizmet", 4: "Danışmanlık"}


def bugunku_filtre_bildirimi(user, gun=None):
    """
    Kullanıcının bugünkü filtre bildiriminin (filtre id'leri, pencere) çifti; yoksa None.

    Birleşik bayrak açıkken tek satır `filtre_idler` taşır; kapalıyken filtre başına ayrı
    satırlar `filter_id` taşır — ikisi de desteklenir.
    """
    from ekap.utils import local_day_range
    from tenders.models import Notification

    bas, bit = local_day_range(gun or timezone.localdate())
    satirlar = list(
        Notification.objects.filter(
            user=user, type=Notification.Type.TENDER,
            created_at__gte=bas, created_at__lt=bit,
            pencere_bas__isnull=False, pencere_bit__isnull=False,
        ).exclude(filtre_idler="", filter_id__isnull=True)
        .values("filtre_idler", "filter_id", "pencere_bas", "pencere_bit")
        .order_by("-created_at")
    )
    if not satirlar:
        return None
    idler = set()
    for s in satirlar:
        idler.update(int(x) for x in (s["filtre_idler"] or "").split(",") if x.strip().isdigit())
        if s["filter_id"]:
            idler.add(int(s["filter_id"]))
    if not idler:
        return None
    return idler, (satirlar[0]["pencere_bas"], satirlar[0]["pencere_bit"])


def adaylar(user, filtre_idler, pencere, *, haric_ihale_idleri=()):
    """Filtre bildirimiyle AYNI küme → kaydedilmiş ve önerilmiş olanlar hariç."""
    from ekap.models import Tender
    from ekap.views import kayitli_filtre_birlesimi
    from tenders.models import SavedFilter, SavedTender

    # ⚠️ Sahiplik: id'ler bildirimden okunuyor ama filtre yine kullanıcıya ait olmalı.
    filtreler = [
        f.filters or {} for f in SavedFilter.objects.filter(user=user, id__in=filtre_idler)
    ]
    if not filtreler:
        return []
    kume = kayitli_filtre_birlesimi(filtreler, pencere=pencere)
    kayitli = SavedTender.objects.filter(user=user).values_list("tender_ikn", flat=True)
    return list(
        Tender.objects.filter(pk__in=kume.values("pk"))
        .exclude(ikn__in=list(kayitli))
        .exclude(pk__in=list(haric_ihale_idleri))
        .order_by("-created_at")
        .prefetch_related("okas_kalemleri")[:AZAMI_ADAY]
    )


def _profil_metni(profile) -> str:
    pm = profile.profile_map or {}
    satirlar = [f"Firma: {profile.company_name or '-'}"]
    for etiket, anahtar in (("Özet", "summary"), ("Firma özeti", "company_summary")):
        if pm.get(anahtar):
            satirlar.append(f"{etiket}: {pm[anahtar]}")
    for etiket, anahtar in (("Uzmanlık anahtar kelimeleri", "keywords"),
                            ("Güçlü yanlar", "strengths"),
                            ("UYGUN OLMAYAN alanlar", "avoid")):
        if pm.get(anahtar):
            satirlar.append(f"{etiket}: {', '.join(map(str, pm[anahtar]))}")
    if profile.activity_areas:
        satirlar.append(f"Faaliyet alanları: {profile.activity_areas}")
    if profile.past_works:
        satirlar.append(f"Geçmiş işler: {str(profile.past_works)[:600]}")
    return "\n".join(satirlar)


def _aday_satiri(t) -> str:
    okas = ", ".join(k.adi for k in t.okas_kalemleri.all()[:2] if k.adi)
    return (f"[{t.pk}] {t.ihale_adi} · {t.idare_adi} · {t.ihale_il_adi or '-'} · "
            f"{_TIP.get(t.ihale_tip, '-')}" + (f" · OKAS: {okas}" if okas else ""))


_PROMPT = """Sen bir kamu ihalesi danışmanısın. Aşağıda bir firmanın profili ve bugün
firmanın kendi kayıtlı filtrelerine uyan ihaleler var. Görevin: bu ihalelerden firmanın
GERÇEKTEN teklif verebileceği, faaliyet alanına en uygun olanları seçmek.

KURALLAR
- En çok {azami} ihale seç. Uygun olan yoksa boş liste döndür — zorla seçme.
- Yalnızca aşağıdaki listedeki köşeli parantez içindeki id'leri kullan.
- "UYGUN OLMAYAN alanlar"a giren ihaleyi seçme.
- Kelime benzerliğine aldanma: işin konusu firmanın işi değilse seçme
  (ör. "köprü" geçen bir diş protezi alımı, inşaat firmasına uygun değildir).
- Listeye YALNIZCA önerdiğin ihaleleri koy; uygun bulmadığın ihaleyi listeye hiç yazma.
- Her seçimde "uygun": true olmalı. Emin değilsen seçme.
- Gerekçe tek kısa cümle, Türkçe, firmanın hangi işiyle örtüştüğünü söylesin.

ÇIKTI: yalnızca JSON, başka metin yok:
{{"secimler": [{{"id": 123, "uygun": true, "gerekce": "..."}}]}}

## FİRMA PROFİLİ
{profil}

## BUGÜNKÜ ADAY İHALELER
{adaylar}
"""


def ai_ile_sec(profile, aday_listesi):
    """[(tender, gerekçe)] — en çok AZAMI_SECIM. Hata → istisna (çağıran yedeğe düşer)."""
    from ai.services.claude import call_claude, get_api_key
    from assistant.services.profile_map import parse_json_output

    if not aday_listesi:
        return []
    prompt = _PROMPT.format(
        azami=AZAMI_SECIM,
        profil=_profil_metni(profile),
        adaylar="\n".join(_aday_satiri(t) for t in aday_listesi),
    )
    cevap = call_claude(get_api_key(), [], prompt, max_tokens=1200,
                        model=settings.CLAUDE_CHAT_MODEL)
    veri = parse_json_output(cevap["analysis"])
    eldeki = {t.pk: t for t in aday_listesi}
    secim, gorulen = [], set()
    for s in veri.get("secimler") or []:
        try:
            pk = int(s.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        # ⚠️ Kod garantisi: aday kümesinde olmayan (uydurma) id ve tekrar atılır.
        if pk not in eldeki or pk in gorulen:
            continue
        # ⚠️ Açık "uygun: true" şart. Üretim kuru çalıştırmasında (2026-10-06) model bir
        # ihaleyi listeye koyup gerekçesine "...için uygun değildir" yazdı; o ihale
        # "öneri" olarak kullanıcıya gidecekti. Alan yoksa/false ise atılır.
        if s.get("uygun") is not True:
            continue
        gorulen.add(pk)
        secim.append((eldeki[pk], str(s.get("gerekce") or "").strip()[:300]))
        if len(secim) >= AZAMI_SECIM:
            break
    return secim


def kural_ile_sec(profile, aday_listesi):
    """AI kullanılamazsa yedek: aynı aday kümesinde kural tabanlı puan (≥3)."""
    from assistant.services.matching import profil_baglami, puanla

    baglam = profil_baglami(profile)
    puanli = []
    for t in aday_listesi:
        sonuc = puanla(t, baglam)
        if sonuc and sonuc[0] >= 3.0:
            puanli.append((sonuc[0], t, "; ".join(sonuc[1])))
    puanli.sort(key=lambda x: x[0], reverse=True)
    return [(t, gerekce) for _, t, gerekce in puanli[:AZAMI_SECIM]]


def sec(profile, aday_listesi):
    """(seçim, yöntem) — yöntem ∈ {"ai", "kural"}."""
    try:
        return ai_ile_sec(profile, aday_listesi), "ai"
    except Exception as e:                              # noqa: BLE001
        logger.warning("asistan önerisi AI seçimi başarısız (uid=%s): %s — kural yedeği",
                       profile.user_id, e)
        return kural_ile_sec(profile, aday_listesi), "kural"
