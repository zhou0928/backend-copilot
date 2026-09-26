"""http_request 工具：目录驱动的后端调用（核心逻辑 + Dify Tool 入口）。

invoke_api：从接口目录找到 api 定义，组装请求（路径参数/query/体），经
AuthProfile 注入鉴权（401 自动刷新重试一次），result_path 提取数据并按
max_rows 截断（附总数与截断说明，防止撑爆 agent 上下文）。
BackendHttpRequestTool：Dify Tool 桥接层，凭证 + 粘贴的 catalog YAML。
"""
from __future__ import annotations

import json
import re
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from tools.auth_profile import AuthProfile
from tools.catalog import ApiConfig, Catalog, CatalogError, parse_catalog


class InvokeError(RuntimeError):
    """调用失败，message 面向最终用户（agent 会转述）。

    kind 用于错误分类（agent 可据此自纠）：
    timeout / auth(401) / forbidden(403) / http(其他 4xx/5xx) /
    not_json / business(业务码失败) / bad_request(参数/目录问题) / write_blocked
    """

    def __init__(self, message: str, kind: str = "bad_request") -> None:
        super().__init__(message)
        self.kind = kind


class InvokeResult(dict):
    """结构化调用结果：data（截断后数据）+ meta（截断/分页说明）。"""


def _extract_path(data: Any, path: str) -> Any:
    if path in (".", "", None):
        return data
    cur: Any = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _count_items(data: Any) -> int | None:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("total", "count", "totalCount", "records"):
            if key in data:
                v = data[key]
                if isinstance(v, int):
                    return v
                if isinstance(v, list):
                    return len(v)
    return None


def _truncate(data: Any, max_rows: int) -> tuple[Any, int | None, bool]:
    if isinstance(data, list) and len(data) > max_rows:
        total = len(data)
        return data[:max_rows], total, True
    return data, _count_items(data), False


def _validate_and_split_params(api: ApiConfig, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    params = params or {}
    path_names = api.path_param_names
    path_vals: dict[str, Any] = {k: v for k, v in params.items() if k in path_names}
    rest: dict[str, Any] = {k: v for k, v in params.items() if k not in path_names}
    missing = [n for n in path_names if n not in path_vals]
    missing += [k for k, p in api.params.items() if p.required and k not in params]
    if missing:
        raise InvokeError(f"缺少必填参数: {', '.join(missing)}（接口 {api.name}）")
    return path_vals, rest


def invoke_api(
    catalog: Catalog,
    api_name: str,
    params: dict[str, Any] | None = None,
    page: int | None = None,
    size: int | None = None,
    allow_write: bool = False,
    credentials: dict[str, str] | None = None,
) -> InvokeResult:
    """按目录调用接口。raise InvokeError / CatalogError。

    allow_write=False（默认）时拒绝调用目录中标记 write:true 的接口——
    写操作必须同时满足「目录显式 write:true」与「凭证 allow_write 开启」。
    """
    api = catalog.get_api(api_name)
    if api is None:
        known = ", ".join(a.name for a in catalog.apis)
        raise InvokeError(f"接口目录中不存在名为 {api_name!r} 的接口。可用接口: {known}")

    if api.write and not allow_write:
        raise InvokeError(
            f"接口 {api_name} 是写操作（目录标记 write:true），已拦截。"
            "如确需开放写操作，请在插件凭证中开启 allow_write",
            kind="write_blocked",
        )

    path_vals, rest = _validate_and_split_params(api, params or {})

    path = api.path
    for k, v in path_vals.items():
        path = re.sub(r"\{" + k + r"\}", str(v), path)

    if api.pagination and (page is not None or size is not None):
        rest = dict(rest)
        if page is not None:
            rest[api.pagination.page_param] = page
        if size is not None:
            rest[api.pagination.size_param] = size

    url = f"{catalog.base_url}{path}"
    timeout = api.timeout or catalog.defaults.timeout
    auth = AuthProfile(catalog.auth, catalog.base_url, timeout=timeout,
                       credentials=credentials)

    def _do(force_refresh: bool) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            **catalog.defaults.merged_headers(api),
            **auth.build_headers(force_refresh=force_refresh),
        }
        try:
            # trust_env=False:插件直连用户内网后端,不经过系统/环境代理
            if api.method in ("GET", "DELETE"):
                return httpx.request(api.method, url, params=rest, headers=headers, timeout=timeout, trust_env=False)
            return httpx.request(api.method, url, json=rest, headers=headers, timeout=timeout, trust_env=False)
        except httpx.ConnectError as e:
            raise InvokeError(f"无法连接后端 {catalog.base_url}（请检查地址/网络）：{e}", kind="connect") from e
        except httpx.TimeoutException as e:
            raise InvokeError(f"后端请求超时（{timeout}s）：{url}", kind="timeout") from e
        except httpx.HTTPError as e:
            raise InvokeError(f"后端请求失败: {e}", kind="http") from e

    resp = _do(force_refresh=False)
    if auth.on_response(resp.status_code):
        resp = _do(force_refresh=True)

    if resp.status_code in (401, 403):
        kind = "auth" if resp.status_code == 401 else "forbidden"
        raise InvokeError(
            f"后端返回 HTTP {resp.status_code}（{'鉴权失败，token 可能已过期或无效' if kind == 'auth' else '无权限访问该接口'}）: {resp.text[:200]}",
            kind=kind,
        )
    if resp.status_code >= 400:
        raise InvokeError(f"后端返回错误 HTTP {resp.status_code}: {resp.text[:300]}", kind="http")

    try:
        body = resp.json()
    except ValueError as e:
        raise InvokeError("后端响应不是合法 JSON（请检查接口地址是否正确）", kind="not_json") from e

    # 业务码校验（可选）：HTTP 200 但业务 code 失败的场景（如 BladeX {code:500,...}）
    bc_path = catalog.defaults.business_code_path
    if bc_path:
        bc = _extract_path(body, bc_path)
        if bc is not None and str(bc) != str(catalog.defaults.business_code_ok):
            msg = _extract_path(body, "msg") or _extract_path(body, "message") or ""
            raise InvokeError(
                f"后端业务错误（{bc_path}={bc}）: {str(msg)[:200] or '无错误信息'}",
                kind="business",
            )

    data = _extract_path(body, api.result_path)

    total: int | None = None
    if api.pagination:
        if api.pagination.total_path:
            total = _extract_path(body, api.pagination.total_path)
        elif api.pagination.total_header and api.pagination.total_header in resp.headers:
            try:
                total = int(resp.headers[api.pagination.total_header])
            except ValueError:
                total = None

    data, listed, truncated = _truncate(data, catalog.defaults.max_rows)
    if truncated and total is None:
        total = listed

    meta: dict[str, Any] = {"api": api.name, "method": api.method, "url": url, "status": resp.status_code}
    if total is not None:
        meta["total"] = total
    if truncated:
        meta["truncated"] = True
        meta["note"] = f"结果已截断：共 {total} 条，仅展示前 {catalog.defaults.max_rows} 条，可用分页参数获取更多"
    return InvokeResult(data=data, meta=meta)


# ---------------- Dify Tool 桥接层 ----------------


class BackendHttpRequestTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage, None, None]:
        c = self.runtime.credentials or {}
        api_name = (tool_parameters.get("api_name") or "").strip()
        params_raw = tool_parameters.get("params") or "{}"
        catalog_raw = (tool_parameters.get("catalog_yaml") or c.get("catalog_yaml") or "")

        try:
            params = json.loads(params_raw) if isinstance(params_raw, str) else dict(params_raw)
            if not isinstance(params, dict):
                raise ValueError("params 必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as e:
            yield self.create_json_message({"error": f"params 不是合法 JSON：{e}"})
            return

        try:
            catalog = self._build_catalog(c, catalog_raw)
        except CatalogError as e:
            yield self.create_json_message({"error": str(e)})
            return

        page = self._to_int(tool_parameters.get("page"))
        size = self._to_int(tool_parameters.get("size"))
        # 凭证级写开关：默认 false,写接口一律拦截
        allow_write = str(c.get("allow_write", "")).strip().lower() in ("1", "true", "yes", "on")
        try:
            result = invoke_api(catalog, api_name, params, page=page, size=size,
                                allow_write=allow_write, credentials=c)
        except (InvokeError, CatalogError) as e:
            # 结构化错误：kind 帮助 agent 自纠（timeout→重试、auth→提示重新登录等）
            err: dict[str, Any] = {"error": str(e)}
            if isinstance(e, InvokeError):
                err["error_kind"] = e.kind
            yield self.create_json_message(err)
            return
        yield self.create_json_message(dict(result))

    @staticmethod
    def _to_int(v: Any) -> int | None:
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _build_catalog(self, c: dict[str, str], catalog_yaml: str) -> Catalog:
        """优先用粘贴的完整 YAML；否则报错引导用户粘贴（凭证模式无法凭空造 apis）。

        凭证字段传入 parse_catalog，用于替换目录中的 {{key}} 占位符。
        """
        if catalog_yaml.strip():
            return parse_catalog(catalog_yaml, credentials=c)
        raise CatalogError(
            "未提供接口目录：请在工具的 catalog_yaml 参数（或凭证 catalog_yaml 字段）粘贴 examples/*.catalog.yaml 的内容"
        )
