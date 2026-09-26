"""接口目录（api catalog）解析与校验模块。

用户通过 YAML 文件登记自己后端的接口清单（schema 见 examples/*.catalog.yaml），
本模块负责加载、校验并转换为 Pydantic 模型，供 http_request 工具与 agent 使用。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CatalogVersion = Literal[1]
AuthType = Literal["none", "bearer", "basic", "apikey"]


class AuthConfig(BaseModel):
    """鉴权配置。token 刷新相关字段仅在 bearer 模式下有意义。"""

    type: AuthType = "none"
    # bearer 固定 token（与 token_endpoint 二选一）
    token: Optional[str] = None
    # 通过登录端点换取 token（如 BladeX OAuth2 password 模式）
    token_endpoint: Optional[str] = None
    token_payload: dict[str, Any] = Field(default_factory=dict)
    # 登录请求附加头（如 BladeX 的 Basic Authorization、Tenant-Id）
    token_extra_headers: dict[str, str] = Field(default_factory=dict)
    # 以 URL 查询参数发送的字段（如 BladeX OAuth2 的 grant_type/scope 走 query）
    token_params: dict[str, Any] = Field(default_factory=dict)
    # 登录请求体格式：json（默认）或 form
    token_body_format: Literal["json", "form"] = "json"
    # 凭证字段变换：sm2 = 对 payload 值做 SM2 加密（BladeX「非手机号即密文」语义）。
    # sm2_public_key 为 04 开头的 hex 公钥；cipher_mode: 0=C1C2C3(默认,sm-crypto 兼容) 1=C1C3C2
    token_payload_transform: Optional[Literal["sm2"]] = None
    sm2_public_key: Optional[str] = None
    sm2_cipher_mode: int = 0
    # 两步登录：登录前先 GET captcha_endpoint 拿 {key, image}，key 放入
    # captcha_key_header 头，验证码值由凭证 captcha_code 提供（人工读图或 OCR）
    captcha_endpoint: Optional[str] = None
    captcha_key_header: str = "Captcha-Key"
    captcha_code_header: str = "Captcha-Code"
    token_path: str = "data.token"  # 登录响应中 token 的 JSON 路径
    # basic / apikey
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    api_key_header: str = "X-Api-Key"
    # bearer 模式的 token 请求头名（默认 Authorization；BladeX 网关用 Blade-Auth）
    token_header: str = "Authorization"

    @model_validator(mode="after")
    def _check_auth_fields(self) -> "AuthConfig":
        if self.type == "bearer" and not self.token and not self.token_endpoint:
            raise ValueError("bearer 鉴权需要提供 token 或 token_endpoint 之一")
        if self.type == "basic" and (not self.username or self.password is None):
            raise ValueError("basic 鉴权需要提供 username 与 password")
        if self.type == "apikey" and not self.api_key:
            raise ValueError("apikey 鉴权需要提供 api_key")
        if self.token_payload_transform == "sm2" and not self.sm2_public_key:
            raise ValueError("token_payload_transform=sm2 需要提供 sm2_public_key")
        return self


class PaginationConfig(BaseModel):
    page_param: str = "page"
    size_param: str = "size"
    total_path: Optional[str] = None      # 响应 JSON 中总数的路径，如 data.total
    total_header: Optional[str] = None    # 或从响应头读取总数，如 X-Total-Count


class ParamConfig(BaseModel):
    """单个接口参数说明（喂给 LLM 的关键信息）。"""

    type: Literal["string", "int", "float", "bool"] = "string"
    required: bool = False
    description: str = ""


class ApiConfig(BaseModel):
    """目录中的单个接口定义。"""

    name: str
    description: str = ""
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"] = "GET"
    path: str  # 支持 /posts/{id} 形式的路径参数
    params: dict[str, ParamConfig] = Field(default_factory=dict)
    result_path: str = "."                # 响应 JSON 中数据所在的路径
    pagination: Optional[PaginationConfig] = None
    write: bool = False                   # 写操作标记：False 时 agent 不可调用
    timeout: Optional[int] = None         # 覆盖 defaults.timeout
    extra_headers: dict[str, str] = Field(default_factory=dict)  # 覆盖/追加 defaults.extra_headers

    @field_validator("path")
    @classmethod
    def _path_must_start_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"接口路径必须以 / 开头: {v}")
        return v

    @property
    def path_param_names(self) -> list[str]:
        """从路径中提取 {xxx} 形式的路径参数名。"""
        import re

        return re.findall(r"\{(\w+)\}", self.path)


class DefaultsConfig(BaseModel):
    timeout: int = 30
    max_rows: int = 50
    # 所有请求附带的自定义头（如 BladeX 的 Tenant-Id / Blade-Requested-With）
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # 业务码校验（可选）：如 BladeX 信封 {code:200,...} 配
    # business_code_path: code / business_code_ok: 200。不配则跳过检查。
    business_code_path: Optional[str] = None
    business_code_ok: Any = 200

    def merged_headers(self, api: "ApiConfig") -> dict[str, str]:
        """目录默认头与接口级头合并，接口级优先。"""
        return {**self.extra_headers, **api.extra_headers}


class Catalog(BaseModel):
    """接口目录根模型。"""

    version: CatalogVersion = 1
    auth: AuthConfig = Field(default_factory=AuthConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    base_url: str  # 后端地址，不带末尾斜杠
    apis: list[ApiConfig] = Field(min_length=1)

    @field_validator("base_url")
    @classmethod
    def _base_url_no_trailing_slash(cls, v: str) -> str:
        v = v.rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"base_url 必须以 http:// 或 https:// 开头: {v}")
        return v

    def get_api(self, name: str) -> Optional[ApiConfig]:
        return next((a for a in self.apis if a.name == name), None)

    def list_api_summaries(self) -> list[dict[str, str]]:
        """生成给 LLM 看的接口摘要列表。"""
        return [
            {
                "name": a.name,
                "description": a.description,
                "method": a.method,
                "path": a.path,
                "params": {k: f"({p.type}, {'必填' if p.required else '可选'}) {p.description}"
                            for k, p in a.params.items()},
            }
            for a in self.apis
        ]


class CatalogError(ValueError):
    """目录文件不合法时抛出，message 面向最终用户（agent 会转述给对话者）。"""


def load_catalog(path: str | Path) -> Catalog:
    """从 YAML 文件加载接口目录，校验失败抛 CatalogError。"""
    p = Path(path)
    if not p.exists():
        raise CatalogError(f"接口目录文件不存在: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise CatalogError(f"YAML 格式错误: {e}") from e
    if not isinstance(raw, dict):
        raise CatalogError("目录文件顶层必须是键值映射（YAML dict）")
    try:
        return Catalog(**raw)
    except Exception as e:  # pydantic.ValidationError
        raise CatalogError(f"目录校验失败: {e}") from e


_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _substitute_credentials(node: Any, credentials: dict[str, str]) -> Any:
    """递归把 {{key}} 占位符替换为凭证值；没有对应凭证的占位符原样保留。

    用于目录中的敏感字段（如 token_payload 的 username/password）：目录可公开
    分享，真实凭据在 Dify 凭证表单中填写。
    """
    if isinstance(node, str):
        def _repl(m: re.Match) -> str:
            return str(credentials.get(m.group(1), m.group(0)))
        return _PLACEHOLDER_RE.sub(_repl, node)
    if isinstance(node, dict):
        return {k: _substitute_credentials(v, credentials) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute_credentials(v, credentials) for v in node]
    return node


def parse_catalog(raw: str | dict, credentials: dict[str, str] | None = None) -> Catalog:
    """从 YAML 字符串或 dict 解析接口目录（便于直接粘贴内容而非文件路径）。

    credentials 用于替换 {{key}} 占位符（Dify 凭证表单字段）。
    """
    if isinstance(raw, str):
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise CatalogError(f"YAML 格式错误: {e}") from e
    if not isinstance(raw, dict):
        raise CatalogError("目录内容顶层必须是键值映射（YAML dict）")
    if credentials:
        raw = _substitute_credentials(raw, credentials)
    try:
        return Catalog(**raw)
    except Exception as e:  # pydantic.ValidationError
        raise CatalogError(f"目录校验失败: {e}") from e
