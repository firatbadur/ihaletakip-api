"""
Doğrudan temin (DT) sorgu katmanı — liste ucu ile bildirim görevinin ORTAK filtresi.

⚠️⚠️ Uç (`ekap/views_dt.py`) ve DT alarm görevi (`tenders.tasks.check_dt_alarms`) bu
modülü PAYLAŞIR. Filtre iki yerde yazılırsa bildirimdeki sayı ile mobilin açtığı liste
zamanla ayrışır — bu kod tabanında üç kez yaşanan arıza sınıfı.

Parametre adları `DogrudanTemin` alan adlarıdır; liste alanları hem virgüllü dizge
(query param) hem gerçek liste (`DtKayitliFiltre.filters` JSON) kabul eder.
"""
from datetime import datetime, time

from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .detsis_tree import descendant_idare_ids
from .mobil import dt as dt_mod
from .models import DogrudanTemin
from .utils import normalize_tr

# Filtre anahtarları — kayıtlı filtre doğrulaması ve OpenAPI belgesi bunu kullanır.
# ⚠️ `dtn` bilinçli olarak YOK: o bir bildirim derin bağlantısıdır, kayıtlı filtre değil.
FILTRE_ANAHTARLARI = ("q", "il_id", "tur", "madde_kod", "idare_detsis",
                      "son_teklif_min", "son_teklif_max", "dokuman_var")

SIRALAMALAR = ("son_teklif", "yeni")


def _liste(deger):
    if deger is None:
        return []
    if isinstance(deger, (list, tuple)):
        parcalar = deger
    else:
        parcalar = str(deger).split(",")
    return [str(p).strip() for p in parcalar if str(p).strip()]


def _int_liste(deger):
    out = []
    for p in _liste(deger):
        try:
            out.append(int(p))
        except ValueError:
            continue
    return out


def _bool(deger):
    if isinstance(deger, bool):
        return deger
    s = str(deger or "").strip().lower()
    if s in ("1", "true", "evet", "yes"):
        return True
    if s in ("0", "false", "hayir", "hayır", "no"):
        return False
    return None


def _an(deger, *, gun_sonu=False):
    """ISO damga ya da `YYYY-MM-DD` → aware datetime (gün verilirse başı/sonu)."""
    if not deger:
        return None
    s = str(deger).strip()
    dt = parse_datetime(s)
    if dt is None:
        d = parse_date(s)
        if d is None:
            return None
        dt = datetime.combine(d, time.max if gun_sonu else time.min)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def acik_qs(simdi=None):
    """Teklif verilebilir DT'ler — tanımın tek kaynağı `dt_mod.acik_q`."""
    return DogrudanTemin.objects.filter(dt_mod.acik_q(simdi))


def filtrele(qs, params):
    """
    `params` `.get()` destekleyen herhangi bir eşleme (QueryDict ya da dict).

    ⚠️ `idare_detsis` çözülemezse `qs.none()` — yanlışlıkla TÜM kayıtları döndürmemek
    için (`ekap.views._detsis_daralt` ile aynı ilke). Genişletilen küme DT tablosunun
    **kendi** idare kümesiyle kesişir: `tender_idare_id_set` burada YANLIŞ olurdu (DT
    açan bir idarenin ihale tablosunda hiç ihalesi olmayabilir).
    """
    q = (params.get("q") or "").strip() if hasattr(params, "get") else ""
    if q:
        n = normalize_tr(q)
        qs = qs.filter(Q(ad_norm__contains=n) | Q(idare_adi_norm__contains=n))

    il = _int_liste(params.get("il_id"))
    if il:
        qs = qs.filter(il_id__in=il)
    tur = _int_liste(params.get("tur"))
    if tur:
        qs = qs.filter(tur__in=tur)
    madde = _int_liste(params.get("madde_kod"))
    if madde:
        qs = qs.filter(madde_kod__in=madde)

    detsis = _liste(params.get("idare_detsis"))
    if detsis:
        genis = descendant_idare_ids(detsis)
        if genis:
            mevcut = set(
                DogrudanTemin.objects.exclude(idare_id="").order_by()
                .values_list("idare_id", flat=True).distinct()
            )
            genis &= mevcut
        qs = qs.filter(idare_id__in=genis) if genis else qs.none()

    alt = _an(params.get("son_teklif_min"))
    if alt:
        qs = qs.filter(son_teklif__gte=alt)
    ust = _an(params.get("son_teklif_max"), gun_sonu=True)
    if ust:
        qs = qs.filter(son_teklif__lte=ust)

    dok = _bool(params.get("dokuman_var"))
    if dok is not None:
        qs = qs.filter(dokuman_var=dok)

    dtnler = [d.upper() for d in _liste(params.get("dtn"))]
    if dtnler:
        qs = qs.filter(dtn__in=dtnler)
    return qs


def sirala(qs, order):
    """
    `son_teklif` (vars.): en acil önce, tarihsizler sonda. `yeni`: en son yayımlanan önce.
    ⚠️ Tie-break `pk` şart: aynı son teklif anını onlarca kayıt paylaşıyor (ölçüldü:
    "30.10.2026 11:30" iki ayrı okulun mazot alımı) ve eşitlerin sırası plana kalırsa
    sayfalama kayıt tekrarlar/kaybeder (bkz. CLAUDE.md "Liste sıralaması").
    """
    if order == "yeni":
        return qs.order_by(F("ilan_tarihi").desc(nulls_last=True), "-created_at", "-pk")
    return qs.order_by(F("son_teklif").asc(nulls_last=True), "pk")
