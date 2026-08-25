"""
İhale Asistanı okuma araçları.

Her araç `ekap` katmanındaki MEVCUT sorgu koduna ince bir kırpma sarmalayıcısıdır.
Yeni sorgu YAZILMAZ: `apply_tender_filters`, `sirala_sayfala`, `_cached_count`,
`authority_profile.profil` gibi fonksiyonlar üretimde ölçülmüş performans ve dürüstlük
bilgisini taşıyor (indeks seçimi, `Exists` yerine JOIN kullanmama, kısmi kapsam
uyarıları). Araçta kopyalanan her sorgu bu bilgiyi kaybeder.

Sözleşme: `-> dict`, daima `{"ok": bool, ...}`; exception sızdırmaz.
"""
import logging

from .trim import butceye_sigdir, kes, para

logger = logging.getLogger("ihaletakip")

# Modelin döndürebileceği azami satır — token bütçesinin ilk savunma hattı.
AZAMI_IHALE = 20
AZAMI_FIRMA = 15
AZAMI_SOZLESME = 15
AZAMI_OKAS_TERIM = 10


def _hata(mesaj, **ek):
    return {"ok": False, "hata": mesaj, **ek}


# ── 1. İhale arama ─────────────────────────────────────
def ihale_ara(ctx, **params):
    """
    Ana arama aracı. Parametre adları `apply_tender_filters` ile BİREBİR aynıdır —
    modelin ürettiği sözlük çevrilmeden geçirilir (ara katman = ayrışma kaynağı).
    """
    from ekap.models import Tender
    from ekap.views import (
        _PRO_PARAMS,
        _cached_count,
        apply_tender_filters,
        kismi_kapsam_uyarisi,
        sirala_sayfala,
    )

    params = {k: v for k, v in params.items() if v not in (None, "", [])}

    # Pro kapısı: sabit `ekap.views`'ten IMPORT edilir, kopyalanmaz.
    # ⚠️ `require_premium` ÇAĞRILMAZ — Celery içinde APIException 403'e dönüşmez.
    kullanilan_pro = _PRO_PARAMS & set(params)
    if kullanilan_pro and not ctx.premium:
        from accounts.premium import MSG_PRO_FILTRE

        return _hata(MSG_PRO_FILTRE, kilitli=True, pro_parametreler=sorted(kullanilan_pro))

    params.setdefault("page_size", 10)
    params["page_size"] = min(int(params["page_size"] or 10), AZAMI_IHALE)

    try:
        qs = apply_tender_filters(Tender.objects.all(), params)
        toplam = _cached_count(qs, params)
        items, sayfa, _b = sirala_sayfala(qs, params, azami_boyut=AZAMI_IHALE)
        liste = []
        for t in items:
            kart = ctx.kart_ekle(t)          # card_pool'a yaz → uydurma İKN engeli
            liste.append({**kart, "durum": t.ihale_durum_aciklama or ""})
    except Exception:
        logger.exception("ihale_ara başarısız: %s", params)
        return _hata("İhale araması sırasında bir sorun oldu.")

    sonuc = {"ok": True, "toplam": toplam, "sayfa": sayfa, "liste": liste}
    # Dürüstlük uyarısı sonuca KONUR; system prompt modeli bunu aktarmakla yükümlü kılar.
    uyari = kismi_kapsam_uyarisi(params)
    if uyari:
        sonuc["uyari"] = uyari
    if not liste:
        sonuc["not"] = (
            "Bu filtrelerle sonuç yok. Terimi genelleştirmeyi ya da il/tarih "
            "kısıtını gevşetmeyi öner."
        )
    return butceye_sigdir(sonuc)


# ── 2. İhale detayı ────────────────────────────────────
def ihale_detay(ctx, key=None, **_):
    """Tek ihalenin özeti. `key` İKN ya da ekap_id olabilir."""
    from ekap.views import _tender_by_key

    if not key:
        return _hata("İhale anahtarı (İKN veya ekap_id) gerekli.")
    try:
        # ⚠️ defer_raw=True ŞART: `detail_raw` ~40 KB JSONB, prompt'a girmesi anlamsız.
        t = _tender_by_key(str(key), defer_raw=True)
    except Exception:
        logger.exception("ihale_detay başarısız: %s", key)
        return _hata("İhale bilgisi alınamadı.")
    if not t:
        return _hata(f"{key} sistemde bulunamadı. İKN'yi kontrol edin.")

    kart = ctx.kart_ekle(t)
    okas = [
        {"kod": o.kodu, "ad": kes(o.adi, 90)}
        for o in t.okas_kalemleri.all()[:8]
    ]
    sonuc = {
        "ok": True,
        **kart,
        "usul": t.ihale_usul_aciklama or "",
        "durum": t.ihale_durum_aciklama or "",
        "yasa_kapsami": t.yasa_kapsami,
        "isin_yapilacagi_yer": kes(t.isin_yapilacagi_yer, 200),
        "ihale_yeri": kes(t.ihale_yeri, 200),
        "dokuman_sayisi": t.dokuman_sayisi,
        "sozlesme_sayisi": t.sozlesme_sayisi,
        # ⚠️ null olabilir → model "Veri yok" demeli, 0 dememeli.
        "yaklasik_maliyet": para(t.yaklasik_maliyet_num),
        "toplam_sozlesme_bedeli": para(t.toplam_sozlesme_bedeli),
        "okas_kalemleri": okas,
        "ilan_tarihi": t.ilan_tarihi.strftime("%d.%m.%Y") if t.ilan_tarihi else None,
    }
    if t.iptal_nedeni:
        sonuc["iptal_nedeni"] = kes(t.iptal_nedeni, 200)
    return butceye_sigdir(sonuc, "okas_kalemleri")


# ── 3. OKAS arama (konu → kod köprüsü) ─────────────────
def okas_ara(ctx, terimler=None, **_):
    """
    Konu kelimesini OKAS kataloğuna oturtur. **Çoğul** terim alır: model eşanlamlıları
    ("otomasyon", "scada", "plc") TEK çağrıda gönderir — her ek tur tüm sohbet geçmişini
    yeniden gönderdiği için tur sayısı en pahalı değişkendir.
    """
    from django.db.models import Q

    from ekap.models import OkasCode
    from ekap.utils import normalize_tr

    if isinstance(terimler, str):
        terimler = [terimler]
    terimler = [t.strip() for t in (terimler or []) if t and len(t.strip()) >= 3]
    if not terimler:
        # Trigram indeksi en az 3 karakter ister; kısası seq scan'e düşer.
        return _hata("En az 3 karakterlik terim verin (kısa terim aramada çalışmaz).")

    sonuc = {}
    try:
        for terim in terimler[:6]:
            n = normalize_tr(terim)
            qs = OkasCode.objects.filter(
                Q(adi_norm__contains=n) | Q(kod__startswith=terim)
            ).values("kod", "adi")[:AZAMI_OKAS_TERIM]
            sonuc[terim] = [{"kod": r["kod"], "ad": kes(r["adi"], 80)} for r in qs]
    except Exception:
        logger.exception("okas_ara başarısız: %s", terimler)
        return _hata("OKAS araması sırasında bir sorun oldu.")

    bos = [t for t, v in sonuc.items() if not v]
    cikti = {"ok": True, "sonuc": sonuc}
    if bos:
        cikti["not"] = (
            f"Şu terimler kataloğa oturmadı: {', '.join(bos)}. "
            "Bunlar için ihale adı araması (ihale_ara ihale_adi/q) kullan."
        )
    return cikti


# ── 4. İdare profili ───────────────────────────────────
def idare_profili(ctx, **params):
    """
    "Şu idare kimlere iş vermiş?" sorusunun karşılığı.
    `ekap.authority_profile.profil()` — kapsam çözümü, HHI, yıllara göre seyir hazır.
    """
    from accounts.premium import MSG_IDARE_PROFIL
    from ekap import authority_profile

    if not ctx.premium:
        return _hata(MSG_IDARE_PROFIL, kilitli=True)

    params = {k: v for k, v in params.items() if v not in (None, "", [])}
    if not (params.keys() & {"idare_id", "idare_detsis", "en_ust_idare_kod"}):
        return _hata("İdare belirtin: idare_id, idare_detsis veya en_ust_idare_kod.")

    try:
        veri, hata = authority_profile.profil(params, detay=bool(params.get("detay", True)))
    except Exception:
        logger.exception("idare_profili başarısız: %s", params)
        return _hata("İdare raporu üretilemedi.")
    if hata:
        return _hata(hata)

    yd = veri.get("yuklenici_dagilim") or {}
    dag = veri.get("dagilim") or {}
    sonuc = {
        "ok": True,
        "kapsam": veri.get("kapsam"),
        "toplam": veri.get("toplam"),
        # Son 5 yıl yeter; enflasyon nedeniyle yıllar arası tutar karşılaştırması
        # zaten dikkatli yapılmalı (system prompt'ta yazılı).
        "yillara_gore": (veri.get("yillara_gore") or [])[-5:],
        "yukleniciler": (yd.get("liste") or [])[:10],
        "yogunlasma": yd.get("yogunlasma"),
        "okas_dagilim": (dag.get("okas") or [])[:8],
        "il_dagilim": (dag.get("il") or [])[:5],
    }
    for k in ("mesaj", "uyari"):
        if veri.get(k):
            sonuc[k] = veri[k]
    return butceye_sigdir(sonuc, "yukleniciler")


# ── 5. Firma arama ─────────────────────────────────────
def firma_ara(ctx, q=None, il_id=None, kind=None, min_sozlesme=None, page_size=None, **_):
    from ekap.models import Contractor
    from ekap.utils import normalize_tr

    if not q or len(str(q).strip()) < 3:
        return _hata("Firma adının en az 3 harfini verin.")
    try:
        qs = Contractor.objects.filter(arama_norm__contains=normalize_tr(str(q)))
        if il_id:
            qs = qs.filter(il_id__in=il_id if isinstance(il_id, list) else [il_id])
        if kind:
            qs = qs.filter(kind__in=kind if isinstance(kind, list) else [kind])
        if min_sozlesme:
            qs = qs.filter(sozlesme_sayisi__gte=int(min_sozlesme))
        n = min(int(page_size or 10), AZAMI_FIRMA)
        liste = [
            {
                "id": c.pk,
                "ad": kes(c.kanonik_ad, 120),
                "tur": c.get_kind_display(),
                "il": c.il_adi or None,
                # ⚠️ sozlesme_sayisi ≠ ihale_sayisi (kısımlı ihalede 1 ihale = N sözleşme)
                "sozlesme_sayisi": c.sozlesme_sayisi,
                "ihale_sayisi": c.ihale_sayisi,
                "toplam_bedel": para(c.toplam_sozlesme_bedeli),
                "son_sozlesme": c.son_sozlesme_tarihi.strftime("%d.%m.%Y")
                if c.son_sozlesme_tarihi else None,
            }
            for c in qs.order_by("-sozlesme_sayisi")[:n]
        ]
    except Exception:
        logger.exception("firma_ara başarısız: %s", q)
        return _hata("Firma araması sırasında bir sorun oldu.")
    if not liste:
        return {"ok": True, "liste": [], "not": "Bu ünvanla firma bulunamadı. Ünvanın bir parçasını dene."}
    return butceye_sigdir({"ok": True, "liste": liste})


# ── 6. Firma profili ───────────────────────────────────
def firma_profili(ctx, contractor_id=None, **_):
    from ekap.models import Contract, Contractor
    from ekap.views import ContractorDetailView

    if not contractor_id:
        return _hata("contractor_id gerekli (önce firma_ara ile bul).")
    try:
        c = Contractor.objects.filter(pk=int(contractor_id)).first()
    except (TypeError, ValueError):
        return _hata("contractor_id sayı olmalı.")
    if not c:
        return _hata("Firma bulunamadı.")

    try:
        dag = ContractorDetailView._dagilim(Contract.objects.filter(yuklenici=c))
    except Exception:
        logger.exception("firma_profili dağılım hatası: %s", contractor_id)
        dag = {}

    sonuc = {
        "ok": True,
        "id": c.pk,
        "ad": c.kanonik_ad,
        "tur": c.get_kind_display(),
        "il": c.il_adi or None,
        "istatistik": {
            "sozlesme_sayisi": c.sozlesme_sayisi,
            "ihale_sayisi": c.ihale_sayisi,
            "idare_sayisi": c.idare_sayisi,
            "toplam_bedel": para(c.toplam_sozlesme_bedeli),
            "ilk_sozlesme": c.ilk_sozlesme_tarihi.strftime("%Y") if c.ilk_sozlesme_tarihi else None,
            "son_sozlesme": c.son_sozlesme_tarihi.strftime("%d.%m.%Y") if c.son_sozlesme_tarihi else None,
            # ⚠️ Ortalama TEK BAŞINA gösterilmez; örnek sayısı 0 ise anlamsızdır.
            "ortalama_indirim_orani": para(c.ortalama_indirim_orani),
            "indirim_orani_ornek_sayisi": c.indirim_orani_ornek_sayisi,
        },
        "ihale_tipi_dagilim": (dag.get("ihale_tipi") or [])[:4],
        "il_dagilim": (dag.get("il") or [])[:5],
        "yil_dagilim": (dag.get("yil") or [])[:6],
        "idare_dagilim": (dag.get("idare") or [])[:10],
        # `aliaslar` bilerek DIŞARIDA: 50 satıra kadar çıkar, modele bir şey katmaz.
        "not": "EKAP yalnızca imzalanmış sözleşmeleri yayımlar; kaybedilen teklifler yoktur. Kazanma oranı hesaplanamaz.",
    }
    if not c.uyeleri_cozumlendi:
        sonuc["uyari"] = "Ortak girişim üyeleri güvenle çözümlenemedi."
    else:
        sonuc["uyeler"] = [{"id": m.uye.pk, "ad": kes(m.uye.kanonik_ad, 80)} for m in c.uyelikler.all()[:5]]
    return butceye_sigdir(sonuc, "idare_dagilim")


# ── 7. Firmanın aldığı işler ───────────────────────────
def firma_isleri(ctx, contractor_id=None, il_id=None, ihale_tip=None, yil=None,
                 order=None, page_size=None, **_):
    """"En son ne iş almış" — sözleşme geçmişi, en yenisi başta."""
    from ekap.models import Contract

    if not contractor_id:
        return _hata("contractor_id gerekli (önce firma_ara ile bul).")
    try:
        qs = Contract.objects.filter(yuklenici_id=int(contractor_id)).select_related("tender")
        if il_id:
            qs = qs.filter(il_id__in=il_id if isinstance(il_id, list) else [il_id])
        if ihale_tip:
            qs = qs.filter(ihale_tip__in=ihale_tip if isinstance(ihale_tip, list) else [ihale_tip])
        if yil:
            qs = qs.filter(sozlesme_tarihi__year=int(yil))
        alan = "sozlesme_bedeli_num" if order == "sozlesme_bedeli" else "sozlesme_tarihi"
        n = min(int(page_size or 10), AZAMI_SOZLESME)
        liste = []
        for s in qs.order_by(f"-{alan}")[:n]:
            t = s.tender
            if t:
                ctx.kart_ekle(t)     # kullanıcı "şu işi göster" diyebilsin
            liste.append({
                "ikn": t.ikn if t else None,
                "ihale_adi": kes(t.ihale_adi, 140) if t else "",
                "idare_adi": kes(t.idare_adi, 90) if t else "",
                "il": t.ihale_il_adi if t else None,
                "sozlesme_tarihi": s.sozlesme_tarihi.strftime("%d.%m.%Y") if s.sozlesme_tarihi else None,
                "sozlesme_bedeli": para(s.sozlesme_bedeli_num),
                "indirim_orani": para(s.indirim_orani),   # ORAN (0.21 = %21), yüzde değil
            })
    except Exception:
        logger.exception("firma_isleri başarısız: %s", contractor_id)
        return _hata("Firma işleri alınamadı.")
    return butceye_sigdir({
        "ok": True,
        "liste": liste,
        "not": "Bunlar firmanın ALDIĞI işlerdir; katıldığı/kaybettiği ihaleler EKAP'ta yayımlanmaz.",
    })


# ── 8. Kullanıcının kendi kayıtları ────────────────────
def kullanicinin_verisi(ctx, tur=None, limit=None, **_):
    from tenders.models import (
        FavoriteAuthority, FavoriteContractor, SavedFilter, SavedTender, TenderAlarm,
    )

    n = min(int(limit or 10), 20)
    u = ctx.user
    if u is None:
        return _hata("Kullanıcı bağlamı yok.")
    try:
        if tur == "kayitli_ihaleler":
            rows = SavedTender.objects.filter(user=u).order_by("-saved_at")[:n]
            return {"ok": True, "liste": [
                {"ikn": r.tender_ikn, "ihale_adi": kes(r.tender_title, 120),
                 "idare_adi": kes(r.institution, 90), "klasor": r.group.name if r.group_id else "Genel"}
                for r in rows]}
        if tur == "kayitli_filtreler":
            rows = SavedFilter.objects.filter(user=u).order_by("-created_at")[:n]
            return {"ok": True, "liste": [
                {"id": r.pk, "ad": r.name, "alarm": r.alarm, "filtreler": r.filters} for r in rows]}
        if tur == "alarmlar":
            rows = TenderAlarm.objects.filter(user=u).order_by("-created_at")[:n]
            return {"ok": True, "liste": [
                {"ikn": getattr(r, "tender_ikn", None) or r.tender_id,
                 "ihale_adi": kes(getattr(r, "tender_title", ""), 120)} for r in rows]}
        if tur == "favori_idareler":
            rows = FavoriteAuthority.objects.filter(user=u)[:n]
            return {"ok": True, "liste": [
                {"detsis_no": r.detsis_no, "ad": kes(r.name, 120), "alarm": getattr(r, "alarm", None)}
                for r in rows]}
        if tur == "favori_firmalar":
            rows = FavoriteContractor.objects.filter(user=u).select_related("contractor")[:n]
            return {"ok": True, "liste": [
                {"id": r.contractor_id, "ad": kes(getattr(r.contractor, "kanonik_ad", ""), 120),
                 "alarm": getattr(r, "alarm", None)} for r in rows]}
    except Exception:
        logger.exception("kullanicinin_verisi başarısız: %s", tur)
        return _hata("Kayıtlarınız alınamadı.")
    return _hata("Geçersiz tür. Seçenekler: kayitli_ihaleler, kayitli_filtreler, alarmlar, favori_idareler, favori_firmalar.")


# ── 9. İdare arama ─────────────────────────────────────
def idare_ara(ctx, q=None, take=None, **_):
    """
    İdareyi ADIYLA arayıp `idare_id` / `detsis_no` çözer.

    ⚠️ Bu araç olmadan `idare_profili` kullanılamaz: model idare adını biliyor ama
    kimliğini bilmiyor. (Canlı testte "Toplu Konut İdaresi kimlere iş vermiş?" sorusu
    tam da bu yüzden yarım kalmıştı.)

    `only_with_tenders` bilerek AÇIK: DETSIS'te aynı kurum birçok kez farklı idare_id
    ile bulunur ve ihaleler yalnızca birinin altındadır; ihalesi olmayan kopyalar
    modeli çıkmaza sokar.
    """
    from django.db.models import Q

    from ekap.detsis_tree import annotate_paths, tender_idare_id_set
    from ekap.models import Authority
    from ekap.utils import normalize_tr

    if not q or len(str(q).strip()) < 3:
        return _hata("İdare adının en az 3 harfini verin.")
    try:
        qs = Authority.objects.filter(
            Q(ad_norm__contains=normalize_tr(str(q))) | Q(idare_id__startswith=str(q))
        ).filter(Q(has_items=True) | Q(idare_id__in=tender_idare_id_set()))
        nodes = list(qs.order_by("ad")[: min(int(take or 10), 15)])
        # Seçilebilir (idare_id dolu) düğümler önce — model doğrudan kullanabilsin.
        nodes.sort(key=lambda a: (a.idare_id == "", a.ad))
        yollar = annotate_paths(nodes)
        liste = [
            {
                "ad": kes(a.ad, 120),
                "idare_id": a.idare_id or None,
                "detsis_no": a.detsis_no,
                # `annotate_paths` liste döner; modele düz metin ver ("A > B").
                "yol": kes(" > ".join(yollar.get(a.detsis_no) or []), 160),
            }
            for a in nodes
        ]
    except Exception:
        logger.exception("idare_ara başarısız: %s", q)
        return _hata("İdare araması sırasında bir sorun oldu.")
    if not liste:
        return {"ok": True, "liste": [], "not": "Bu adla idare bulunamadı. Adın bir parçasını dene."}
    return butceye_sigdir({
        "ok": True,
        "liste": liste,
        "not": "idare_profili için: yaprak birim ise idare_id, üst kurum ise detsis_no kullan.",
    })
