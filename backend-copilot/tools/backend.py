"""backend 工具 provider：凭证校验 + 共享工具源声明。

凭证字段（见 backend.yaml credential_form）：
- base_url：后端网关地址
- catalog_yaml：接口目录 YAML（examples/*.catalog.yaml 内容，含 apis 清单）
- auth_type + 鉴权字段：none / bearer / basic / apikey
校验方式：解析 catalog YAML（结构合法即通过）；bearer+token_endpoint 时
尝试真实登录一次，登录失败视为凭证错误。
"""
from __future__ import annotations

from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from tools.auth_profile import AuthError, AuthProfile
from tools.catalog import CatalogError, parse_catalog


class BackendProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        base_url = (credentials.get("base_url") or "").strip()
        if not base_url:
            raise ToolProviderCredentialValidationError("base_url 不能为空")
        catalog_raw = credentials.get("catalog_yaml") or ""
        if not catalog_raw.strip():
            raise ToolProviderCredentialValidationError(
                "catalog_yaml 不能为空：请粘贴 examples/*.catalog.yaml 的完整内容"
            )
        try:
            catalog = parse_catalog(catalog_raw)
        except CatalogError as e:
            raise ToolProviderCredentialValidationError(f"接口目录校验失败：{e}") from e

        # bearer + 登录端点：真实登录一次验证凭证
        auth_cfg = catalog.auth
        if auth_cfg.type == "bearer" and auth_cfg.token_endpoint:
            try:
                AuthProfile(auth_cfg, base_url).build_headers()
            except AuthError as e:
                raise ToolProviderCredentialValidationError(f"鉴权验证失败：{e}") from e
