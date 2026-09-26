"""鉴权档案（auth_profile）：按目录中的 auth 配置为请求注入凭据。

支持四种模式（与 catalog.AuthConfig 对应）：
- none:    不做任何处理（内网/开放接口）
- bearer:  固定 token，或经 token_endpoint 登录换取（如 BladeX OAuth2 password 模式），
           缓存并自动刷新；请求遇 401 可强制刷新重试
- basic:   HTTP Basic（base64(user:pass)）
- apikey:  自定义请求头携带 API Key
"""
from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any, Optional

import httpx

from tools.catalog import AuthConfig


class AuthError(RuntimeError):
    """鉴权失败（登录失败、响应结构不符等），message 面向最终用户。"""


class AuthProfile:
    """按 AuthConfig 生成请求头；bearer 模式带 token 缓存与刷新。"""

    def __init__(self, config: AuthConfig, base_url: str, timeout: int = 30,
                 credentials: dict[str, str] | None = None):
        self._config = config
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # 凭证（用于 captcha_code 等登录时才提供的值）
        self._credentials = credentials or {}
        # token 缓存按凭证内容键控，避免多目录/多租户互相覆盖
        self._token_cache: dict[str, tuple[str, float]] = {}  # key -> (token, expire_ts)
        self._lock = threading.Lock()

    # ---------- 对外主入口 ----------

    def build_headers(self, force_refresh: bool = False) -> dict[str, str]:
        """生成该次请求所需的鉴权头。"""
        cfg = self._config
        if cfg.type == "none":
            return {}
        if cfg.type == "bearer":
            token = self._get_token(force_refresh=force_refresh)
            return {cfg.token_header: f"Bearer {token}"}
        if cfg.type == "basic":
            raw = f"{cfg.username}:{cfg.password}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        if cfg.type == "apikey":
            return {cfg.api_key_header: cfg.api_key or ""}
        return {}

    def on_response(self, status_code: int) -> bool:
        """请求后回调：返回 True 表示"建议刷新凭据后重试一次"（当前仅处理 401）。"""
        return status_code == 401 and self._config.type == "bearer" and bool(self._config.token_endpoint)

    # ---------- bearer：固定 token / 登录换取 ----------

    def _get_token(self, force_refresh: bool = False) -> str:
        cfg = self._config
        if not cfg.token_endpoint:
            # 固定 token，无刷新逻辑
            if not cfg.token:
                raise AuthError("bearer 鉴权未配置 token")
            return cfg.token

        cache_key = self._cache_key()
        now = time.time()
        with self._lock:
            cached = self._token_cache.get(cache_key)
            if cached and not force_refresh and cached[1] > now:
                return cached[0]

        token, expire_ts = self._fetch_token()
        with self._lock:
            self._token_cache[cache_key] = (token, expire_ts)
        return token

    def _fetch_captcha_key(self, cfg: AuthConfig) -> str:
        """两步登录第一步：GET captcha 端点，从响应中提取验证码 key。

        兼容 {key: ...} / {data: {key: ...}} 两种响应结构。
        """
        url = f"{self._base_url}{cfg.captcha_endpoint}"
        try:
            resp = httpx.get(url, trust_env=False, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise AuthError(f"验证码端点请求失败: {e}") from e
        if resp.status_code != 200:
            raise AuthError(f"获取验证码失败（HTTP {resp.status_code}）：{resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise AuthError("验证码响应不是合法 JSON") from e
        key = data.get("key") if isinstance(data, dict) else None
        if not key and isinstance(data, dict):
            key = (data.get("data") or {}).get("key")
        if not key:
            raise AuthError("验证码响应中未找到 key 字段")
        return str(key)

    def _transform_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按 token_payload_transform 对登录 payload 做字段变换。

        sm2：对每个字符串值做 SM2 加密（sm-crypto 兼容的 C1C2C3、无 04 前缀 hex），
        对应 BladeX「非手机号一律视为已加密」的服务端语义。
        """
        cfg = self._config
        if cfg.token_payload_transform != "sm2":
            return payload
        try:
            from gmssl import sm2 as gm_sm2
        except ImportError as e:
            raise AuthError("SM2 变换需要安装 gmssl 依赖") from e
        crypt = gm_sm2.CryptSM2(public_key=cfg.sm2_public_key, private_key="",
                                mode=cfg.sm2_cipher_mode)

        def _enc(value: Any) -> Any:
            if isinstance(value, str):
                return crypt.encrypt(value.encode()).hex()
            if isinstance(value, dict):
                return {k: _enc(v) for k, v in value.items()}
            return value

        return {k: _enc(v) for k, v in payload.items()}

    def _cache_key(self) -> str:
        cfg = self._config
        payload = json.dumps(cfg.token_payload, sort_keys=True, ensure_ascii=False)
        return f"{self._base_url}{cfg.token_endpoint}::{payload}"

    def _fetch_token(self) -> tuple[str, float]:
        """调用 token_endpoint 换取 token。默认提前 120 秒过期以规避时钟偏差。

        配置 captcha_endpoint 时走两步登录：先 GET 验证码端点拿 {key,...}，
        key 放入 captcha_key_header 头，验证码值取凭证 captcha_code。
        """
        cfg = self._config
        url = f"{self._base_url}{cfg.token_endpoint}"
        body = self._transform_payload(cfg.token_payload) if cfg.token_payload else None
        headers: dict[str, str] = {"Content-Type": "application/json", **cfg.token_extra_headers}
        if cfg.captcha_endpoint:
            captcha_key = self._fetch_captcha_key(cfg)
            headers[cfg.captcha_key_header] = captcha_key
            captcha_code = self._credentials.get("captcha_code", "")
            if captcha_code:
                headers[cfg.captcha_code_header] = captcha_code
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": self._timeout,
        }
        if cfg.token_params:
            kwargs["params"] = cfg.token_params
        if body is not None and cfg.token_body_format == "form":
            kwargs["data"] = body
        else:
            kwargs["json"] = body
        try:
            # trust_env=False:登录请求直连后端,不走系统代理
            resp = httpx.post(url, trust_env=False, **kwargs)
        except httpx.HTTPError as e:
            raise AuthError(f"登录端点请求失败: {e}") from e
        if resp.status_code != 200:
            raise AuthError(f"登录失败（HTTP {resp.status_code}）：{resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise AuthError("登录响应不是合法 JSON") from e
        token = self._extract_path(data, cfg.token_path)
        if not token or not isinstance(token, str):
            raise AuthError(f"登录响应中未找到 token（路径 {cfg.token_path}）")
        # 过期时间：响应若带 expiresIn/expires_in（秒）则采用，否则默认 55 分钟
        expire = None
        if isinstance(data, dict):
            expire = data.get("expiresIn") or data.get("expires_in") or data.get("data", {}).get("expiresIn")
        try:
            expire = int(expire) if expire else 55 * 60
        except (TypeError, ValueError):
            expire = 55 * 60
        return token, time.time() + max(expire - 120, 60)

    @staticmethod
    def _extract_path(data: Any, path: str) -> Any:
        """按 a.b.c 路径从嵌套 dict 中取值。"""
        cur: Any = data
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur
