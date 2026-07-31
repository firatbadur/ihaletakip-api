"""
DETSIS ağaç yardımcıları — üst→alt genişletme ve ata-yolu çözümleme.

`Authority` bir ağaç düğümüdür (`detsis_no` anahtar, `parent_detsis` üst bağ,
`idare_id` ihale filtre anahtarı). İhale filtresi `Tender.idare_id` ile çalışır;
kullanıcı ağaçta bir ÜST düğüm seçtiğinde (örn. bir bakanlık) alt birimlerin
ihaleleri de gelsin diye seçilen `detsis_no`'ları tüm alt `idare_id`'lere genişletiriz.
"""
import hashlib

from django.core.cache import cache

from .models import Authority

# Ağaç derinliği güvenlik tavanı (sonsuz döngü / bozuk veri koruması)
_MAX_DEPTH = 20

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
