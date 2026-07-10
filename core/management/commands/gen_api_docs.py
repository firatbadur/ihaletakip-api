"""
API dokümanlarını üretir: OpenAPI şeması (docs/openapi.yaml) + Postman koleksiyonu
(docs/postman_collection.json).

drf-spectacular şemasından türetildiği için endpoint/serializer değiştikçe otomatik
güncel kalır. git pre-commit hook bu komutu çalıştırıp docs/ dosyalarını stage'ler.

Postman tarafında hedef: **her istek elle düzenlenmeden çalıştırılabilsin**. Bunun için
şemadaki `OpenApiExample` değerleri gövdeye/parametreye taşınır; örnek yoksa alan adı ve
tipinden gerçekçi bir test değeri türetilir (bkz. `_sample_value`).

Kullanım:
    python manage.py gen_api_docs
"""
import json
import re
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand
from drf_spectacular.generators import SchemaGenerator

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
DOCS_DIR = Path(settings.BASE_DIR) / "docs"

# Klasör sırası: kimlik akışı önce gelsin ki koleksiyon yukarıdan aşağıya çalıştırılabilsin.
FOLDER_ORDER = [
    "health", "auth", "ekap", "ai",
    "favorites", "saved-tenders", "saved-filters", "alarms",
    "notifications", "support", "oturum-sonu",
]

# Oturumu bitiren uçlar kendi klasöründe en sona alınır; aksi halde koleksiyonu baştan
# sona çalıştıran biri `auth` klasörünün sonunda token'ını kaybeder ve kalan tüm
# istekler 401 döner.
TEARDOWN_FOLDER = "oturum-sonu"
TEARDOWN_PATHS = {
    ("post", "/api/v1/auth/logout/"),
    ("post", "/api/v1/auth/deactivate/"),
}

# Klasör içi istek sırası. Koleksiyon yukarıdan aşağıya çalıştırılabilmeli:
# önce token üreten uçlar, en sonda token'ı geçersizleştiren / yıkıcı olanlar.
# Burada olmayan uçlar `_generic_rank` ile sıralanır.
ITEM_ORDER = {
    "auth": [
        ("post", "/api/v1/auth/register/"),
        ("post", "/api/v1/auth/login/"),
        ("post", "/api/v1/auth/social/google/"),
        ("post", "/api/v1/auth/social/apple/"),
        ("post", "/api/v1/auth/token/refresh/"),
        ("get", "/api/v1/auth/profile/"),
        ("patch", "/api/v1/auth/profile/"),
        ("patch", "/api/v1/auth/preferences/"),
        ("post", "/api/v1/auth/fcm-token/"),
    ],
    TEARDOWN_FOLDER: [
        ("post", "/api/v1/auth/logout/"),      # refresh token'ı kara listeye alır
        ("post", "/api/v1/auth/deactivate/"),  # hesabı kapatır → en sonda
    ],
    "ekap": [
        ("get", "/api/v1/ekap/tenders/"),
        ("get", "/api/v1/ekap/tenders/{key}/"),
        ("get", "/api/v1/ekap/tenders/{key}/announcements/"),
        ("get", "/api/v1/ekap/tenders/{ekap_id}/document-url/"),
        ("get", "/api/v1/ekap/cities/"),
        ("get", "/api/v1/ekap/okas/search/"),
        ("get", "/api/v1/ekap/authorities/search/"),
    ],
    "ai": [
        ("post", "/api/v1/ai/analyze/"),
        ("get", "/api/v1/ai/tasks/{task_id}/"),
        ("post", "/api/v1/ai/tts/"),
    ],
    "notifications": [
        ("get", "/api/v1/notifications/"),
        ("get", "/api/v1/notifications/unread-count/"),
        ("post", "/api/v1/notifications/{notification_id}/read/"),
        ("post", "/api/v1/notifications/read-all/"),
    ],
}

# Açık sıra verilmeyen klasörler: liste → oluştur → getir → değiştir → sil
_METHOD_RANK = {"get": 0, "post": 1, "put": 2, "patch": 3, "delete": 4}

FOLDER_DESCRIPTIONS = {
    "health": "Kimlik doğrulaması istemeyen sağlık kontrolü.",
    "auth": (
        "Kayıt, giriş, sosyal giriş, token yenileme ve çıkış.\n\n"
        "**Önce `Giriş yap` isteğini çalıştırın** — `access_token` ve `refresh_token` "
        "koleksiyon değişkenleri otomatik dolar, diğer tüm klasörler bunları kullanır."
    ),
    "ekap": (
        "İhale arama, detay, ilanlar, OKAS/idare/il referans verileri.\n\n"
        "Veriler kendi veritabanımızdan servis edilir; EKAP'a yalnızca detay ve belge "
        "bağlantısı için canlı gidilir."
    ),
    "ai": "Claude ile doküman analizi (asenkron, Celery) ve Google TTS seslendirme.",
    "favorites": "Kullanıcının favori ihaleleri.",
    "saved-tenders": "Kullanıcının kaydettiği ihaleler (İKN ile anahtarlanır).",
    "saved-filters": "Kaydedilmiş arama filtreleri; `alarm=true` ise eşleşmelerde bildirim üretir.",
    "alarms": "İhale alarmları — ihale günü ve doküman değişikliği bildirimleri.",
    "notifications": "Alarm ve filtre eşleşmelerinden üretilen bildirimler.",
    "support": "Destek talepleri.",
    "oturum-sonu": (
        "Oturumu sonlandıran uçlar — **kasıtlı olarak en sonda**.\n\n"
        "`Çıkış yap` refresh token'ı kara listeye alır, `Hesabı devre dışı bırak` ise "
        "hesabı kapatır ve sonraki tüm istekler `401` döner. Koleksiyonu baştan sona "
        "çalıştırıyorsanız bu klasör en son çalışmalıdır."
    ),
}

COLLECTION_DESCRIPTION = """\
IhaleTakip API — kamu ihalesi takip uygulamasının backend servisi.

Bu koleksiyon `python manage.py gen_api_docs` ile **otomatik üretilir**; elle düzenlemeyin,
değişiklikler bir sonraki üretimde kaybolur. Kaynak: view'lardaki `@extend_schema`.

## Hızlı başlangıç

1. `base_url` değişkenini ortamınıza göre ayarlayın (varsayılan `http://localhost:8000`).
2. **auth → Giriş yap** isteğini çalıştırın. Başarılı yanıt `access_token` ve
   `refresh_token` koleksiyon değişkenlerini otomatik doldurur.
3. Diğer istekler `access_token`'ı Bearer header'ı olarak kalıtım yoluyla kullanır —
   elle kopyalamanıza gerek yok.
4. `access` token'ın ömrü 1 gündür. Dolduğunda **auth → Access token yenile** isteğini
   çalıştırın; her iki değişken de tazelenir.

Klasörler bu akışa göre sıralıdır, koleksiyonu Runner ile baştan sona çalıştırabilirsiniz.
Oturumu bitiren `Çıkış yap` ve `Hesabı devre dışı bırak` uçları bilerek en sondaki
**oturum-sonu** klasörüne alınmıştır.

## Yanıt zarfı

**Tüm** yanıtlar aynı zarfta döner — asıl veri her zaman `data` içindedir:

```json
{ "success": true, "message": "", "data": { } }
```

Hata durumunda `success: false`, `data: null` olur ve `message` açıklamayı, `errors` ise
(varsa) alan bazlı doğrulama hatalarını taşır. Beklenmeyen sunucu hataları dahil her yanıt
JSON'dur; ham HTML 500 dönmez.

## Değişkenler

| Değişken | Açıklama |
|---|---|
| `base_url` | API kök adresi, sonunda `/` olmadan |
| `access_token` | Giriş/yenileme sonrası otomatik dolar |
| `refresh_token` | Giriş/yenileme sonrası otomatik dolar; çıkış ve yenileme kullanır |

## Test verileri

İstek gövdeleri ve query parametreleri örnek değerlerle doldurulmuştur. Bunlar
**örnektir** — `ekap_id`, `İKN`, `task_id` gibi değerleri kendi veritabanınızdaki gerçek
kayıtlarla değiştirin. İsteğe bağlı query parametreleri varsayılan olarak **kapalı**
gelir; kullanmak için Postman'de yanındaki kutuyu işaretleyin.
"""

# Giriş ve token yenileme yanıtından token'ları koleksiyon değişkenine yazar.
TOKEN_CAPTURE_TEST = """\
// access + refresh token'ları koleksiyon değişkenlerine kaydet.
// Global zarf nedeniyle token'lar `data` içindedir; zarfsız yanıta da tolerans var.
try {
  const body = pm.response.json();
  const d = (body && body.data) || body || {};
  if (d.access) { pm.collectionVariables.set('access_token', d.access); }
  if (d.refresh) { pm.collectionVariables.set('refresh_token', d.refresh); }
  pm.test('token alindi', function () { pm.expect(d.access).to.be.a('string'); });
} catch (e) {
  console.log('Token yakalanamadi:', e);
}"""

TOKEN_CAPTURE_PATHS = {
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/social/google",
    "/api/v1/auth/social/apple",
    "/api/v1/auth/token/refresh",
}


class Command(BaseCommand):
    help = "OpenAPI (docs/openapi.yaml) + Postman (docs/postman_collection.json) üretir."

    def handle(self, *args, **options):
        DOCS_DIR.mkdir(exist_ok=True)
        schema = _plain(SchemaGenerator().get_schema(request=None, public=True))

        openapi_path = DOCS_DIR / "openapi.yaml"
        openapi_path.write_text(
            yaml.safe_dump(schema, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        collection = build_postman(schema)
        postman_path = DOCS_DIR / "postman_collection.json"
        postman_path.write_text(
            json.dumps(collection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        n_req = sum(len(f["item"]) for f in collection["item"])
        self.stdout.write(self.style.SUCCESS(
            f"✅ API dokümanları güncellendi "
            f"({len(schema.get('paths', {}))} yol, {n_req} Postman isteği): "
            f"{openapi_path.name}, {postman_path.name}"
        ))


# ── Yardımcılar ────────────────────────────────────────
def _plain(obj):
    """OrderedDict/özel tipleri düz dict/list'e çevirir (yaml/json güvenli)."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _resolve(schema, components, _seen=None):
    """`$ref` ve `allOf` sarmalını açar."""
    _seen = _seen or set()
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if ref:
        if ref in _seen:  # kendine referans veren şema → sonsuz döngüyü kes
            return {}
        _seen = _seen | {ref}
        return _resolve(components.get(ref.split("/")[-1], {}), components, _seen)
    if "allOf" in schema:
        merged = {}
        for part in schema["allOf"]:
            merged.update(_resolve(part, components, _seen))
        merged.update({k: v for k, v in schema.items() if k != "allOf"})
        return merged
    return schema


# Alan adı → gerçekçi test değeri. Postman'deki isteklerin elle düzenlenmeden
# çalışabilmesi için; sıra önemlidir (ilk eşleşen kazanır).
_FIELD_SAMPLES = [
    (r"^password$", "Test1234!"),
    (r"^refresh$", "{{refresh_token}}"),
    (r"^access$", "{{access_token}}"),
    (r"id_token|identity_token", "eyJhbGciOiJSUzI1NiJ9...token"),
    (r"fcm_token", "fMEP0vJqR0m2Xy1s_example_device_token_abc123"),
    (r"email", "test@ihaletakip.com"),
    (r"phone", "05551234567"),
    (r"^ikn$|tender_ikn", "2025/1234567"),
    (r"tender_id|^ekap_id$", "1234567"),
    (r"task_id", "3f8c2b1a-7e4d-4a91-9c2f-8b6d5e1a0c33"),
    (r"file_name", "teknik_sartname.pdf"),
    (r"file_base64|audio_base64", "JVBERi0xLjQKJeLjz9MKMyAwIG9iago8PC9GaWx0ZXI..."),
    (r"display_name|full_name", "Test Kullanıcı"),
    (r"institution|idare", "Ankara Büyükşehir Belediyesi"),
    (r"title|ihale_adi|^name$", "Bilgisayar ve Çevre Birimi Alımı"),
    (r"city", "ANKARA"),
    (r"message", "Örnek destek mesajı."),
    (r"photo_url|^url$", "https://example.com/foto.jpg"),
    (r"date|_at$", "23.03.2027 14:00"),
    (r"^q$|search", "bilgisayar"),
]


def _sample_value(name, schema, components, depth=0):
    """Bir şema (ve alan adı) için gerçekçi örnek değer üretir."""
    schema = _resolve(schema, components)
    if depth > 6 or not schema:
        return ""

    # Şemada açıkça verilmiş değerler her zaman kazanır
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]

    stype = schema.get("type")
    if stype == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:  # serbest JSON (JSONField)
            return {}
        return {
            k: _sample_value(k, v, components, depth + 1)
            for k, v in props.items()
            if not _resolve(v, components).get("readOnly")
        }
    if stype == "array":
        return [_sample_value(name, schema.get("items", {}), components, depth + 1)]

    lname = (name or "").lower()
    if stype in (None, "string"):
        fmt = schema.get("format")
        if fmt == "email":
            return "test@ihaletakip.com"
        if fmt == "date-time":
            return "2026-03-23T14:00:00+03:00"
        if fmt == "date":
            return "2026-03-23"
        for pattern, value in _FIELD_SAMPLES:
            if re.search(pattern, lname):
                return value
        return "ornek"
    if stype == "integer":
        return 1
    if stype == "number":
        return 1.0
    if stype == "boolean":
        return True
    return ""


def _first_example(container):
    """OpenAPI `examples` sözlüğünden ilk örneğin değerini döner."""
    examples = container.get("examples")
    if isinstance(examples, dict):
        for ex in examples.values():
            if isinstance(ex, dict) and "value" in ex:
                return ex["value"]
    if "example" in container:
        return container["example"]
    return None


def _param_value(param, components):
    """Bir query/path parametresi için örnek değer."""
    value = _first_example(param)
    if value is None:
        value = _sample_value(param.get("name", ""), param.get("schema", {}), components)
    return "" if value is None else str(value)


def _param_description(param):
    """Parametre açıklaması + zorunluluk/enum ipuçları."""
    parts = []
    if param.get("required"):
        parts.append("**Zorunlu.**")
    if param.get("description"):
        parts.append(param["description"])
    enum = (param.get("schema") or {}).get("enum")
    if enum:
        parts.append("Geçerli değerler: " + ", ".join(f"`{e}`" for e in enum) + ".")
    return " ".join(parts)


def _is_public(op):
    """
    Uç kimlik doğrulaması istemiyor mu?

    OpenAPI'de operasyon `security` taşımıyorsa kök `security`yi miras alır; bu şemada
    kök `security` yoktur, dolayısıyla anahtarın yokluğu "herkese açık" demektir.
    drf-spectacular `auth=[]` için anahtarı hiç yazmaz, korumalı uçlara ise açıkça yazar.
    """
    return not op.get("security")


def _postman_path(path):
    """
    '/api/v1/ekap/tenders/{key}/' → ['api','v1','ekap','tenders',':key','']

    Postman isteği `raw` metninden değil bu segment dizisinden kurar; sondaki `/`
    yalnızca **boş bir son segment** ile temsil edilir. Eksikse Postman
    `/api/v1/auth/login` gönderir, Django `APPEND_SLASH` ile 301 döner ve Postman
    yönlendirmeyi izlerken POST'u GET'e çevirir → "GET metoduna izin verilmiyor" (405).
    """
    segs = []
    for seg in path.strip("/").split("/"):
        segs.append(":" + seg[1:-1] if seg.startswith("{") and seg.endswith("}") else seg)
    if path.endswith("/"):
        segs.append("")
    return segs


def _raw_url(path, queries):
    """`raw` alanı `path` dizisiyle birebir tutarlı olmalı ({key} yerine :key)."""
    raw = "{{base_url}}/" + "/".join(_postman_path(path))
    enabled = [q for q in queries if not q.get("disabled")]
    if enabled:
        raw += "?" + "&".join(f"{q['key']}={q['value']}" for q in enabled)
    return raw


def _request_body(op, components):
    """İstek gövdesi — önce şemadaki örnek, yoksa alanlardan türetilmiş test verisi."""
    rb = op.get("requestBody")
    if not rb:
        return None
    content = rb.get("content") or {}
    media = content.get("application/json") or (
        next(iter(content.values())) if content else {}
    )
    if not media:
        return None

    example = _first_example(media)
    if example is None:
        example = _sample_value("", media.get("schema", {}), components)
    if example in (None, "", {}):
        return None
    return {
        "mode": "raw",
        "raw": json.dumps(example, ensure_ascii=False, indent=2),
        "options": {"raw": {"language": "json"}},
    }


def _describe(op, extra_examples):
    """Postman isteği açıklaması: şema açıklaması + alternatif gövde örnekleri."""
    parts = []
    if op.get("description"):
        parts.append(op["description"].strip())

    if extra_examples:
        parts.append("---\n\n### Alternatif gövde örnekleri")
        for name, ex in extra_examples:
            block = json.dumps(ex["value"], ensure_ascii=False, indent=2)
            desc = ex.get("description")
            head = f"**{name}**" + (f" — {desc}" if desc else "")
            parts.append(f"{head}\n\n```json\n{block}\n```")

    parts.append(
        "_Herkese açık — kimlik doğrulaması gerekmez._"
        if _is_public(op)
        else "_Kimlik doğrulaması gerekir: `Authorization: Bearer {{access_token}}`._"
    )
    return "\n\n".join(parts)


def _item_rank(folder, method, path):
    """Klasör içi sıra: açık listede varsa oradaki index, yoksa REST akışına göre."""
    explicit = ITEM_ORDER.get(folder, [])
    key = (method.lower(), path)
    if key in explicit:
        return (0, explicit.index(key), "")
    # Koleksiyon uçları (path parametresiz) detay uçlarından önce gelsin.
    has_path_param = "{" in path
    return (1, (has_path_param, _METHOD_RANK.get(method.lower(), 9)), path)


def build_postman(schema):
    info = schema.get("info", {})
    components = (schema.get("components") or {}).get("schemas", {})

    groups = {}
    for path, methods in schema.get("paths", {}).items():
        for method, op in methods.items():
            if method.lower() not in HTTP_METHODS:
                continue
            folder = _folder_for(method, path)
            groups.setdefault(folder, []).append(
                (_item_rank(folder, method, path), _build_item(path, method, op, components))
            )

    items = []
    for tag in sorted(groups, key=lambda t: (FOLDER_ORDER.index(t) if t in FOLDER_ORDER else 99, t)):
        folder = {"name": tag, "item": [it for _, it in sorted(groups[tag], key=lambda p: p[0])]}
        if tag in FOLDER_DESCRIPTIONS:
            folder["description"] = FOLDER_DESCRIPTIONS[tag]
        items.append(folder)

    return {
        "info": {
            "name": info.get("title", "IhaleTakip API"),
            "description": COLLECTION_DESCRIPTION,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}],
        },
        "variable": [
            {"key": "base_url", "value": "http://localhost:8000", "type": "string"},
            {"key": "access_token", "value": "", "type": "string"},
            {"key": "refresh_token", "value": "", "type": "string"},
        ],
        "item": items,
    }


def _folder_for(method, path):
    """Yolu uygulamaya göre klasöre ata: /api/v1/<grup>/... → <grup>."""
    if (method.lower(), path) in TEARDOWN_PATHS:
        return TEARDOWN_FOLDER
    segs = path.strip("/").split("/")
    if segs[:2] == ["api", "v1"] and len(segs) > 2:
        return segs[2]
    return segs[0] if segs else "genel"


def _build_item(path, method, op, components):
    params = op.get("parameters") or []

    # Query parametreleri — isteğe bağlı olanlar kapalı gelir ki istek olduğu gibi çalışsın.
    queries = [
        {
            "key": p["name"],
            "value": _param_value(p, components),
            "description": _param_description(p),
            "disabled": not p.get("required", False),
        }
        for p in params
        if p.get("in") == "query"
    ]

    # Path değişkenleri — Postman `:name` yer tutucularını buradan doldurur.
    variables = [
        {
            "key": p["name"],
            "value": _param_value(p, components),
            "description": _param_description(p),
        }
        for p in params
        if p.get("in") == "path"
    ]

    url = {
        "raw": _raw_url(path, queries),
        "host": ["{{base_url}}"],
        "path": _postman_path(path),
    }
    if queries:
        url["query"] = queries
    if variables:
        url["variable"] = variables

    body = _request_body(op, components)
    headers = [{"key": "Accept", "value": "application/json"}]
    if body:
        headers.append({"key": "Content-Type", "value": "application/json"})

    # Şemadaki ikinci ve sonraki örnekler açıklamaya yazılır (Postman tek gövde tutar).
    media = ((op.get("requestBody") or {}).get("content") or {}).get("application/json", {})
    all_examples = list((media.get("examples") or {}).items())
    extra_examples = all_examples[1:]

    request = {
        "method": method.upper(),
        "header": headers,
        "url": url,
        "description": _describe(op, extra_examples),
    }
    if body:
        request["body"] = body

    # Açık uçlar (login, register, social, refresh, health) Bearer header'ı göndermemeli.
    if _is_public(op):
        request["auth"] = {"type": "noauth"}

    item = {
        "name": op.get("summary") or op.get("operationId") or f"{method.upper()} {path}",
        "request": request,
    }

    if path.rstrip("/") in TOKEN_CAPTURE_PATHS and method.lower() == "post":
        item["event"] = [{
            "listen": "test",
            "script": {"type": "text/javascript", "exec": TOKEN_CAPTURE_TEST.split("\n")},
        }]

    return item
