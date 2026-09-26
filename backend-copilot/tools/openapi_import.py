"""OpenAPI/Swagger 导入：把后端的 swagger.json / openapi.json 转成接口目录草稿。

设计要点（对应 M2 方案）：
- 用户给一个 OpenAPI 文档（URL 或 JSON/YAML 字符串），本模块解析并映射为
  Catalog 草稿（Pydantic 模型），**不落盘**——先产出"导入报告 + 草稿 YAML"
  供用户确认（补 write 标注 / 中文描述），确认后即得可用目录。
- write 判定保守：默认全部只读；POST/PUT/DELETE/PATCH 标记为"疑似写接口"
  进入报告待确认清单，由人工显式放行（不自动写 write:true）。
- 分页 / result_path 用启发式识别（见 _detect_pagination / _detect_result_path）。
- $ref 解析一层（components/schemas 内引用），不支持循环引用的深层展开。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import yaml

from tools.catalog import (
    ApiConfig,
    AuthConfig,
    Catalog,
    CatalogError,
    DefaultsConfig,
    PaginationConfig,
    ParamConfig,
)

WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
_WRITE_PARAM_HINTS = re.compile(r"create|add|save|update|delete|remove|edit|submit|approve|reject", re.I)
_PAGE_HINTS = {"page", "current", "pagenum", "page_num", "page_no", "pageindex", "page_index"}
_SIZE_HINTS = {"size", "pagesize", "page_size", "limit", "per_page", "perpage", "rows_per_page"}
_ENVELOPE_KEYS = ("data", "result", "rows", "records", "list", "content")


# ---------------- 文档加载 ----------------

def load_openapi_doc(source: str) -> dict[str, Any]:
    """从 URL 或 JSON/YAML 字符串加载 OpenAPI 文档，返回 dict。

    Swagger 2.0 与 OpenAPI 3.x 均可（两者的 paths/parameters 语义基本一致，
    requestBody 仅 3.x 有——2.0 的 body 参数按 in=body 处理）。
    """
    raw: Any
    if re.match(r"^https?://", source.strip()):
        try:
            resp = httpx.get(source.strip(), timeout=15, trust_env=False,
                             follow_redirects=True)
            resp.raise_for_status()
            text = resp.text
        except httpx.HTTPError as e:
            raise CatalogError(f"无法下载 OpenAPI 文档: {e}") from e
    else:
        text = source
    try:
        raw = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise CatalogError("文档不是合法的 JSON 或 YAML") from e
    if not isinstance(raw, dict) or "paths" not in raw:
        raise CatalogError("文档缺少 paths 字段，不是 OpenAPI/Swagger 文档")
    return raw


# ---------------- $ref 解析 ----------------

def _resolve_ref(doc: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """解析 {$ref: '#/components/schemas/X'}，最多递归 depth 层。"""
    if depth > 3 or not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if not ref or not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    cur: Any = doc
    for part in ref[2:].split("/"):
        cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return node
    return _resolve_ref(doc, cur, depth + 1)


# ---------------- 名称清洗 ----------------

def _api_name(operation: dict[str, Any], method: str, path: str) -> str:
    """优先 operationId，清洗成 snake_case（含驼峰拆分）；缺失时从路径生成。"""
    raw = operation.get("operationId") or f"{method.lower()}_{path}"
    # 驼峰拆分：ticketList → ticket_list
    raw = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", raw)
    name = re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_").lower()
    name = re.sub(r"_+", "_", name)
    if not name or name[0].isdigit():
        name = f"api_{name}"
    return name


def _snake(key: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9]+", "_", str(key)).strip("_")
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key).lower() or "param"


# ---------------- 参数映射 ----------------

def _map_schema_props(doc: dict[str, Any], schema: dict[str, Any],
                      required_names: set[str]) -> dict[str, ParamConfig]:
    """把 requestBody / response schema 的 properties 摊平为参数说明。"""
    schema = _resolve_ref(doc, schema) or {}
    props = schema.get("properties") or {}
    req = set(schema.get("required") or []) | required_names
    out: dict[str, ParamConfig] = {}
    for key, p in props.items():
        p = _resolve_ref(doc, p) or {}
        ptype = p.get("type")
        tmap = {"integer": "int", "number": "float", "boolean": "bool"}
        out[_snake(key)] = ParamConfig(
            type=tmap.get(ptype, "string"),
            required=key in req,
            description=str(p.get("description") or p.get("title") or ""),
        )
    return out


def _collect_params(doc: dict[str, Any], operation: dict[str, Any],
                    path_item: dict[str, Any]) -> dict[str, ParamConfig]:
    """合并 path-item 级与 operation 级参数 + 3.x requestBody / 2.0 body 参数。"""
    params: dict[str, ParamConfig] = {}
    required_names: set[str] = set()
    # 1) path/query 参数（去重：operation 级覆盖 path-item 级同位置参数）
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for p in (path_item.get("parameters") or []) + (operation.get("parameters") or []):
        p = _resolve_ref(doc, p) or {}
        key = (str(p.get("in")), str(p.get("name")))
        seen[key] = p
    for (loc, name), p in seen.items():
        if loc not in ("query", "path"):
            continue
        ptype = (p.get("schema") or {}).get("type", p.get("type"))
        tmap = {"integer": "int", "number": "float", "boolean": "bool"}
        params[_snake(name)] = ParamConfig(
            type=tmap.get(ptype, "string"),
            required=bool(p.get("required")) or loc == "path",
            description=str(p.get("description") or ""),
        )
        if p.get("required") and loc != "path":
            required_names.add(_snake(name))
    # 2) 3.x requestBody
    body = operation.get("requestBody")
    if isinstance(body, dict):
        content = _resolve_ref(doc, body).get("content") or {}
        json_media = content.get("application/json") or {}
        schema = json_media.get("schema")
        if schema:
            props = _map_schema_props(doc, schema, {n for n, c in params.items() if c.required})
            params.update(props)
    # 3) 2.0 body 参数
    for p in (operation.get("parameters") or []):
        p = _resolve_ref(doc, p) or {}
        if p.get("in") == "body" and p.get("schema"):
            params.update(_map_schema_props(doc, p["schema"], set()))
    return params


# ---------------- 启发式识别（任务 #11） ----------------

def _detect_pagination(params: dict[str, ParamConfig],
                       operation: dict[str, Any],
                       doc: dict[str, Any]) -> Optional[PaginationConfig]:
    """识别分页：查询参数含 page/current + size/pageSize 组合即认为分页接口。"""
    keys = set(params)
    page_p = next((k for k in keys if k in _PAGE_HINTS), None)
    size_p = next((k for k in keys if k in _SIZE_HINTS), None)
    if not (page_p and size_p):
        return None
    total_path = _detect_total_path(doc, operation)
    return PaginationConfig(page_param=page_p, size_param=size_p, total_path=total_path)


def _response_schema(doc: dict[str, Any], operation: dict[str, Any]) -> Optional[dict[str, Any]]:
    resp = (operation.get("responses") or {})
    ok = resp.get("200") or resp.get("201") or resp.get("default") or {}
    ok = _resolve_ref(doc, ok)
    if not isinstance(ok, dict):
        return None
    # OpenAPI 3.x: content.application/json.schema;Swagger 2.0: 直接 schema
    content = ok.get("content") or {}
    schema = (content.get("application/json") or {}).get("schema") or ok.get("schema")
    return _resolve_ref(doc, schema) if schema else None


def _detect_total_path(doc: dict[str, Any], operation: dict[str, Any]) -> Optional[str]:
    """从响应 schema 找 total 字段位置：顶层 total → data.total。"""
    schema = _response_schema(doc, operation)
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties") or {}
    if "total" in props:
        return "total"
    for env in ("data", "result"):
        sub = props.get(env)
        sub = _resolve_ref(doc, sub) or {}
        if isinstance(sub, dict) and "total" in (sub.get("properties") or {}):
            return f"{env}.total"
    return None


def _detect_result_path(doc: dict[str, Any], operation: dict[str, Any]) -> str:
    """推断数据所在路径：顶层含数组字段用之；否则找信封（data/result）下的数组；再退回 '.'。"""
    schema = _response_schema(doc, operation)
    if not isinstance(schema, dict):
        return "."
    props = schema.get("properties") or {}
    # 1) 顶层就是数组字段（RuoYi rows 风格）
    for key, p in props.items():
        p = _resolve_ref(doc, p) or {}
        if p.get("type") == "array":
            return key
    # 2) 信封 data/result 下的数组（BladeX data.records 风格）
    for env in ("data", "result"):
        sub = _resolve_ref(doc, props.get(env) or {}) or {}
        sub_props = sub.get("properties") or {}
        for key, p in sub_props.items():
            p = _resolve_ref(doc, p) or {}
            if p.get("type") == "array":
                return f"{env}.{key}"
        if env in props:
            sub_type = sub.get("type")
            if sub_type == "array":
                return env
            if sub_props:  # 信封下是对象（详情接口）
                return env
    # 3) 信封存在但没有数组字段——取信封本身
    for env in _ENVELOPE_KEYS:
        if env in props:
            return env
    return "."


# ---------------- write 判定（任务 #12） ----------------

def _write_verdict(method: str, operation: dict[str, Any]) -> tuple[bool, bool]:
    """返回 (write, suspect)：write 恒为 False（保守默认），suspect 标记疑似写接口。

    疑似 = 写方法 或 operationId/description 命中写动作关键词。
    """
    if method in WRITE_METHODS:
        return False, True
    text = f"{operation.get('operationId', '')} {operation.get('summary', '')} {operation.get('description', '')}"
    if _WRITE_PARAM_HINTS.search(text):
        return False, True
    return False, False


# ---------------- 主转换（任务 #13 报告在 generate_report） ----------------

def import_openapi(source: str, include: Optional[list[str]] = None,
                   exclude: Optional[list[str]] = None) -> tuple[Catalog, dict[str, Any]]:
    """从 OpenAPI 文档生成目录草稿 + 导入报告。

    include/exclude：路径正则白/黑名单（exclude 优先）。
    返回 (Catalog 草稿, report dict)。不落盘、不写任何 write:true。
    """
    doc = load_openapi_doc(source)
    base_url = _base_url_from_doc(doc)

    include_res = [re.compile(p) for p in (include or [])]
    exclude_res = [re.compile(p) for p in (exclude or [])]

    apis: list[ApiConfig] = []
    skipped: list[dict[str, str]] = []
    suspects: list[str] = []
    imported = 0
    seen_names: set[str] = set()

    for path, path_item in sorted((doc.get("paths") or {}).items()):
        if exclude_res and any(r.search(path) for r in exclude_res):
            continue
        if include_res and not any(r.search(path) for r in include_res):
            continue
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "post", "put", "delete", "patch"):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            name = _api_name(operation, method.upper(), path)
            if name in seen_names:
                skipped.append({"path": f"{method.upper()} {path}", "reason": f"重名 {name}"})
                continue
            write, suspect = _write_verdict(method.upper(), operation)
            params = _collect_params(doc, operation, path_item)
            api = ApiConfig(
                name=name,
                description=str(operation.get("summary") or operation.get("description") or ""),
                method=method.upper(),
                path=path,
                params=params,
                result_path=_detect_result_path(doc, operation),
                pagination=_detect_pagination(params, operation, doc),
                write=write,
            )
            apis.append(api)
            seen_names.add(name)
            imported += 1
            if suspect:
                suspects.append(name)

    if not apis:
        raise CatalogError("文档中没有可导入的接口（检查 include/exclude 过滤条件）")

    # 报告中列出无鉴权字段的真实凭据仍需用户手填
    base_url_hint = ""
    if re.search(r"//(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|127\.|localhost|host\.docker\.internal)", base_url):
        base_url_hint = (
            "检测到 base_url 是内网/直连地址（来自文档 servers 字段）。"
            "若插件运行在容器中或后端走网关，请把目录里的 base_url 覆盖为网关地址"
            "（如 http://host.docker.internal:8101），否则将无法连接"
        )
    report: dict[str, Any] = {
        "imported": imported,
        "skipped": skipped,
        "suspected_write": suspects,   # 待人工确认后写入 write: true
        "base_url": base_url,
        "base_url_hint": base_url_hint,
        "note": "默认全部只读；疑似写接口已列出，确认后请显式标记 write: true",
    }
    catalog = Catalog(
        version=1,
        auth=AuthConfig(),  # 凭据不在 OpenAPI 中，留给用户/凭证表单
        defaults=DefaultsConfig(),
        base_url=base_url,
        apis=apis,
    )
    return catalog, report


def _base_url_from_doc(doc: dict[str, Any]) -> str:
    servers = doc.get("servers") or []
    if servers and isinstance(servers[0], dict) and servers[0].get("url"):
        return str(servers[0]["url"]).rstrip("/")
    host = doc.get("host")
    if host:
        scheme = (doc.get("schemes") or ["http"])[0]
        base = f"{scheme}://{host}"
        if doc.get("basePath"):
            base += str(doc["basePath"]).rstrip("/")
        return base
    return "http://localhost:8080"


# ---------------- 导出为目录 YAML ----------------

def catalog_to_yaml(catalog: Catalog) -> str:
    """把目录草稿序列化回 YAML 字符串（给用户确认/编辑后粘贴使用）。"""
    data: dict[str, Any] = {
        "version": 1,
        "base_url": catalog.base_url,
        "defaults": {"timeout": catalog.defaults.timeout, "max_rows": catalog.defaults.max_rows},
    }
    auth = catalog.auth
    if auth.type != "none":
        a: dict[str, Any] = {"type": auth.type}
        if auth.token_endpoint:
            a.update(token_endpoint=auth.token_endpoint, token_path=auth.token_path,
                     token_payload=auth.token_payload)
        elif auth.token:
            a["token"] = "{{token}}"
        data["auth"] = a
    apis: list[dict[str, Any]] = []
    for a in catalog.apis:
        item: dict[str, Any] = {
            "name": a.name,
            "description": a.description,
            "method": a.method,
            "path": a.path,
        }
        if a.params:
            item["params"] = {
                k: {"type": p.type, "required": p.required, "description": p.description}
                for k, p in a.params.items()
            }
        if a.pagination:
            item["pagination"] = {
                "page_param": a.pagination.page_param,
                "size_param": a.pagination.size_param,
                "total_path": a.pagination.total_path,
            }
        if a.result_path and a.result_path != ".":
            item["result_path"] = a.result_path
        if a.write:
            item["write"] = True
        apis.append(item)
    data["apis"] = apis
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)


def generate_report_text(report: dict[str, Any]) -> str:
    """导入报告的文本版（面向用户展示）。"""
    lines = [
        f"导入完成：{report['imported']} 个接口，base_url={report['base_url']}",
    ]
    if report.get("base_url_hint"):
        lines.append(f"\n⚠ {report['base_url_hint']}")
    if report["suspected_write"]:
        lines.append(f"\n疑似写接口（{len(report['suspected_write'])} 个，默认已按只读导入，确认后请标记 write: true）：")
        lines += [f"  - {n}" for n in report["suspected_write"]]
    if report["skipped"]:
        lines.append(f"\n跳过（{len(report['skipped'])} 个）：")
        lines += [f"  - {s['path']}（{s['reason']}）" for s in report["skipped"]]
    return "\n".join(lines)
