"""auth_profile 模块测试：四种鉴权头 + token 缓存/刷新。"""
import base64
import json as J
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from pydantic import ValidationError

from tools.auth_profile import AuthError, AuthProfile
from tools.catalog import AuthConfig


class TestStaticAuth:
    def test_none(self):
        p = AuthProfile(AuthConfig(type="none"), "http://x.com")
        assert p.build_headers() == {}
        assert p.on_response(401) is False

    def test_basic(self):
        p = AuthProfile(AuthConfig(type="basic", username="u", password="p"), "http://x.com")
        assert p.build_headers()["Authorization"] == "Basic " + base64.b64encode(b"u:p").decode()

    def test_apikey(self):
        p = AuthProfile(AuthConfig(type="apikey", api_key="k1", api_key_header="X-Key"), "http://x.com")
        assert p.build_headers() == {"X-Key": "k1"}

    def test_bearer_fixed_token(self):
        p = AuthProfile(AuthConfig(type="bearer", token="T1"), "http://x.com")
        assert p.build_headers()["Authorization"] == "Bearer T1"
        assert p.on_response(401) is False  # 固定 token 无刷新逻辑

    def test_bearer_missing_token(self):
        # AuthConfig 在构造时就应拒绝（缺 token 且缺 token_endpoint）
        with pytest.raises(ValidationError):
            AuthConfig(type="bearer")


class TestBearerRefresh:
    """用本地 mock 服务验证登录换 token、缓存、401 强制刷新。"""

    @pytest.fixture()
    def server(self):
        calls = {"login": 0}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                calls["login"] += 1
                body = J.dumps({"data": {"token": f"TK{calls['login']}", "expiresIn": 3600}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield srv, calls
        srv.shutdown()

    def test_login_cache_refresh(self, server):
        srv, calls = server
        port = srv.server_address[1]
        p = AuthProfile(
            AuthConfig(type="bearer", token_endpoint="/auth/login",
                       token_payload={"username": "u", "password": "p"}, token_path="data.token"),
            f"http://127.0.0.1:{port}",
        )
        h1 = p.build_headers()
        h2 = p.build_headers()
        assert h1 == h2 and calls["login"] == 1  # 缓存命中
        assert p.on_response(401) is True
        h3 = p.build_headers(force_refresh=True)
        assert h3 != h1 and calls["login"] == 2  # 强制刷新重新登录

    def test_login_bad_response(self, server):
        srv, _ = server
        port = srv.server_address[1]
        p = AuthProfile(
            AuthConfig(type="bearer", token_endpoint="/auth/login", token_path="wrong.path"),
            f"http://127.0.0.1:{port}",
        )
        with pytest.raises(AuthError, match="未找到 token"):
            p.build_headers()


class TestTokenRequestShape:
    """token_extra_headers / token_params / token_body_format(BladeX 登录形态)。"""

    @pytest.fixture()
    def echo_server(self):
        seen = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                from urllib.parse import parse_qs, urlparse
                u = urlparse(self.path)
                seen["query"] = {k: v[0] for k, v in parse_qs(u.query).items()}
                seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
                length = int(self.headers.get("Content-Length") or 0)
                seen["body"] = self.rfile.read(length).decode()
                body = J.dumps({"data": {"token": "TK", "expiresIn": 3600}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield srv, seen
        srv.shutdown()

    def test_bladex_style_login(self, echo_server):
        srv, seen = echo_server
        port = srv.server_address[1]
        p = AuthProfile(
            AuthConfig(
                type="bearer", token_endpoint="/blade-auth/oauth/token",
                token_params={"grant_type": "password", "scope": "all"},
                token_body_format="form",
                token_payload={"username": "admin", "password": "pw"},
                token_extra_headers={"Authorization": "Basic saber", "Tenant-Id": "000000"},
                token_path="data.token",
            ),
            f"http://127.0.0.1:{port}",
        )
        assert p.build_headers()["Authorization"] == "Bearer TK"
        assert seen["query"] == {"grant_type": "password", "scope": "all"}
        assert seen["headers"]["authorization"] == "Basic saber"
        assert seen["headers"]["tenant-id"] == "000000"
        assert "username=admin" in seen["body"]  # form 体而非 JSON


class TestNewAuthFeatures:
    """M1 适配四项:token_header / sm2 变换 / 两步登录 captcha。"""

    def test_token_header_custom(self):
        p = AuthProfile(AuthConfig(type="bearer", token="T1", token_header="Blade-Auth"), "http://x.com")
        assert p.build_headers() == {"Blade-Auth": "Bearer T1"}

    def test_token_header_default(self):
        p = AuthProfile(AuthConfig(type="bearer", token="T1"), "http://x.com")
        assert p.build_headers() == {"Authorization": "Bearer T1"}

    def test_sm2_transform_requires_key(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AuthConfig(type="bearer", token="T", token_payload_transform="sm2")

    def test_sm2_transform_payload(self):
        from gmssl import sm2 as gm
        PK = "04" + "01" * 32 + "02" * 32  # 非法曲线点,但只需走到加密前
        # 用真实生成的公钥对验证变换格式
        import subprocess, json as J
        cfg = AuthConfig(
            type="bearer", token_endpoint="/login",
            token_payload={"username": "admin", "password": "pw"},
            token_payload_transform="sm2",
            sm2_public_key="04b271d0ef9ad1470bf46c4b1f748194e714a12229fc6efcc0bf527cd73c6005649e12b5124693da01456a163b0d5eb9a78802f96cf5c013de5e4dbf2476bb9031",
            token_path="data.token",
        )
        p = AuthProfile(cfg, "http://127.0.0.1:1")
        out = p._transform_payload({"username": "admin", "password": "pw", "grant_type": "password"})
        # 字符串值被加密成 hex;非字符串(如 grant_type 不在时)原样——本例全是字符串
        for v in out.values():
            assert isinstance(v, str) and all(c in "0123456789abcdef" for c in v)
            assert not v.startswith("04")  # C1C2C3 无前缀(sm-crypto 兼容)

    def test_captcha_two_step(self):
        """两步登录:先 GET captcha 端点,key 放入头,验证码值取凭证。"""
        seen = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = J.dumps({"key": "CK123", "image": "data:image/png;base64,xx"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                seen["captcha_key"] = self.headers.get("Captcha-Key")
                seen["captcha_code"] = self.headers.get("Captcha-Code")
                body = J.dumps({"data": {"token": "TK", "expiresIn": 3600}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cfg = AuthConfig(
                type="bearer", token_endpoint="/oauth/token",
                captcha_endpoint="/oauth/captcha",
                token_payload={"username": "u", "password": "p"},
                token_path="data.token",
            )
            p = AuthProfile(cfg, f"http://127.0.0.1:{srv.server_address[1]}",
                            credentials={"captcha_code": "Ab12Cd"})
            assert p.build_headers()["Authorization"] == "Bearer TK"
            assert seen["captcha_key"] == "CK123"
            assert seen["captcha_code"] == "Ab12Cd"
        finally:
            srv.shutdown()

    def test_captcha_data_envelope(self):
        """captcha 响应为 {data:{key}} 信封时也能提取。"""
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = J.dumps({"data": {"key": "ENV"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = J.dumps({"data": {"token": "T"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cfg = AuthConfig(type="bearer", token_endpoint="/t", captcha_endpoint="/c",
                             token_payload={"u": "x"}, token_path="data.token")
            p = AuthProfile(cfg, f"http://127.0.0.1:{srv.server_address[1]}")
            assert p.build_headers()["Authorization"] == "Bearer T"
        finally:
            srv.shutdown()
