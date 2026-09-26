"""http_request 工具测试：分页/截断/路径参数/401刷新/错误拦截。"""
import json as J
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tools.catalog import parse_catalog
from tools.http_request import InvokeError, invoke_api

CATALOG_TEMPLATE = """
version: 1
base_url: http://127.0.0.1:{port}
auth:
  type: bearer
  token_endpoint: /auth/login
  token_payload: {{username: admin, password: pw}}
  token_path: data.token
defaults: {{timeout: 10, max_rows: 10}}
apis:
  - name: order_list
    description: 分页查询订单
    method: GET
    path: /orders
    params: {{status: {{type: int, required: false, description: 状态}}}}
    pagination: {{page_param: current, size_param: size, total_path: data.total}}
    result_path: data.records
  - name: order_detail
    method: GET
    path: /orders/{{id}}
    params: {{id: {{type: int, required: true, description: 订单ID}}}}
    result_path: data
  - name: order_close
    method: POST
    path: /orders/{{id}}/close
    write: true
    params: {{id: {{type: int, required: true, description: 订单ID}}}}
    result_path: data
"""


class MockBackend(BaseHTTPRequestHandler):
    state = {"login": 0}
    last: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = J.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        MockBackend.last = {"method": "POST", "path": self.path, "body": self.rfile.read(int(self.headers.get("Content-Length", 0)))}
        if self.path == "/auth/login":
            MockBackend.state["login"] += 1
            self._send({"data": {"token": f"TK{MockBackend.state['login']}", "expiresIn": 3600}})
        else:
            self._send({"data": {"ok": True}})

    def do_GET(self):
        MockBackend.last = {"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")}
        if self.path.startswith("/orders/"):
            self._send({"data": {"id": 1, "name": "订单1"}})
        elif self.path.startswith("/orders"):
            qs = dict(p.split("=") for p in self.path.split("?")[1].split("&")) if "?" in self.path else {}
            page, size = int(qs.get("current", 1)), int(qs.get("size", 50))
            total = 120
            rows = [{"id": i, "name": f"订单{i}"} for i in range((page - 1) * size, min(page * size, total))]
            self._send({"data": {"records": rows, "total": total}})
        else:
            self._send({}, 404)


@pytest.fixture()
def backend():
    MockBackend.state["login"] = 0
    srv = HTTPServer(("127.0.0.1", 0), MockBackend)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    catalog = parse_catalog(CATALOG_TEMPLATE.format(port=port))
    yield catalog
    srv.shutdown()


class TestInvoke:
    def test_pagination_truncation(self, backend):
        r = invoke_api(backend, "order_list", {"status": 1}, page=1, size=50)
        assert r["meta"]["total"] == 120
        assert r["meta"]["truncated"] is True and len(r["data"]) == 10

    def test_page2_correctness(self, backend):
        r = invoke_api(backend, "order_list", {}, page=2, size=10)
        assert r["data"][0]["id"] == 10

    def test_path_param_and_result_path(self, backend):
        r = invoke_api(backend, "order_detail", {"id": 1})
        assert r["data"]["name"] == "订单1"

    def test_bearer_login_flow(self, backend):
        invoke_api(backend, "order_list", {}, page=1, size=5)
        assert MockBackend.state["login"] >= 1
        assert MockBackend.last["auth"].startswith("Bearer TK")

    def test_missing_required_param(self, backend):
        with pytest.raises(InvokeError, match="缺少必填参数"):
            invoke_api(backend, "order_detail", {})

    def test_unknown_api(self, backend):
        with pytest.raises(InvokeError, match="不存在"):
            invoke_api(backend, "nope")

    def test_write_api_post_body(self, backend):
        r = invoke_api(backend, "order_close", {"id": 9}, allow_write=True)
        assert r["data"]["ok"] is True
        assert MockBackend.last["path"] == "/orders/9/close"


class TestWriteGuard:
    """P2 write 拦截:write:true 接口默认拒绝,allow_write=True 放行。"""

    @pytest.fixture()
    def server(self):
        calls = {"close": 0}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                calls["close"] += 1
                body = J.dumps({"code": 200, "data": {"ok": True}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                body = J.dumps({"data": {"records": [], "total": 0}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield srv, calls
        srv.shutdown()

    def _catalog(self, port):
        return parse_catalog(f"""
base_url: http://127.0.0.1:{port}
apis:
  - name: order_close
    method: POST
    path: /orders/{{id}}/close
    write: true
    result_path: data
    params:
      id: {{type: int, required: true}}
  - name: order_list
    method: GET
    path: /orders
""")

    def test_write_blocked_by_default(self, server):
        srv, calls = server
        c = self._catalog(srv.server_address[1])
        with pytest.raises(InvokeError, match="已拦截") as ei:
            invoke_api(c, "order_close", {"id": 1})
        assert ei.value.kind == "write_blocked"
        assert calls["close"] == 0  # 请求未发出

    def test_write_allowed_with_flag(self, server):
        srv, calls = server
        c = self._catalog(srv.server_address[1])
        r = invoke_api(c, "order_close", {"id": 1}, allow_write=True)
        assert r["data"] == {"ok": True}
        assert calls["close"] == 1

    def test_read_api_unaffected(self, server):
        srv, _ = server
        c = self._catalog(srv.server_address[1])
        invoke_api(c, "order_list")  # 只读接口不需要开关


class TestErrorKinds:
    """P2 错误分类:timeout/connect/401/403/not_json/business。"""

    @pytest.fixture()
    def server(self):
        mode = {"status": 200, "body": J.dumps({"code": 200, "data": 1}).encode(), "sleep": 0}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if mode["sleep"]:
                    import time as T
                    T.sleep(mode["sleep"])
                self.send_response(mode["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(mode["body"])))
                self.end_headers()
                self.wfile.write(mode["body"])

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield srv, mode
        srv.shutdown()

    def _catalog(self, port, extra=""):
        return parse_catalog(f"""
base_url: http://127.0.0.1:{port}
defaults: {{timeout: 10{extra}}}
apis:
  - {{name: a1, method: GET, path: /x}}
""")

    def test_kind_auth_401(self, server):
        srv, mode = server
        mode["status"], mode["body"] = 401, b"unauthorized"
        with pytest.raises(InvokeError) as ei:
            invoke_api(self._catalog(srv.server_address[1]), "a1")
        assert ei.value.kind == "auth"

    def test_kind_forbidden_403(self, server):
        srv, mode = server
        mode["status"], mode["body"] = 403, b"denied"
        with pytest.raises(InvokeError) as ei:
            invoke_api(self._catalog(srv.server_address[1]), "a1")
        assert ei.value.kind == "forbidden"

    def test_kind_http_500(self, server):
        srv, mode = server
        mode["status"], mode["body"] = 500, b"boom"
        with pytest.raises(InvokeError) as ei:
            invoke_api(self._catalog(srv.server_address[1]), "a1")
        assert ei.value.kind == "http"

    def test_kind_not_json(self, server):
        srv, mode = server
        mode["status"], mode["body"] = 200, b"<html>gateway error</html>"
        with pytest.raises(InvokeError) as ei:
            invoke_api(self._catalog(srv.server_address[1]), "a1")
        assert ei.value.kind == "not_json"

    def test_kind_business(self, server):
        srv, mode = server
        mode["status"] = 200
        mode["body"] = J.dumps({"code": 500, "msg": "系统异常"}).encode()
        c = self._catalog(srv.server_address[1], extra:=", business_code_path: code")
        with pytest.raises(InvokeError) as ei:
            invoke_api(c, "a1")
        assert ei.value.kind == "business"
        assert "系统异常" in str(ei.value)

    def test_kind_timeout(self, server):
        srv, mode = server
        mode["status"], mode["sleep"] = 200, 1.0
        c = parse_catalog(f"""
base_url: http://127.0.0.1:{srv.server_address[1]}
defaults: {{timeout: 1}}
apis:
  - {{name: a1, method: GET, path: /x, timeout: 1}}
""")
        # timeout=1s 配置为秒?目录里 timeout 单位秒,httpx 需要秒——1s 睡眠 + 1s 超时可能竞态,放宽
        c.defaults.timeout = 1
        with pytest.raises(InvokeError) as ei:
            c2 = c.model_copy(deep=True)
            invoke_api(c2, "a1")
        assert ei.value.kind in ("timeout",)  # 1s 睡眠应触发 1s 超时

    def test_kind_connect(self):
        c = self._catalog(59999)  # 未监听端口
        with pytest.raises(InvokeError) as ei:
            invoke_api(c, "a1")
        # 连接拒绝在本机/gevent 环境可能表现为 ReadTimeout，二者都属"到不了后端"
        assert ei.value.kind in ("connect", "timeout")
