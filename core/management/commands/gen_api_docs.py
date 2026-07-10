"""
API dokümanlarını üretir: OpenAPI şeması (docs/openapi.yaml) + Postman koleksiyonu
(docs/postman_collection.json).

drf-spectacular şemasından türetildiği için endpoint/serializer değiştikçe otomatik
güncel kalır. git pre-commit hook bu komutu çalıştırıp docs/ dosyalarını stage'ler.

Kullanım:
    python manage.py gen_api_docs
"""
import json
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand
from drf_spectacular.generators import SchemaGenerator

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
DOCS_DIR = Path(settings.BASE_DIR) / "docs"


class Command(BaseCommand):
    help = "OpenAPI (docs/openapi.yaml) + Postman (docs/postman_collection.json) üretir."

    def handle(self, *args, **options):
        DOCS_DIR.mkdir(exist_ok=True)
        schema = SchemaGenerator().get_schema(request=None, public=True)

        # 1) OpenAPI YAML
        openapi_path = DOCS_DIR / "openapi.yaml"
        openapi_path.write_text(
            yaml.safe_dump(_plain(schema), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        # 2) Postman koleksiyonu
        collection = build_postman(_plain(schema))
        postman_path = DOCS_DIR / "postman_collection.json"
        postman_path.write_text(
            json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        n_paths = len(schema.get("paths", {}))
        self.stdout.write(self.style.SUCCESS(
            f"✅ API dokümanları güncellendi ({n_paths} yol): {openapi_path.name}, {postman_path.name}"
        ))


# ── Yardımcılar ────────────────────────────────────────
def _plain(obj):
    """OrderedDict/özel tipleri düz dict/list'e çevirir (yaml/json güvenli)."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _resolve_ref(ref, components):
    """'#/components/schemas/Foo' → şema dict."""
    name = ref.split("/")[-1]
    return components.get(name, {})


def _example_from_schema(schema, components, depth=0):
    """Şemadan örnek gövde üretir (Postman'da doldurmak için)."""
    if depth > 6 or not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        return _example_from_schema(_resolve_ref(schema["$ref"], components), components, depth + 1)
    if "example" in schema:
        return schema["example"]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        return {
            k: _example_from_schema(v, components, depth + 1)
            for k, v in (schema.get("properties") or {}).items()
        }
    if t == "array":
        return [_example_from_schema(schema.get("items", {}), components, depth + 1)]
    return {"string": "", "integer": 0, "number": 0, "boolean": False}.get(t, "")


def _postman_path(path):
    """'/api/v1/ekap/tenders/{key}/' → (segment listesi :key formatında)."""
    segs = []
    for seg in path.strip("/").split("/"):
        segs.append(":" + seg[1:-1] if seg.startswith("{") and seg.endswith("}") else seg)
    return segs


LOGIN_TEST = (
    "// Giriş yanıtındaki access token'ı koleksiyon değişkenine kaydet\n"
    "try {\n"
    "  const d = pm.response.json();\n"
    "  const t = (d.data && d.data.access) || d.access;\n"
    "  if (t) { pm.collectionVariables.set('access_token', t); }\n"
    "} catch (e) {}"
)


def build_postman(schema):
    info = schema.get("info", {})
    components = (schema.get("components") or {}).get("schemas", {})

    collection = {
        "info": {
            "name": info.get("title", "IhaleTakip API"),
            "description": info.get("description", ""),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}],
        },
        "variable": [
            {"key": "base_url", "value": "http://localhost:8000", "type": "string"},
            {"key": "access_token", "value": "", "type": "string"},
        ],
        "item": [],
    }

    groups = {}
    for path, methods in schema.get("paths", {}).items():
        for method, op in methods.items():
            if method.lower() not in HTTP_METHODS:
                continue
            tag = _group_for(path)
            groups.setdefault(tag, []).append(_build_item(path, method, op, components))

    for tag in sorted(groups):
        collection["item"].append({"name": tag, "item": groups[tag]})
    return collection


def _group_for(path):
    """Yolu uygulamaya göre klasöre ata: /api/v1/<grup>/... → <grup>."""
    segs = path.strip("/").split("/")
    if segs[:2] == ["api", "v1"] and len(segs) > 2:
        return segs[2]  # auth, ekap, ai, favorites, saved-filters, ...
    return segs[0] if segs else "genel"


def _build_item(path, method, op, components):
    request = {
        "method": method.upper(),
        "header": [{"key": "Content-Type", "value": "application/json"}],
        "url": {
            "raw": "{{base_url}}" + path,
            "host": ["{{base_url}}"],
            "path": _postman_path(path),
        },
    }

    # İstek gövdesi örneği
    rb = op.get("requestBody")
    if rb:
        content = (rb.get("content") or {}).get("application/json", {})
        body_schema = content.get("schema", {})
        example = _example_from_schema(body_schema, components)
        if example is not None:
            request["body"] = {
                "mode": "raw",
                "raw": json.dumps(example, ensure_ascii=False, indent=2),
                "options": {"raw": {"language": "json"}},
            }

    item = {
        "name": op.get("summary") or op.get("operationId") or f"{method.upper()} {path}",
        "request": request,
    }

    # Login → token'ı otomatik kaydet
    if path.rstrip("/").endswith("/auth/login") or path.rstrip("/").endswith("/auth/social/google") \
            or path.rstrip("/").endswith("/auth/social/apple") or path.rstrip("/").endswith("/auth/register"):
        item["event"] = [{
            "listen": "test",
            "script": {"type": "text/javascript", "exec": LOGIN_TEST.split("\n")},
        }]

    return item
