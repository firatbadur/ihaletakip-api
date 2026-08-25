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

from django.db.models import F

from .trim import butceye_sigdir, kes, para

logger = logging.getLogger("ihaletakip")

# Modelin döndürebileceği azami satır — token bütçesinin ilk savunma hattı.
AZAMI_IHALE = 20
AZAMI_FIRMA = 15
AZAMI_SOZLESME = 15
AZAMI_OKAS_TERIM = 10
AZAMI_BENZER = 8
AZAMI_SERI = 10

# Tekrar eden seri sıralaması: sıradaki ilanı en yakın olan önce; tarihi bilinmeyen
# seriler (beklenen_ilan_tarihi NULL) sona. `RecurringSeriesListView` ile aynı kural.
_SERI_SIRA = F("beklenen_ilan_tarihi").asc(nulls_last=True)


def _hata(mesaj, **ek):
    return {"ok": False, "hata": mesaj, **ek}


def son_yillar(liste, n=5):
    """
    Yıl serisinden EN YENİ `n` yılı, kronolojik (eskiden yeniye) sırada döner.

    ⚠️ `ekap.authority_profile._yillara_gore` seriyi **azalan** üretir
    (`sorted(..., reverse=True)`, en yeni başta). Burada `[-n:]` yazmak en ESKİ n yılı
    verir. Bu hata üretimde yakalandı: TOKİ profili 2016-2026 kapsarken modele yalnızca
    2016-2020 gidiyordu ve model —doğru biçimde— "2016-2020 arası" diye yazıyordu.
    Model uydurmuyordu; yanlış veriyi doğru okuyordu. En sinsi hata sınıfı budur.
    """
    return list(reversed((liste or [])[:n]))


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
        "yillara_gore": son_yillar(veri.get("yillara_gore")),
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
        # ⚠️ Bu liste YILA göre değil ADETE göre sıralıdır (`_dagilim` `-adet` kullanır):
        # "en yoğun 6 yıl" demektir, "son 6 yıl" değil. Anahtar adı bunu belli etsin.
        "en_yogun_yillar": (dag.get("yil") or [])[:6],
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
        if tur == "yaklasan":
            # ⚠️ `SavedTender.tender_date` bir CharField'dır (serbest metin) — ona göre
            # sıralamak "10.03.2026" ile "9.3.2026"yı yanlış sıralar. Gerçek tarih
            # `ekap.Tender.ihale_tarihi`de; İKN üzerinden JOIN'liyoruz.
            from datetime import datetime
            from datetime import time as dt_time

            from django.utils import timezone

            from ekap.models import Tender

            iknler = set(
                SavedTender.objects.filter(user=u).values_list("tender_ikn", flat=True)
            )
            iknler |= set(
                TenderAlarm.objects.filter(user=u, completed=False)
                .exclude(tender_ikn__isnull=True).exclude(tender_ikn="")
                .values_list("tender_ikn", flat=True)
            )
            if not iknler:
                return {"ok": True, "liste": [],
                        "not": "Kullanıcının kayıtlı ihalesi ya da alarmı yok."}
            bugun = timezone.localdate()
            # ⚠️ `ihale_tarihi` DateTimeField: sınırı `date` olarak vermek Django'da
            # naive-datetime uyarısı üretir ve sınır UTC'ye göre kayabilir. Yerel gün
            # başlangıcını aware datetime olarak kuruyoruz.
            gun_basi = timezone.make_aware(
                datetime.combine(bugun, dt_time.min), timezone.get_current_timezone()
            )
            rows = (
                Tender.objects.filter(ikn__in=sorted(iknler), ihale_tarihi__gte=gun_basi)
                .defer("detail_raw", "list_raw")
                .order_by("ihale_tarihi")[:n]
            )
            liste = []
            for t in rows:
                kart = ctx.kart_ekle(t)      # kullanıcı "buna alarm kur" diyebilsin
                # ⚠️ `Tender.ihale_tarihi` DateTimeField'dır (DateField DEĞİL) —
                # doğrudan `date`ten çıkarmak TypeError verir. Yerel güne indiriyoruz;
                # "kalan gün" kullanıcının takvimine göre okunmalı, UTC'ye göre değil.
                gun = timezone.localtime(t.ihale_tarihi).date()
                liste.append({**kart, "kalan_gun": (gun - bugun).days})
            return {"ok": True, "liste": liste,
                    "not": "Yalnızca ihale tarihi BUGÜN veya sonrası olanlar; "
                           "tarihi geçmiş kayıtlar listelenmez."}
        if tur == "onerilerim":
            from assistant.models import TenderRecommendation

            rows = (
                TenderRecommendation.objects.filter(user=u)
                .select_related("tender")
                .defer("tender__detail_raw", "tender__list_raw")
                .order_by("-date", "-score")[:n]
            )
            liste = []
            for r in rows:
                kart = ctx.kart_ekle(r.tender)
                liste.append({**kart, "skor": round(r.score, 1),
                              "gerekce": r.reasons or [], "gorulduMu": r.seen})
            return {"ok": True, "liste": liste,
                    "not": "gerekce alanı, öneriyi üreten KURAL TABANLI eşleşmedir "
                           "(anahtar kelime/OKAS/il/tür). 'Neden önerdin' sorusunda "
                           "bunu aynen aktar, yeni gerekçe uydurma."}
    except Exception:
        logger.exception("kullanicinin_verisi başarısız: %s", tur)
        return _hata("Kayıtlarınız alınamadı.")
    return _hata("Geçersiz tür. Seçenekler: kayitli_ihaleler, kayitli_filtreler, alarmlar, "
                 "favori_idareler, favori_firmalar, yaklasan, onerilerim.")


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


# ── 10. Fiyat analizi (benchmark) ──────────────────────
def ihale_benchmark(ctx, key=None, yil_geri=None, kapsam=None, **_):
    """
    "Bu iş kaça kapanır / ne kadar indirim verilir?" sorusunun karşılığı.

    ⚠️ Free maskelemesi YAPILMAZ: asistan ucu `require_premium(MSG_CHAT)` ile
    korunuyor (assistant/views.py::AssistantChatView), buraya yalnızca Pro üye
    ulaşır. `TenderBenchmarkView._KILITLI` maskesi HTTP ucuna aittir; burada
    tekrarlanırsa Pro kullanıcıya boş veri gösteririz.
    """
    from ekap import benchmark as benchmark_mod
    from ekap.views import _tender_by_key

    if not key:
        return _hata("İhale anahtarı (İKN veya ekap_id) gerekli.")
    try:
        # ⚠️ defer_raw YOK: `benchmark()` ilk satırda `tender.detail_synced_at`e ve
        # `_merdiven` içinde OKAS/idare alanlarına bakıyor; `_tender_by_key`nin tam
        # nesnesi gerekiyor.
        t = _tender_by_key(str(key))
        if t is None:
            return _hata(f"{key} sistemde bulunamadı.")
        veri, hata = benchmark_mod.benchmark(
            t, yil_geri=yil_geri or benchmark_mod.VARSAYILAN_YIL, kapsam=kapsam or "auto",
            limit=AZAMI_BENZER,
        )
    except Exception:
        logger.exception("ihale_benchmark başarısız: %s", key)
        return _hata("Fiyat analizi üretilemedi.")
    if hata:
        return _hata(hata)

    ornek = veri.get("ornek") or {}
    sonuc = {
        "ok": True,
        "ihale": veri.get("ihale"),
        # `kapsam.aciklama` modele ZORUNLU: aynı sayı "bu idarede" ile "Türkiye
        # genelinde" tamamen farklı anlam taşır.
        "kapsam": veri.get("kapsam"),
        "ornek": ornek,
        "indirim_orani": veri.get("indirim_orani"),
        "ortalama_indirim_orani": veri.get("ortalama_indirim_orani"),
        "sozlesme_bedeli": veri.get("sozlesme_bedeli"),
        # ⚠️ benchmark._yillara_gore `.order_by("-yil")` üretir → AZALAN.
        # `son_yillar` doğru araç. (market.grup_detayi ARTAN üretir, orada değil.)
        "yillara_gore": son_yillar(veri.get("yillara_gore")),
        "rekabet": veri.get("rekabet"),
        "fiyat_disi_unsur_var": veri.get("fiyat_disi_unsur_var"),
        "benzer_ihaleler": [
            {
                "ikn": b.get("ikn"),
                "ihale_adi": kes(b.get("ihale_adi"), 110),
                "idare_adi": kes(b.get("idare_adi"), 80),
                "tarih": b.get("sozlesme_tarihi"),
                "sozlesme_bedeli": b.get("sozlesme_bedeli"),
                "indirim_orani": b.get("indirim_orani"),
                "teklif_sayisi": b.get("teklif_sayisi"),
                "yuklenici": (b.get("yuklenici") or {}).get("ad"),
            }
            for b in (veri.get("benzer_ihaleler") or [])
        ],
    }
    if veri.get("uyari"):
        sonuc["uyari"] = veri["uyari"]
    if ornek.get("yeterli_veri") is False:
        sonuc["not"] = (
            "Örneklem yetersiz: dağılımı rakamla verme, 'bu iş için karşılaştırılabilir "
            "yeterli sözleşme yok' de ve kapsamı genişletmeyi öner."
        )
    return butceye_sigdir(sonuc, "benzer_ihaleler")


# ── 11. Tekrar eden ihaleler ───────────────────────────
def tekrar_eden_ihaleler(ctx, key=None, beklenen_gun=None, page_size=None, **params):
    """
    İki kip:
      · `key` verilirse → BU ihalenin serisi + geçmiş örnekleri
        ("bu iş her yıl açılıyor mu, geçen sene kaça verilmişti").
      · verilmezse → filtreli seri listesi ("bu idare önümüzdeki 60 günde ne açar").

    ⚠️ `beklenen_ilan_tarihi` bir TAHMİNDİR. `guven` alanı cevaba mutlaka taşınmalı;
    `dusuk` güvende tarih kesin verilmemeli.
    """
    from ekap.models import RecurringTenderSeries, Tender
    from ekap.views import _seri_dict, _tender_by_key

    n = min(int(page_size or 10), AZAMI_SERI)
    try:
        if key:
            t = _tender_by_key(str(key), defer_raw=True)
            if t is None:
                return _hata(f"{key} sistemde bulunamadı.")
            if not t.seri_anahtar:
                return {
                    "ok": True, "seri": None, "gecmis": [],
                    "not": "Bu ihale tekrar eden bir seriye ait değil; benzerleri için "
                           "ihale_benchmark kullan.",
                }
            seri = RecurringTenderSeries.objects.filter(
                seri_anahtar=t.seri_anahtar, idare_id=t.idare_id
            ).first()
            gecmis = (
                Tender.objects.filter(seri_anahtar=t.seri_anahtar, idare_id=t.idare_id)
                .exclude(pk=t.pk)
                .only("ikn", "ekap_id", "ihale_adi", "ilan_tarihi", "sozlesme_bedeli_num")
                .order_by("-ilan_tarihi")[:n]
            )
            return butceye_sigdir({
                "ok": True,
                "seri": _seri_dict(seri) if seri else None,
                "gecmis": [
                    {
                        "ikn": g.ikn,
                        "ihale_adi": kes(g.ihale_adi, 110),
                        "ilan_tarihi": g.ilan_tarihi.isoformat() if g.ilan_tarihi else None,
                        "sozlesme_bedeli": para(g.sozlesme_bedeli_num),
                    }
                    for g in gecmis
                ],
            }, "gecmis")

        qs = RecurringTenderSeries.objects.filter(aktif=True)
        for alan in ("idare_id", "en_ust_idare_kod", "okas_ana_kod", "il_id",
                     "ihale_tip", "periyot_tip", "guven"):
            deger = params.get(alan)
            if deger:
                qs = qs.filter(**{f"{alan}__in": deger if isinstance(deger, list) else [deger]})
        detsis = (params.get("idare_detsis") or "").strip() if params.get("idare_detsis") else ""
        if detsis:
            from ekap.detsis_tree import descendant_idare_ids, tender_idare_id_set

            genis = descendant_idare_ids([detsis]) & tender_idare_id_set()
            qs = qs.filter(idare_id__in=sorted(genis)) if genis else qs.none()
        if beklenen_gun:
            from datetime import timedelta

            from django.utils import timezone

            bugun = timezone.localdate()
            qs = qs.filter(
                beklenen_ilan_tarihi__gte=bugun,
                beklenen_ilan_tarihi__lte=bugun + timedelta(days=int(beklenen_gun)),
            )
        toplam = qs.count()
        satirlar = qs.order_by(_SERI_SIRA)[:n]
        liste = [_seri_dict(s) for s in satirlar]
    except Exception:
        logger.exception("tekrar_eden_ihaleler başarısız: %s / %s", key, params)
        return _hata("Tekrar eden ihale bilgisi alınamadı.")

    sonuc = {"ok": True, "toplam": toplam, "liste": liste}
    if not liste:
        sonuc["not"] = (
            "Bu filtrelerle tekrar eden seri yok. Seri kurulması için aynı idarede aynı "
            "işin en az 2 kez açılmış olması gerekir; kısıtı gevşetmeyi öner."
        )
    else:
        sonuc["not"] = (
            "beklenen_ilan_tarihi TAHMİNDİR. guven='dusuk' olan satırlarda tarihi kesin "
            "verme, '≈' ile yaklaşık söyle."
        )
    return butceye_sigdir(sonuc)


# ── 12. Pazar panosu ───────────────────────────────────
def pazar_panosu(ctx, okas_bucket=None, yil=None, limit=None):
    """
    `okas_bucket` yoksa yılın genel görünümü + en büyük iş grupları;
    varsa o iş grubunun yıllara göre seyri, il/firma kırılımı ve yoğunlaşması.

    ⚠️ İhale listesine geçerken `okas_kod` kullanılır (önek eşleşir), `okas_ana_kod`
    DEĞİL: `okas_bucket = okas_ana_kod[:4]` olduğu için tam eşleşme boş döner.
    """
    from ekap import market as market_mod

    n = min(int(limit or 10), 20)
    try:
        if okas_bucket is not None and str(okas_bucket).strip():
            veri, hata = market_mod.grup_detayi(str(okas_bucket).strip(), yil=yil, limit=n)
        else:
            veri, hata = market_mod.genel_bakis(yil, limit=n)
    except Exception:
        logger.exception("pazar_panosu başarısız: %s / %s", okas_bucket, yil)
        return _hata("Pazar panosu verisi alınamadı.")
    if hata:
        return _hata(hata)

    sonuc = {"ok": True, **veri}
    # ⚠️ market.grup_detayi serisi ARTAN üretir (`order_by("yil")`) — burada
    # `son_yillar` KULLANILMAZ, kuyruk alınır. (benchmark AZALAN üretir, orada tersi.)
    if isinstance(sonuc.get("yillara_gore"), list):
        sonuc["yillara_gore"] = sonuc["yillara_gore"][-6:]
    for anahtar in ("iller", "firmalar", "gruplar"):
        if isinstance(sonuc.get(anahtar), list):
            sonuc[anahtar] = sonuc[anahtar][:n]
    sonuc["not"] = (
        "hhi 0–1 aralığındadır, YÜZDE DEĞİLDİR (0,25 = orta yoğunlaşma). "
        "okas_bucket='' 'Sınıflandırılmamış' gerçek veridir, listeden düşürme. "
        "Yıllar arası tutar karşılaştırmasında enflasyon notu düş."
    )
    return butceye_sigdir(sonuc, "firmalar")


# ── 13. Doküman analizi (varsa) ────────────────────────
def dokuman_analizi(ctx, ikn=None, tur=None, **_):
    """
    İhaleye ait DAHA ÖNCE üretilmiş şartname/maliyet analizini döner.

    ⚠️ Asistan dokümanı KENDİ İNDİREMEZ: EKAP doküman indirme captcha'lı bir
    kullanıcı akışıdır (mobil `TenderDetail` → Dokümanlar). `AnalysisCache` ise
    kullanıcı bazlı DEĞİL (ikn, analysis_type) bazlıdır; başka bir kullanıcı analiz
    ettiyse sonuç burada hazır durur. Yoksa uydurma — kullanıcıyı uygulama içindeki
    akışa yönlendir (EKAP'a DEĞİL).
    """
    from ai.models import AnalysisCache

    if not ikn:
        return _hata("İKN gerekli.")
    try:
        qs = AnalysisCache.objects.filter(ikn=str(ikn).strip())
        if tur:
            qs = qs.filter(analysis_type=tur)
        satirlar = list(qs.order_by("-updated_at")[:2])
    except Exception:
        logger.exception("dokuman_analizi başarısız: %s", ikn)
        return _hata("Analiz kaydı okunamadı.")

    if not satirlar:
        return {
            "ok": True,
            "liste": [],
            "not": (
                "Bu ihale için hazır analiz yok. Kullanıcıya: ihale detayındaki "
                "Dokümanlar sekmesinden şartnameyi indirip Analiz sekmesinde "
                "analiz ettirebileceğini söyle. EKAP'a yönlendirme."
            ),
        }
    return butceye_sigdir({
        "ok": True,
        "liste": [
            {
                "tur": s.get_analysis_type_display(),
                "tarih": s.updated_at.strftime("%d.%m.%Y"),
                "metin": kes(s.analysis, 3000),
            }
            for s in satirlar
        ],
        "not": "Bu analiz uygulama içinde yüklenen dokümandan üretildi; özetleyerek aktar.",
    })
