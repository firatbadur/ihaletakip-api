"""
DETSIS ağaç yardımcıları — üst→alt genişletme ve ata-yolu çözümleme.

`Authority` bir ağaç düğümüdür (`detsis_no` anahtar, `parent_detsis` üst bağ,
`idare_id` ihale filtre anahtarı). İhale filtresi `Tender.idare_id` ile çalışır;
kullanıcı ağaçta bir ÜST düğüm seçtiğinde (örn. bir bakanlık) alt birimlerin
ihaleleri de gelsin diye seçilen `detsis_no`'ları tüm alt `idare_id`'lere genişletiriz.
"""
import hashlib

from django.core.cache import cache

from .models import Authority, Tender

# Ağaç derinliği güvenlik tavanı (sonsuz döngü / bozuk veri koruması)
_MAX_DEPTH = 20

# ── İhalede geçen idare_id kümesi ──────────────────────────────────────────────
_IDARE_SET_KEY = "tender_idare_id_set"
_IDARE_SET_BACKUP_KEY = "tender_idare_id_set:backup"
_IDARE_SET_LOCK_KEY = "tender_idare_id_set:lock"
_IDARE_SET_TTL = 1800  # 30 dk — beat görevi 10 dk'da bir tazelediği için pratikte dolmaz
_IDARE_SET_BACKUP_TTL = 86400  # 24 sa — beat durursa bile istek yolu bayat kümeyle döner


def _compute_tender_idare_id_set():
    """
    `DISTINCT idare_id` — **pahalı** (500k satır, ölçüm: ~40 sn soğuk).

    ⚠️ **`.order_by()` ŞART — kaldırmayın.** `Tender.Meta.ordering = ["-ihale_tarihi"]`
    explicit sıralama yokken uygulanır ve Django `distinct()` ile birlikte ORDER BY
    kolonunu SELECT listesine eklemek zorunda kalır:
        SELECT DISTINCT idare_id, ihale_tarihi FROM ekap_tender ... ORDER BY ihale_tarihi DESC
    Yani DISTINCT `idare_id` üzerinde değil **(idare_id, ihale_tarihi) çifti** üzerinde
    olurdu → birkaç bin satır yerine tablonun TAMAMI sıralanıp Python'a taşınır ve orada
    `set()` ile tekilleştirilirdi.
    """
    return set(
        Tender.objects.exclude(idare_id="")
        .order_by()  # ⚠️ bkz. docstring — kaldırmayın
        .values_list("idare_id", flat=True)
        .distinct()
    )


def refresh_tender_idare_id_set():
    """Kümeyi yeniden hesaplayıp cache'e yazar — `ekap.tasks.refresh_idare_id_set` çağırır."""
    ids = _compute_tender_idare_id_set()
    cache.set(_IDARE_SET_KEY, ids, _IDARE_SET_TTL)
    cache.set(_IDARE_SET_BACKUP_KEY, ids, _IDARE_SET_BACKUP_TTL)
    return ids


def tender_idare_id_set():
    """
    İhalede **gerçekten geçen** benzersiz `idare_id` kümesi (cache'li).
    İdare ağaç seçimi büyük bakanlıklarda on binlerce alt birime açılabildiğinden
    (okullar vb.) genişletilen küme bununla kesiştirilir → küçük, hızlı IN listesi.

    ⚠️ **Hesabı normalde beat görevi yapar** (`refresh_idare_id_set`, 10 dk). Sorgu
    500k satırda ~40 sn sürüyor; TTL dolduğunda ilk kullanıcının bunu ödemesi kabul
    edilemezdi (ölçüldü: `?idare_detsis=` ucu soğukta 40 sn). Beat 10 dk'da bir
    tazelediği için 30 dk'lık TTL pratikte hiç dolmaz.

    İstek yolundaki hesap yalnızca **son çare**dir (Redis boş / beat hiç çalışmamış):
    kilidi alan tek istek hesaplar, diğerleri 24 saatlik **yedek** kopyayı döner. Bayat
    yedek zararsızdır — bu bir kesişim kümesidir, gecikme yalnızca yepyeni bir idarenin
    filtrede geç görünmesi demektir.
    """
    ids = cache.get(_IDARE_SET_KEY)
    if ids is not None:
        return ids

    if not cache.add(_IDARE_SET_LOCK_KEY, "1", 120):
        backup = cache.get(_IDARE_SET_BACKUP_KEY)
        if backup is not None:
            return backup
        # Yedek de yoksa (ilk ısınma) hesaplamaktan başka çare yok.

    try:
        return refresh_tender_idare_id_set()
    finally:
        cache.delete(_IDARE_SET_LOCK_KEY)

# Ağaç `sync_authorities` ile **haftalık** güncellenir → uzun TTL güvenli.
_DESC_TTL = 3600


def descendant_idare_ids(detsis_nos):
    """
    Seçilen `detsis_no` düğümlerini (dahil) tüm alt düğümlerin `idare_id`'lerine
    genişletir. Dönüş: boş olmayan `idare_id` string'lerinden bir `set`.

    BFS ile `parent_detsis` üzerinden aşağı iner; her seviye tek sorgu. Dal/gruplama
    düğümlerinin `idare_id`'i boştur (atlanır) ama çocukları taranmaya devam eder.

    **Cache'li (1 sa)**: bir bakanlık seçildiğinde BFS 20 seviyeye kadar inip her
    seviyede binlerce `detsis_no` içeren IN sorgusu atıyor — arama ucunda her istekte
    tekrarlanamayacak kadar pahalı. Ağaç haftalık senkronlandığı için 1 saatlik bayatlık
    pratikte görünmez.
    """
    detsis_nos = [str(x).strip() for x in detsis_nos if str(x).strip()]
    if not detsis_nos:
        return set()

    key = "detsis_desc:" + hashlib.sha1(",".join(sorted(set(detsis_nos))).encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached

    idare_ids = set()
    # 1) Seçilen düğümlerin kendi idare_id'leri
    # ⚠️ `.order_by()`: `Authority.Meta.ordering = ["ad"]` aksi halde her BFS seviyesinde
    # gereksiz bir ORDER BY sort'u ekler (sonucu `set`e attığımız için tamamen boşa iş).
    for did in (
        Authority.objects.filter(detsis_no__in=detsis_nos)
        .order_by()
        .values_list("idare_id", flat=True)
    ):
        if did:
            idare_ids.add(did)

    # 2) Alt düğümleri BFS ile tara
    frontier = set(detsis_nos)
    seen = set(frontier)
    depth = 0
    while frontier and depth < _MAX_DEPTH:
        rows = (
            Authority.objects.filter(parent_detsis__in=frontier)
            .order_by()
            .values_list("detsis_no", "idare_id")
        )
        next_frontier = set()
        for child_detsis, child_idare in rows:
            if child_idare:
                idare_ids.add(child_idare)
            if child_detsis and child_detsis not in seen:
                seen.add(child_detsis)
                next_frontier.add(child_detsis)
        frontier = next_frontier
        depth += 1

    cache.set(key, idare_ids, _DESC_TTL)
    return idare_ids


def ancestor_path(detsis_no, name_cache=None):
    """
    Bir düğümün ata adlarını kök→ebeveyn sırasıyla döner (düğümün KENDİsi hariç).
    Örn. "BARAJLAR VE HİDROELEKTRİK... DAİRESİ" için
    ["TARIM VE ORMAN BAKANLIĞI", "DEVLET SU İŞLERİ...", "GENEL MÜDÜR YARDIMCILIĞI 2"].

    `name_cache` (detsis_no→(ad, parent_detsis)) birden çok düğüm için tekrar sorguyu
    azaltmak amacıyla dışarıdan verilebilir (bkz. `annotate_paths`).
    """
    if name_cache is None:
        name_cache = {}
    path = []
    cur = Authority.objects.filter(detsis_no=str(detsis_no)).values_list("parent_detsis", flat=True).first()
    depth = 0
    while cur and depth < _MAX_DEPTH:
        if cur in name_cache:
            ad, parent = name_cache[cur]
        else:
            row = Authority.objects.filter(detsis_no=cur).values_list("ad", "parent_detsis").first()
            if not row:
                break
            ad, parent = row
            name_cache[cur] = (ad, parent)
        path.append(ad)
        cur = parent
        depth += 1
    path.reverse()
    return path


def annotate_paths(nodes):
    """
    Bir düğüm listesi (Authority) için her birine kök→ebeveyn ata adlarını çözer.
    Dönüş: `{detsis_no: [ata_adlari...]}`. Ortak `name_cache` ile sorgu tasarrufu.
    """
    cache = {}
    return {n.detsis_no: ancestor_path(n.detsis_no, cache) for n in nodes}
