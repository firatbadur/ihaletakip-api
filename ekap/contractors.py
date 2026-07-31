"""
Yüklenici (firma) kimliği — kanonikleştirme, ortak girişim ayrıştırma, toplu çözümleme.

EKAP payload'ında **VKN/vergi no/TCKN YOKTUR**; yüklenici yalnızca serbest metin ünvan
olarak gelir. Bu yüzden firma kimliği `canonical_key()` ile üretilen normalize ünvandır.

Tasarım ilkesi: **yanlış-birleştirme, yanlış-ayırmadan daha kötüdür.** İki ayrı firmayı
birleştirmek geçmişi geri dönülmez biçimde kirletir; iki yazımı ayrı bırakmak ise alias
tablosundan görülebilen, elle düzeltilebilir bir durumdur. Bu yüzden burada bulanık
eşleştirme, edit-distance veya sektör sözcüğü silme YOKTUR.
"""
import logging
import re
import unicodedata
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .utils import normalize_tr

logger = logging.getLogger("ihaletakip")


# ── Kanonikleştirme ────────────────────────────────────────
# ⚠️ `normalize_tr` DEĞİŞTİRİLMEZ — mevcut tüm `*_norm` sütunları (Tender.ihale_adi_norm,
# Authority.ad_norm, OkasCode.adi_norm) onunla yazıldı; davranışını değiştirmek hepsini
# sessizce geçersizleştirir. Ek katlama yalnızca burada yapılır.

# Noktalı kısaltmalar → tam sözcük. **Nokta şartı güvenliği sağlar**: "İT." kesinlikle
# "ithalat" kısaltmasıdır, çıplak "İT" ise olmayabilir → dokunulmaz.
# Gerçek veri kanıtı (aynı payload, aynı firma):
#   "LABOR ... İNŞAAT TAAHHÜT GIDA ORGANİZASYON İTHALAT VE İHRACAT SANAYİ TİCARET LİMİTED ŞİRKETİ"
#   "LABOR ... İNŞ.TAAH.GD.ORG.İTVE İH.SA.TİC.LTD.ŞTİ"
_ABBREV = {
    "ins": "insaat", "insa": "insaat",
    "taah": "taahhut", "tah": "taahhut", "taahh": "taahhut",
    "gd": "gida",
    "org": "organizasyon",
    "it": "ithalat", "ith": "ithalat",
    "ih": "ihracat", "ihr": "ihracat",
    "sa": "sanayi", "san": "sanayi",
    "tic": "ticaret",
    "paz": "pazarlama",
    "nak": "nakliyat",
    "muh": "muhendislik", "muhend": "muhendislik",
    "tur": "turizm",
    "mim": "mimarlik",
    "tem": "temizlik",
    "hiz": "hizmetleri",
    "teks": "tekstil",
    "konf": "konfeksiyon",
    "mob": "mobilya",
    "med": "medikal",
    "elek": "elektrik",
    "insaat": "insaat",
}
_ABBREV_RE = re.compile(r"\b(" + "|".join(sorted(_ABBREV, key=len, reverse=True)) + r")\s*\.")

# Tüzel biçim → tek token. **Silinmez, tekilleştirilir**: "X LTD ŞTİ" ile "X A.Ş." hukuken
# farklı tüzel kişilerdir; silmek onları birleştirir (yanlış-birleştirme).
_LTD_RE = re.compile(
    r"\b(limited\s+sirketi|limited\s+sti|ltd\s+sirketi|ltd\s+sti|limited)\b"
)
_AS_RE = re.compile(r"\b(anonim\s+sirketi|anonim\s+sti|anonim)\b")
_KOLLEKTIF_RE = re.compile(r"\b(kollektif\s+sirketi|kollektif)\b")
_KOMANDIT_RE = re.compile(r"\b(komandit\s+sirketi|komandit)\b")
_KOOP_RE = re.compile(r"\b(kooperatifi|kooperatif)\b")

_LEGAL_TOKENS = {"ltdsti", "as", "kollektif", "komandit", "kooperatif"}

# Kimlik taşımayan sektör/bağlaç sözcükleri — parça doğrulamada "anlamlı token" sayılmaz.
# (Kanonik anahtardan SİLİNMEZ; yalnızca ortak girişim parça doğrulamasında kullanılır.)
_STOPWORDS = {
    "ve", "ile", "sanayi", "ticaret", "insaat", "taahhut", "ithalat", "ihracat",
    "pazarlama", "hizmetleri", "hizmet", "turizm", "gida", "organizasyon", "nakliyat",
    "tekstil", "medikal", "mobilya", "temizlik", "konfeksiyon", "muhendislik",
    "mimarlik", "elektrik", "otomotiv", "madencilik", "enerji", "bilgisayar",
} | _LEGAL_TOKENS


def _fold_unicode(text: str) -> str:
    """NFKD + birleşik işaretleri at (é→e, â→a), sonra Türkçe katlama."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return normalize_tr(stripped)


def canonical_key(raw) -> str:
    """
    Ham ünvanı firma **kimlik anahtarına** indirger.

    Sıralı boru hattı — 2. adım noktalama temizliğinden ÖNCE gelir, çünkü nokta
    kısaltmayı ayırt eden tek sinyaldir.
    """
    if not raw:
        return ""

    # 1) Unicode + Türkçe katlama
    s = _fold_unicode(raw)
    if not s:
        return ""

    # 2) Noktalı kısaltmaları aç ("ins." → "insaat").
    #    ⚠️ Bu adım noktalama temizliğinden ÖNCE olmak ZORUNDA — nokta, kısaltmayı
    #    ayırt eden tek sinyaldir ("İT." kesin kısaltma, çıplak "İT" değil).
    s = _ABBREV_RE.sub(lambda m: _ABBREV[m.group(1)] + " ", s)

    # 3) Noktalama temizliği.
    #    ⚠️ Tüzel biçimden ÖNCE gelmeli: EKAP "LTD.ŞTİ." biçimini noktayla gönderiyor,
    #    noktalar durduğu sürece `ltd\s+sti` deseni eşleşmiyor ve "ltd sti" iki ayrı
    #    token olarak kalıyordu → "… LİMİTED ŞİRKETİ" ile "… LTD.ŞTİ." ayrı firma olurdu.
    s = s.replace("&", " ve ").replace("+", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # 4) Tüzel biçimi tekilleştir (SİLME — bkz. yukarıdaki not)
    s = _LTD_RE.sub(" ltdsti ", s)
    s = _AS_RE.sub(" as ", s)
    s = _KOLLEKTIF_RE.sub(" kollektif ", s)
    s = _KOMANDIT_RE.sub(" komandit ", s)
    s = _KOOP_RE.sub(" kooperatif ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # "a s" (A.Ş.) ancak SON token grubuysa ve öncesinde ≥2 token varsa A.Ş. sayılır
    tokens = s.split()
    if len(tokens) >= 4 and tokens[-2:] == ["a", "s"]:
        s = " ".join(tokens[:-2] + ["as"])

    s = re.sub(r"\s+", " ", s).strip()
    return s[:500]


def display_name(raw) -> str:
    """Gösterim adı — ham yazımı korur, yalnızca fazla boşluk/sondaki noktayı temizler."""
    if not raw:
        return ""
    return re.sub(r"\s+", " ", str(raw)).strip().rstrip(".").strip()[:500]


def guess_kind(raw, key: str) -> str:
    """
    Firma / şahıs / ortak girişim tahmini.
    ⚠️ Yalnızca **bilgilendiricidir**, kimlik anahtarının parçası DEĞİLDİR.
    """
    from .models import Contractor

    if split_joint_venture(raw).is_jv:
        return Contractor.Kind.ORTAK_GIRISIM

    tokens = key.split()
    if tokens and not (_LEGAL_TOKENS & set(tokens)) and len(tokens) <= 3:
        if all(t.isalpha() for t in tokens):
            # Gerçek veri: "CENGİZ KABAKULAK", "SALİM KÜÇÜK", "KEZİBAN ATÇA"
            return Contractor.Kind.SAHIS
    return Contractor.Kind.FIRMA


def legal_form(key: str) -> str:
    """Kanonik anahtardan tüzel tip token'ını çıkarır ("" = belirsiz)."""
    for token in key.split():
        if token in _LEGAL_TOKENS:
            return token
    return ""


# ── Ortak girişim (iş ortaklığı / konsorsiyum) ─────────────

@dataclass(frozen=True)
class JVSplit:
    is_jv: bool
    marker: str = ""
    members: tuple = ()          # ham parçalar (kanonikleştirilmemiş)
    guven: str = "yuksek"        # yuksek | orta | dusuk


# Marker **zorunludur**: sıradan bir ünvandaki "-" veya "," asla bölmeyi tetiklememeli.
_JV_MARKERS = (
    "is ortakligi", "isortakligi", "ortak girisimi", "ortak girisim",
    "konsorsiyumu", "konsorsiyum", "adi ortakligi", "adi ortaklik",
)
_JV_TAIL = 40           # marker string'in son N karakterinde olmalı
_JV_MAX_MEMBERS = 6     # fazlası ayırıcı şelalesinin şaştığı anlamına gelir

_SEPARATORS = (" - ", " – ", " — ", " & ", " ile ", " + ")


def _meaningful_tokens(raw_part: str) -> int:
    """Parçadaki kimlik taşıyan (stopword olmayan) token sayısı."""
    key = canonical_key(raw_part)
    return sum(1 for t in key.split() if t not in _STOPWORDS)


def split_joint_venture(raw) -> JVSplit:
    """
    Ortak girişim ünvanını üyelerine ayırır. **Marker yoksa asla bölmez.**

    ⚠️ Yerel geliştirme DB'sinde ortak girişim örneği bulunmadığı için savunmacı yazıldı;
    şüphede kalınca `guven="dusuk"` döner ve çağıran taraf üyelik YAZMAZ.
    """
    if not raw:
        return JVSplit(False)

    norm = _fold_unicode(raw)
    norm = re.sub(r"[^a-z0-9 ]+", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return JVSplit(False)

    # 1) Marker kapısı — string'in sonuna yakın olmalı
    tail = norm[-_JV_TAIL:]
    marker = next((m for m in _JV_MARKERS if m in tail), "")
    if not marker:
        return JVSplit(False)

    # 2) Marker'ı ve varsa sondaki eki at
    body = str(raw)
    cut = _fold_unicode(body)
    idx = cut.rfind(marker.split()[0])
    if idx > 0:
        body = body[:idx]
    body = body.strip().rstrip("-–—,").strip()
    if not body:
        return JVSplit(True, marker, (), "dusuk")

    # 3) Ayırıcı şelalesi — ≥2 anlamlı parça veren ilki kazanır
    parts = []
    for sep in _SEPARATORS:
        candidate = [p.strip() for p in body.split(sep) if p.strip()]
        if len(candidate) >= 2:
            parts = candidate
            break

    if not parts:
        # Son çare: virgül. Ek koruma — her parça ≥8 karakter ve ≥2 anlamlı token olmalı,
        # aksi halde "X SANAYİ, TİCARET LTD ŞTİ" yanlışlıkla bölünürdü.
        candidate = [p.strip() for p in body.split(",") if p.strip()]
        if len(candidate) >= 2 and all(
            len(p) >= 8 and _meaningful_tokens(p) >= 2 for p in candidate
        ):
            parts = candidate

    if len(parts) < 2:
        return JVSplit(True, marker, (), "dusuk")
    if len(parts) > _JV_MAX_MEMBERS:
        logger.warning("Ortak girişim üye tavanı aşıldı (%s parça): %s", len(parts), raw)
        return JVSplit(True, marker, (), "dusuk")

    # 4) Parça doğrulama — her parça kimlik taşımalı
    if any(_meaningful_tokens(p) < 1 for p in parts):
        return JVSplit(True, marker, (), "dusuk")

    # 5) Güven
    has_legal = [bool(_LEGAL_TOKENS & set(canonical_key(p).split())) for p in parts]
    if all(has_legal) or all(_meaningful_tokens(p) >= 2 for p in parts):
        guven = "yuksek"
    else:
        guven = "orta"

    return JVSplit(True, marker, tuple(parts), guven)


# ── Toplu çözümleme (N+1 önleme) ───────────────────────────

def resolve_contractors(raw_names, *, kaynak=None) -> dict:
    """
    Ham ad → `Contractor`. Bir ihalenin TÜM sözleşmeleri için ~5 sorgu.

    Dönen sözlüğün anahtarı **ham ad string'idir** (çağıran onu elinde tutar).
    Çözülemeyen (boş) adlar sözlükte yer almaz.
    """
    from .models import Contractor, ContractorAlias

    names = [str(n).strip() for n in raw_names if n and str(n).strip()]
    if not names:
        return {}
    unique_names = list(dict.fromkeys(names))
    kaynak = kaynak or ContractorAlias.Kaynak.SOZLESME

    norm_by_name = {n: normalize_tr(n)[:500] for n in unique_names}
    key_by_name = {n: canonical_key(n) for n in unique_names}

    # 1) Alias cache isabet yolu
    alias_rows = ContractorAlias.objects.filter(
        ham_ad_norm__in=set(norm_by_name.values())
    ).select_related("contractor")
    by_alias = {a.ham_ad_norm: a.contractor for a in alias_rows}

    resolved = {}
    missing = []
    for name in unique_names:
        hit = by_alias.get(norm_by_name[name])
        if hit is not None:
            resolved[name] = hit
        elif key_by_name[name]:
            missing.append(name)

    # 2) Kanonik anahtarla mevcut firmalar
    if missing:
        keys = {key_by_name[n] for n in missing}
        existing = {c.kanonik_anahtar: c for c in
                    Contractor.objects.filter(kanonik_anahtar__in=keys)}

        # 3) Yeni firmalar — ignore_conflicts + yeniden okuma (yarış güvenli).
        #    Gerekli: TenderDetailView'ın lazy-sync yolu web process'inde, `ekap`
        #    tek-concurrency worker'ının DIŞINDA çalışıyor.
        new_objs, seen_new = [], set()
        for name in missing:
            key = key_by_name[name]
            if key in existing or key in seen_new:
                continue
            seen_new.add(key)
            new_objs.append(Contractor(
                kanonik_anahtar=key,
                kanonik_ad=display_name(name),
                kind=guess_kind(name, key),
                tuzel_tip=legal_form(key),
                arama_norm=_build_arama_norm(display_name(name), key, []),
            ))
        if new_objs:
            Contractor.objects.bulk_create(new_objs, ignore_conflicts=True, batch_size=500)
            existing.update({
                c.kanonik_anahtar: c
                for c in Contractor.objects.filter(kanonik_anahtar__in=seen_new)
            })

        for name in missing:
            hit = existing.get(key_by_name[name])
            if hit is not None:
                resolved[name] = hit

    # 4) Alias satırlarını yaz/güncelle
    _bulk_upsert_aliases(
        [(resolved[n], n, norm_by_name[n], kaynak) for n in unique_names if n in resolved]
    )

    # 5) Ortak girişim ayrıştırma — **yalnızca 1 seviye** (üye kendisi ayrıştırılmaz)
    _resolve_memberships([c for c in resolved.values()
                          if c.kind == Contractor.Kind.ORTAK_GIRISIM])

    return resolved


def _build_arama_norm(kanonik_ad: str, kanonik_anahtar: str, aliases) -> str:
    """Ad + anahtar + alias'ları tek aranabilir blob'a katlar (≤1000 char)."""
    blob = " ".join([normalize_tr(kanonik_ad), kanonik_anahtar,
                     *(normalize_tr(a) for a in aliases)])
    blob = re.sub(r"\s+", " ", blob).strip()
    return blob[:1000]


def _bulk_upsert_aliases(rows) -> None:
    """rows: [(contractor, ham_ad, ham_ad_norm, kaynak)] — tek sorguda upsert."""
    from .models import ContractorAlias

    if not rows:
        return
    # Aynı ham_ad_norm iki kez gelirse Postgres ON CONFLICT patlar → dedupe şart
    deduped = {}
    for contractor, ham_ad, ham_norm, kaynak in rows:
        if ham_norm:
            deduped[ham_norm] = (contractor, ham_ad, kaynak)

    now = timezone.now()
    objs = [
        ContractorAlias(contractor=c, ham_ad=display_name(ham), ham_ad_norm=norm,
                        kaynak=kaynak, son_gorulme=now)
        for norm, (c, ham, kaynak) in deduped.items()
    ]
    ContractorAlias.objects.bulk_create(
        objs, update_conflicts=True, unique_fields=["ham_ad_norm"],
        update_fields=["son_gorulme"], batch_size=500,
    )


def attach_alias(contractor, raw, kaynak) -> bool:
    """
    Varyant yazımı **mevcut** firmaya bağlar (konum tabanlı — tahmin yok).

    Kullanım: `kisimItemDto.yuklenici` ve sonuç ilanının `istekliAdi` alanı aynı sözleşme
    satırında olduğu için firması kesindir; ama yazımları farklı olabilir (gerçek veri:
    "ASİDENT … TAAHÜT …" vs "ASİDENT … TAAHHÜT …" — yazım hatası). Bunları bağımsız
    çözmek mükerrer firma üretirdi.

    `canonical_key(raw)` BAŞKA bir mevcut firmaya çözülüyorsa bağlamaz (kimlik çalmaz).
    """
    from .models import Contractor, ContractorAlias

    if not raw or contractor is None:
        return False
    ham_norm = normalize_tr(raw)[:500]
    if not ham_norm:
        return False

    key = canonical_key(raw)
    if key and key != contractor.kanonik_anahtar:
        clash = Contractor.objects.filter(kanonik_anahtar=key).exclude(pk=contractor.pk).first()
        if clash is not None:
            # Beklenen ve zararsız bir sonuç, hata DEĞİL: bu yazım zaten başka bir
            # firmanın kimliği. Bağlasaydık yazımı bir firmadan alıp diğerine verirdik
            # (yanlış-birleştirme). Sözleşme yine `yukleniciAdi`'ndan çözülen firmaya
            # bağlı kalır; yalnızca varyant kaydı atlanır.
            # INFO seviyesi bilinçli: 500 binlik backfill'de WARNING log seli yapıyordu.
            # Sayısı çalışma özetinde `alias_cakismasi` olarak raporlanır.
            logger.info(
                "alias atlandı — '%s' zaten #%s firmasına ait (hedef #%s)",
                raw, clash.pk, contractor.pk,
            )
            return False

    existing = ContractorAlias.objects.filter(ham_ad_norm=ham_norm).first()
    if existing is not None:
        return existing.contractor_id == contractor.pk

    _bulk_upsert_aliases([(contractor, raw, ham_norm, kaynak)])
    return True


def _resolve_memberships(jv_contractors) -> None:
    """Ortak girişim üyeliklerini çözer. Güven düşükse ÜYE YAZMAZ."""
    from .models import Contractor, ContractorAlias, ContractorMembership

    for jv in jv_contractors:
        if jv.uyelikler.exists():
            continue
        split = split_joint_venture(jv.kanonik_ad)
        if not split.is_jv or not split.members or split.guven == "dusuk":
            if not jv.uyeleri_cozumlendi:
                continue
            Contractor.objects.filter(pk=jv.pk).update(uyeleri_cozumlendi=False)
            continue

        # Üyeleri çöz — depth 1: üye kendisi ortak girişim olarak ayrıştırılmaz
        member_map = {}
        for raw in split.members:
            key = canonical_key(raw)
            if not key:
                continue
            member, _ = Contractor.objects.get_or_create(
                kanonik_anahtar=key,
                defaults={
                    "kanonik_ad": display_name(raw),
                    "kind": (Contractor.Kind.SAHIS
                             if guess_kind(raw, key) == Contractor.Kind.SAHIS
                             else Contractor.Kind.FIRMA),
                    "tuzel_tip": legal_form(key),
                    "arama_norm": _build_arama_norm(display_name(raw), key, []),
                },
            )
            member_map[raw] = member
            _bulk_upsert_aliases([
                (member, raw, normalize_tr(raw)[:500], ContractorAlias.Kaynak.SOZLESME)
            ])

        if len(member_map) < 2:
            Contractor.objects.filter(pk=jv.pk).update(uyeleri_cozumlendi=False)
            continue

        ContractorMembership.objects.bulk_create(
            [
                ContractorMembership(
                    ortak_girisim=jv, uye=member, sira=i, pilot=(i == 0),
                    kaynak_metin=display_name(raw)[:500], guven=split.guven,
                )
                for i, (raw, member) in enumerate(member_map.items())
            ],
            ignore_conflicts=True,
        )
        Contractor.objects.filter(pk=jv.pk).update(
            uyeleri_cozumlendi=True, uye_sayisi=len(member_map)
        )
        for member in member_map.values():
            Contractor.objects.filter(pk=member.pk).update(
                ortak_girisim_sayisi=member.ortakliklar.count()
            )


# ── Agrega yeniden hesabı ──────────────────────────────────

def recompute_aggregates(contractor_ids=None, chunk: int = 2000) -> int:
    """
    Firma agregalarını (`sozlesme_sayisi`, `toplam_sozlesme_bedeli`, …) yeniden hesaplar.

    `contractor_ids` None ise TÜM firmalar. Döner: güncellenen firma sayısı.
    """
    from django.db.models import Avg, Count, Max, Min, Sum

    from .models import Contract, Contractor

    qs = Contractor.objects.all()
    if contractor_ids is not None:
        ids = list(contractor_ids)
        if not ids:
            return 0
        qs = qs.filter(pk__in=ids)

    now = timezone.now()
    updated = 0
    pks = list(qs.values_list("pk", flat=True))

    for start in range(0, len(pks), chunk):
        batch = pks[start:start + chunk]
        stats = {
            row["yuklenici"]: row
            for row in Contract.objects.filter(yuklenici_id__in=batch)
            .values("yuklenici")
            .annotate(
                n_sozlesme=Count("id"),
                n_ihale=Count("tender_id", distinct=True),
                n_idare=Count("idare_id", distinct=True),
                toplam=Sum("sozlesme_bedeli_num"),
                ilk=Min("sozlesme_tarihi"),
                son=Max("sozlesme_tarihi"),
                ort_indirim=Avg("indirim_orani"),
                n_indirim=Count("indirim_orani"),
            )
        }
        objs = []
        for c in Contractor.objects.filter(pk__in=batch):
            row = stats.get(c.pk)
            if row:
                c.sozlesme_sayisi = row["n_sozlesme"]
                c.ihale_sayisi = row["n_ihale"]
                c.idare_sayisi = row["n_idare"]
                c.toplam_sozlesme_bedeli = row["toplam"]
                c.ilk_sozlesme_tarihi = row["ilk"]
                c.son_sozlesme_tarihi = row["son"]
                c.ortalama_indirim_orani = row["ort_indirim"]
                c.indirim_orani_ornek_sayisi = row["n_indirim"]
            else:
                c.sozlesme_sayisi = c.ihale_sayisi = c.idare_sayisi = 0
                c.toplam_sozlesme_bedeli = None
                c.ilk_sozlesme_tarihi = c.son_sozlesme_tarihi = None
                c.ortalama_indirim_orani = None
                c.indirim_orani_ornek_sayisi = 0
            c.uye_sayisi = c.uyelikler.count()
            c.ortak_girisim_sayisi = c.ortakliklar.count()
            c.arama_norm = _build_arama_norm(
                c.kanonik_ad, c.kanonik_anahtar,
                list(c.aliaslar.values_list("ham_ad", flat=True)[:20]),
            )
            c.agrega_guncelleme = now
            objs.append(c)

        with transaction.atomic():
            Contractor.objects.bulk_update(
                objs,
                ["sozlesme_sayisi", "ihale_sayisi", "idare_sayisi", "toplam_sozlesme_bedeli",
                 "ilk_sozlesme_tarihi", "son_sozlesme_tarihi", "ortalama_indirim_orani",
                 "indirim_orani_ornek_sayisi", "uye_sayisi", "ortak_girisim_sayisi",
                 "arama_norm", "agrega_guncelleme"],
                batch_size=500,
            )
        updated += len(objs)

    return updated
