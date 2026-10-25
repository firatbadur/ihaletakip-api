"""
Doğrudan temin (DT) toplama — mobil API `DogrudanTemin/Liste`.

İki iş vardır, ikisi de `ekap.mobil.tasks.dt_tik` içinden çağrılır:

1. **Yeni ilan yoklaması** (`yoklama`) — bugün yayımlanmış açık DT'ler, dtn azalan,
   1 sayfa (gerekirse 2). DT'lerin bir kısmı aynı gün, saatler içinde kapandığı için
   mesai içinde sık koşar (`EKAP_MOBIL_DT_ARALIK_DK`).
2. **Tam açık taraması** (`tam_tarama_adimi`) — son `EKAP_DT_ACIK_AZAMI_GUN` günün
   tüm açık DT'leri, **tur başına bir sayfa**. Amacı iptal edilen / sonuca geçen
   kayıtları "açık" dışına almak: o kayıtlar 202 listesinden düşer, tam taramalarda
   görülmeyince gizlenir.

⚠️ Kararlı anahtar **dtn**; `encID` her çağrıda farklı üretilir (ölçüldü).

⚠️⚠️ "Açık" tanımının TEK kaynağı `acik_q` — liste ucu ve bildirim görevi aynı
fonksiyonu kullanır. İki yerde ayrı yazılsaydı bildirimdeki sayı ekrandaki listeyle
tutmazdı (bu kod tabanında üç kez yaşanan arıza sınıfı).
"""
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ..models import City, DogrudanTemin, SyncCheckpoint
from ..utils import normalize_tr
from . import constants as C
from . import idare as idare_mod

logger = logging.getLogger("ihaletakip")

CHECKPOINT = "mobil_dt"
# Tam taramada bu kadar sayfadan sonra tur bitmiş sayılmaz, kesilir: ~10 bin açık DT
# beklenen hacmin kat kat üstüdür → filtre bozulmuş demektir. Kesilen tur "görülmedi"
# kararı VERMEZ (yarım tur hayatta olanları gizlerdi).
_TAM_AZAMI_SAYFA = 40


# ── Ayrıştırma ─────────────────────────────────────────
def dtn_parcala(dtn: str):
    """`"26DT2112816"` → `("2026", "2112816")`; biçim tutmazsa `(None, None)`."""
    s = (dtn or "").strip().upper()
    yil, _, sayi = s.partition("DT")
    if len(yil) != 2 or not yil.isdigit() or not sayi.isdigit():
        return None, None
    return f"20{yil}", sayi


def _tarih_saat(metin):
    """`"30.10.2026 11:30"` → aware datetime (TR). "Tarih Belirtilmemiş" → None."""
    s = (metin or "").strip()
    for bicim in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return timezone.make_aware(datetime.strptime(s, bicim))
        except ValueError:
            continue
    return None


def _tarih(metin):
    try:
        return datetime.strptime((metin or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _ekap_tarih(gun) -> str:
    """Liste filtresinin istediği biçim: `dd.mm.yyyy` (ISO değil — ölçüldü)."""
    return gun.strftime("%d.%m.%Y")


def _kod(harita: dict, metin):
    """Metin → kod. ⚠️ Eşleşmezse None: uydurma kod filtreleri sessizce bozardı."""
    if not metin:
        return None
    kod = harita.get(normalize_tr(metin))
    if kod is None:
        logger.warning("DT: bilinmeyen metin %r", str(metin)[:80])
    return kod


def _madde_kod(metin):
    # ⚠️ Katlanmadan eşlenir: normalize_tr "22-ı" ile "22-i"yi aynı dizgeye indirir.
    if not metin:
        return None
    kod = C.DT_MADDE_METIN.get(str(metin).strip())
    if kod is None:
        logger.warning("DT: bilinmeyen madde %r", str(metin)[:80])
    return kod


# Model alanı → onu besleyen liste anahtarı. ⚠️ Koruyucu yazma: anahtar `item`'da hiç
# yoksa alan ellenmez ("anahtar yok" = "veri yok", "değeri sil" DEĞİL — bkz.
# `sync._LISTE_KAYNAK_ANAHTARI`). Yeni alan eklerken buraya da ekleyin.
_LISTE_ALANLARI = {
    "ad": ("dogrudanTeminAdi", lambda v: (v or "").strip()),
    "idare_adi": ("adi", lambda v: (v or "").strip()[:500]),
    "il_adi": ("il", lambda v: (v or "").strip().upper()[:100]),
    "tur": ("dogrudanTeminTipi", lambda v: _kod(C.DT_TUR_METIN, v)),
    "madde": ("dogrudanTeminYasaKapsamiTeminMadde", lambda v: (v or "")[:100]),
    "madde_kod": ("dogrudanTeminYasaKapsamiTeminMadde", _madde_kod),
    "istisna_dayanagi": ("dogrudanTeminIstisnaAliminDayanagi", lambda v: (v or "")[:200]),
    "durum": ("dogrudanTeminDurum", lambda v: _kod(C.DT_DURUM_METIN, v)),
    "durum_metin": ("dogrudanTeminDurum", lambda v: (v or "")[:120]),
    "son_teklif": ("dogrudanTeminTarihSaat", _tarih_saat),
    "ilan_tarihi": ("ilanTarihi", _tarih),
    "ilanli": ("dogrudanTeminIlaniVarMi", bool),
    "dokuman_var": ("dogrudanTeminDokumaniVarMi", bool),
}


def satirdan(item: dict) -> dict:
    """Liste satırı → model alanları (yalnızca satırda anahtarı olanlar)."""
    out = {}
    for alan, (anahtar, cevir) in _LISTE_ALANLARI.items():
        if anahtar in item:
            out[alan] = cevir(item.get(anahtar))
    if "ad" in out:
        out["ad_norm"] = normalize_tr(out["ad"])
    if "idare_adi" in out:
        out["idare_adi_norm"] = normalize_tr(out["idare_adi"])[:500]
    return out


def _il_haritasi() -> dict:
    return dict(City.objects.values_list("ad", "ekap_il_id"))


# ── Upsert ─────────────────────────────────────────────
def upsert(satirlar, *, simdi=None) -> dict:
    """
    Liste satırlarını dtn ile yazar. `{"yeni": [dtn…], "guncel": n}` döner.

    ⚠️ `yeni` bildirim görevinin girdisidir: bu turda DB'ye **ilk kez** giren dtn'ler.
    """
    simdi = simdi or timezone.now()
    tekil = {}
    for item in satirlar or []:
        dtn = (item.get("dtn") or "").strip().upper()
        if dtn:
            tekil[dtn] = item
    if not tekil:
        return {"yeni": [], "guncel": 0}

    mevcut = {d.dtn: d for d in DogrudanTemin.objects.filter(dtn__in=list(tekil))}
    iller = _il_haritasi()
    yeniler, guncel = [], []
    for dtn, item in tekil.items():
        alanlar = satirdan(item)
        if alanlar.get("il_adi"):
            alanlar["il_id"] = iller.get(alanlar["il_adi"])
        obj = mevcut.get(dtn)
        if obj is None:
            obj = DogrudanTemin(dtn=dtn, **alanlar)
            # ⚠️ İdare yalnızca yaratmada çözülür: ad değişmez, her turda 250 satır
            # için trigram araması koşturmak boşuna DB yükü olurdu (sonuç 24 sa
            # önbellekli olsa da).
            if obj.idare_adi:
                obj.idare_id, obj.idare_kaynak = idare_mod.coz(obj.idare_adi)
            yeniler.append(obj)
        else:
            for alan, deger in alanlar.items():
                setattr(obj, alan, deger)
            guncel.append(obj)
        if item.get("encID"):
            obj.enc_id = str(item["encID"])[:128]
            obj.enc_id_at = simdi
        obj.list_raw = item
        obj.son_gorulme = simdi

    if yeniler:
        DogrudanTemin.objects.bulk_create(yeniler, batch_size=500, ignore_conflicts=True)
    if guncel:
        alanlar = sorted(
            {a for a in _LISTE_ALANLARI} | {"ad_norm", "idare_adi_norm", "il_id",
                                             "enc_id", "enc_id_at", "list_raw",
                                             "son_gorulme"}
        )
        # `auto_now` bulk_update'te uygulanmaz → elle.
        for o in guncel:
            o.updated_at = simdi
        DogrudanTemin.objects.bulk_update(guncel, alanlar + ["updated_at"], batch_size=500)
    return {"yeni": [o.dtn for o in yeniler], "guncel": len(guncel)}


# ── "Açık" tanımı (TEK KAYNAK) ─────────────────────────
def _cp():
    cp, _ = SyncCheckpoint.objects.get_or_create(name=CHECKPOINT)
    if not isinstance(cp.extra, dict):
        cp.extra = {}
    return cp


def gorulme_esigi():
    """
    Bir DT'nin "hâlâ listede" sayılması için en geç görülmüş olması gereken an.

    ⚠️ **Son İKİ tam taramanın eskisinin başlangıcı**, sonuncununki değil. Sayfalı
    tarama sırasında bir kayıt kapanırsa arkadaki satırlar bir sayfa yukarı kayar ve
    sayfa sınırındaki bir kayıt o turda **hiç görülmeyebilir**. Tek tura bakan eşik
    onu (gerçekte açık olduğu hâlde) gizlerdi; iki ardışık turda birden kaçması ise
    pratikte olmaz. Bedeli: iptal edilen kayıt en geç iki tur (≈4 sa) daha görünür —
    son teklif anı geçen kayıt zaten anında düşer.
    """
    cp = SyncCheckpoint.objects.filter(name=CHECKPOINT).first()
    baslar = ((cp.extra or {}).get("tam_baslar") if cp else None) or []
    if not baslar:
        return None
    try:
        return datetime.fromisoformat(baslar[0])
    except (TypeError, ValueError):
        return None


def acik_q(simdi=None):
    """
    Teklif verilebilir DT: durum 202 + son teklif anı geçmemiş (ya da belirtilmemiş)
    + ilanı `EKAP_DT_ACIK_AZAMI_GUN`'den yeni + tam taramalarda görülmeye devam ediyor.

    ⚠️ İlan yaşı tavanı zombi kayıtlar içindir: ölçüldü, 25DT2095274 (ilan
    21.11.2025) son teklif "24.11.2028" yazılı ve hâlâ 202 — yalnızca tarihe bakan bir
    tanım bunu yıllarca "açık" listelerdi.
    """
    simdi = simdi or timezone.now()
    taban = timezone.localdate(simdi) - timedelta(
        days=getattr(settings, "EKAP_DT_ACIK_AZAMI_GUN", 30)
    )
    q = (
        Q(durum=C.DT_DURUM_ACIK)
        & (Q(son_teklif__gt=simdi) | Q(son_teklif__isnull=True))
        & Q(ilan_tarihi__gte=taban)
    )
    esik = gorulme_esigi()
    if esik is not None:
        q &= Q(son_gorulme__gte=esik)
    return q


# ── Zamanlama ──────────────────────────────────────────
def _saat(metin, varsayilan):
    try:
        s, d = str(metin).split(":")
        return int(s) * 60 + int(d)
    except (TypeError, ValueError):
        return varsayilan


def mesai_mi(simdi=None) -> bool:
    """Hafta içi + `EKAP_MOBIL_DT_MESAI_BAS`–`BIT` (yerel saat)."""
    yerel = timezone.localtime(simdi or timezone.now())
    if yerel.weekday() >= 5:
        return False
    dk = yerel.hour * 60 + yerel.minute
    bas = _saat(getattr(settings, "EKAP_MOBIL_DT_MESAI_BAS", "07:30"), 450)
    bit = _saat(getattr(settings, "EKAP_MOBIL_DT_MESAI_BIT", "18:30"), 1110)
    return bas <= dk < bit


def _gecti_mi(iso, dakika, simdi) -> bool:
    if not iso:
        return True
    try:
        son = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return True
    # Beat */5 tetikler; 1 dk pay, aralığa tam denk gelen turun kaçmaması için.
    return simdi - son >= timedelta(minutes=dakika) - timedelta(minutes=1)


def yoklama_zamani_mi(simdi=None) -> bool:
    simdi = simdi or timezone.now()
    dakika = (getattr(settings, "EKAP_MOBIL_DT_ARALIK_DK", 5) if mesai_mi(simdi)
              else getattr(settings, "EKAP_MOBIL_DT_GECE_ARALIK_DK", 30))
    cp = SyncCheckpoint.objects.filter(name=CHECKPOINT).first()
    return _gecti_mi((cp.extra or {}).get("son_yoklama") if cp else None, dakika, simdi)


# ── İşler ──────────────────────────────────────────────
def yoklama(cli, *, simdi=None, azami_sayfa: int = 2) -> dict:
    """
    Bugün yayımlanmış açık DT'ler (dtn azalan). Genelde **1 istek**.

    Sayfa dolu (250) ve son satırı da yeniyse bir sonraki sayfaya geçilir; son satır
    zaten biliniyorsa daha eskiler de biliniyordur (dtn azalan) → durulur.
    ⚠️ Kayıt sırası ≠ yayım sırası: eski dtn'li bir DT bugün yayımlanırsa listenin
    aşağısında kalabilir; günde 250'den fazla ilan varsa o kayıt bir sonraki tam
    taramada (≤ `EKAP_MOBIL_DT_TAM_ARALIK_DK`) yakalanır.
    """
    simdi = simdi or timezone.now()
    gun = _ekap_tarih(timezone.localdate(simdi))
    yeni, istek, toplam = [], 0, 0
    for sayfa in range(1, azami_sayfa + 1):
        satirlar = cli.dt_liste(cli.dt_liste_govdesi(
            dogrudanTeminDurumu=C.DT_DURUM_ACIK, ilanTarihiBaslangic=gun,
            ilanTarihiBitis=gun, orderBy=C.DT_ORDER_DTN_AZALAN,
            topRowCnt=C.DT_SAYFA, pageIndex=sayfa,
        ))
        istek += 1
        toplam += len(satirlar)
        sonuc = upsert(satirlar, simdi=simdi)
        yeni += sonuc["yeni"]
        if len(satirlar) < C.DT_SAYFA:
            break
        son_dtn = (satirlar[-1].get("dtn") or "").strip().upper()
        if son_dtn not in set(sonuc["yeni"]):
            break
    cp = _cp()
    cp.extra["son_yoklama"] = simdi.isoformat()
    cp.save(update_fields=["extra", "updated_at"])
    return {"is": "yoklama", "istek": istek, "satir": toplam, "yeni": yeni}


def tam_tarama_adimi(cli, *, simdi=None):
    """
    Tam açık taramasının **bir sayfası**. İş yoksa `None` (istek harcanmaz).

    Tur `extra["tam"] = {"bas", "sayfa"}` ile sürer; eksik sayfa (< 250) gelince tamam
    sayılır ve başlangıcı `extra["tam_baslar"]`a (son iki) yazılır — `gorulme_esigi`
    yalnızca TAMAMLANAN turlara bakar.
    """
    simdi = simdi or timezone.now()
    cp = _cp()
    tam = cp.extra.get("tam")
    if not tam:
        aralik = getattr(settings, "EKAP_MOBIL_DT_TAM_ARALIK_DK", 120)
        if not _gecti_mi(cp.extra.get("son_tam_bit"), aralik, simdi):
            return None
        tam = {"bas": simdi.isoformat(), "sayfa": 1}

    bugun = timezone.localdate(simdi)
    taban = bugun - timedelta(days=getattr(settings, "EKAP_DT_ACIK_AZAMI_GUN", 30))
    satirlar = cli.dt_liste(cli.dt_liste_govdesi(
        dogrudanTeminDurumu=C.DT_DURUM_ACIK, ilanTarihiBaslangic=_ekap_tarih(taban),
        ilanTarihiBitis=_ekap_tarih(bugun), orderBy=C.DT_ORDER_DTN_AZALAN,
        topRowCnt=C.DT_SAYFA, pageIndex=tam["sayfa"],
    ))
    sonuc = upsert(satirlar, simdi=simdi)
    out = {"is": "tam", "istek": 1, "sayfa": tam["sayfa"], "satir": len(satirlar),
           "yeni": sonuc["yeni"]}

    cp = _cp()  # upsert sırasında başka yazım olmuşsa ezmeyelim
    if len(satirlar) < C.DT_SAYFA:
        baslar = (list(cp.extra.get("tam_baslar") or []) + [tam["bas"]])[-2:]
        cp.extra["tam_baslar"] = baslar
        cp.extra["son_tam_bit"] = simdi.isoformat()
        cp.extra["son_tam_sayfa"] = tam["sayfa"]
        cp.extra.pop("tam", None)
        out["tamamlandi"] = True
    elif tam["sayfa"] >= _TAM_AZAMI_SAYFA:
        logger.warning("DT tam tarama %s sayfada kesildi — filtre bozulmuş olabilir",
                       tam["sayfa"])
        cp.extra["son_tam_bit"] = simdi.isoformat()
        cp.extra.pop("tam", None)
        out["kesildi"] = True
    else:
        cp.extra["tam"] = {"bas": tam["bas"], "sayfa": tam["sayfa"] + 1}
    cp.save(update_fields=["extra", "updated_at"])
    return out


def temizle(simdi=None) -> int:
    """Son teklif anı `EKAP_DT_SAKLA_GUN` önce geçmiş (ya da hiç tarihsiz eski) satırlar."""
    simdi = simdi or timezone.now()
    gun = getattr(settings, "EKAP_DT_SAKLA_GUN", 30)
    sinir = simdi - timedelta(days=gun)
    eski_ilan = timezone.localdate(simdi) - timedelta(
        days=gun + getattr(settings, "EKAP_DT_ACIK_AZAMI_GUN", 30)
    )
    n, _ = DogrudanTemin.objects.filter(
        Q(son_teklif__lt=sinir)
        | Q(son_teklif__isnull=True, ilan_tarihi__lt=eski_ilan)
    ).delete()
    return n
