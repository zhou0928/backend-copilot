"""openapi_import 测试：BladeX 风格(3.x 信封)与 RuoYi 风格(2.0 rows/total)文档导入。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tools.catalog import CatalogError, parse_catalog
from tools.openapi_import import (
    catalog_to_yaml,
    generate_report_text,
    import_openapi,
)

# ---------------- BladeX 风格 OpenAPI 3.0(信封 data.records / data.total) ----------------

_LIST_RESP_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "integer"},
        "data": {"type": "object", "properties": {
            "total": {"type": "integer"},
            "records": {"type": "array", "items": {"type": "object"}},
        }},
    },
}

_DETAIL_RESP_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "integer"},
        "data": {"type": "object", "properties": {"id": {"type": "integer"}}},
    },
}

_SUBMIT_BODY_SCHEMA = {
    "type": "object",
    "required": ["title"],
    "properties": {
        "title": {"type": "string", "description": "工单标题"},
        "priority": {"type": "integer"},
    },
}

BLADEX_DOC = {
    "openapi": "3.0.1",
    "info": {"title": "BladeX Demo", "version": "1.0"},
    "servers": [{"url": "http://host.docker.internal:9998"}],
    "paths": {
        "/blade-ticket/ticket/list": {
            "get": {
                "operationId": "ticketList",
                "summary": "分页查询工单列表",
                "parameters": [
                    {"name": "current", "in": "query", "schema": {"type": "integer"},
                     "description": "页码"},
                    {"name": "size", "in": "query", "schema": {"type": "integer"},
                     "description": "每页条数"},
                    {"name": "keyword", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"content": {"application/json": {"schema": _LIST_RESP_SCHEMA}}}},
            },
        },
        "/blade-ticket/ticket/detail": {
            "get": {
                "operationId": "ticketDetail",
                "summary": "工单详情",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"content": {"application/json": {"schema": _DETAIL_RESP_SCHEMA}}}},
            },
        },
        "/blade-ticket/ticket/submit": {
            "post": {
                "operationId": "ticketSubmit",
                "summary": "提交工单",
                "requestBody": {"content": {"application/json": {"schema": _SUBMIT_BODY_SCHEMA}}},
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"code": {"type": "integer"}},
                }}}}},
            },
        },
        "/blade-auth/oauth/token": {
            "post": {"operationId": "oauthToken", "summary": "登录换 token",
                     "responses": {"200": {"description": "ok"}}},
        },
    },
}

# ---------------- RuoYi 风格 Swagger 2.0(顶层 rows/total) ----------------

RUOYI_DOC = {
    "swagger": "2.0",
    "info": {"title": "RuoYi", "version": "1.0"},
    "host": "host.docker.internal:8080",
    "basePath": "/prod-api",
    "paths": {
        "/system/user/list": {
            "get": {
                "operationId": "listUser",
                "summary": "查询用户列表",
                "parameters": [
                    {"name": "pageNum", "in": "query", "type": "integer"},
                    {"name": "pageSize", "in": "query", "type": "integer"},
                    {"name": "userName", "in": "query", "type": "string"},
                ],
                "responses": {
                    "200": {"schema": {"type": "object", "properties": {
                        "total": {"type": "integer"},
                        "rows": {"type": "array", "items": {"type": "object"}},
                    }}},
                },
            },
        },
        "/system/user/{userId}": {
            "get": {
                "operationId": "getUser",
                "summary": "用户详情",
                "parameters": [
                    {"name": "userId", "in": "path", "required": True, "type": "integer"},
                ],
                "responses": {
                    "200": {"schema": {"$ref": "#/definitions/UserDetail"}},
                },
            },
        },
    },
    "definitions": {
        "UserDetail": {"type": "object", "properties": {
            "code": {"type": "integer"},
            "data": {"type": "object", "properties": {"userId": {"type": "integer"},
                                                      "userName": {"type": "string"}}},
        }},
    },
}


class TestBladexStyle:
    def test_import_basic(self):
        cat, report = import_openapi(json.dumps(BLADEX_DOC, ensure_ascii=False))
        assert cat.base_url == "http://host.docker.internal:9998"
        assert report["imported"] == 4
        names = {a.name for a in cat.apis}
        assert {"ticket_list", "ticket_detail", "ticket_submit", "oauth_token"} == names

    def test_pagination_detected(self):
        cat, _ = import_openapi(json.dumps(BLADEX_DOC))
        a = cat.get_api("ticket_list")
        assert a.pagination is not None
        assert a.pagination.page_param == "current"
        assert a.pagination.size_param == "size"
        assert a.pagination.total_path == "data.total"

    def test_result_path_envelope(self):
        cat, _ = import_openapi(json.dumps(BLADEX_DOC))
        assert cat.get_api("ticket_list").result_path == "data.records"
        assert cat.get_api("ticket_detail").result_path == "data"

    def test_request_body_props(self):
        cat, _ = import_openapi(json.dumps(BLADEX_DOC))
        a = cat.get_api("ticket_submit")
        assert a.params["title"].required is True
        assert a.params["priority"].required is False
        assert "标题" in a.params["title"].description

    def test_path_param_required(self):
        cat, _ = import_openapi(json.dumps(BLADEX_DOC))
        assert cat.get_api("ticket_detail").params["id"].required is True

    def test_write_default_readonly_with_suspects(self):
        cat, report = import_openapi(json.dumps(BLADEX_DOC))
        assert all(a.write is False for a in cat.apis)  # 保守：全部只读
        assert set(report["suspected_write"]) >= {"ticket_submit", "oauth_token"}

    def test_exclude_auth_endpoint(self):
        cat, report = import_openapi(
            json.dumps(BLADEX_DOC), exclude=["^/blade-auth/"])
        assert "oauth_token" not in {a.name for a in cat.apis}
        assert report["imported"] == 3


class TestRuoyiStyle:
    def test_swagger2_host_basepath(self):
        cat, _ = import_openapi(json.dumps(RUOYI_DOC))
        assert cat.base_url == "http://host.docker.internal:8080/prod-api"

    def test_top_level_rows_result_path(self):
        cat, _ = import_openapi(json.dumps(RUOYI_DOC))
        a = cat.get_api("list_user")
        assert a.result_path == "rows"
        assert a.pagination is not None
        assert a.pagination.page_param == "page_num"  # camelCase 归一为 snake
        assert a.pagination.total_path == "total"

    def test_ref_definition(self):
        cat, _ = import_openapi(json.dumps(RUOYI_DOC))
        # $ref 定义里 data 是对象 → result_path 取 data
        assert cat.get_api("get_user").result_path == "data"
        assert cat.get_api("get_user").params["user_id"].required is True


class TestRoundtrip:
    """草稿 YAML → parse_catalog 回读，与草稿模型一致。"""

    def test_yaml_roundtrip(self):
        cat, _ = import_openapi(json.dumps(BLADEX_DOC))
        yaml_text = catalog_to_yaml(cat)
        cat2 = parse_catalog(yaml_text)
        assert cat2.base_url == cat.base_url
        assert {a.name for a in cat2.apis} == {a.name for a in cat.apis}
        a1 = cat2.get_api("ticket_list")
        assert a1.pagination.total_path == "data.total"
        assert a1.result_path == "data.records"
        assert a1.method == "GET"
        # 分页参数保留
        assert "current" in a1.params and "size" in a1.params

    def test_roundtrip_write_flag(self):
        cat, _ = import_openapi(json.dumps(BLADEX_DOC))
        yaml_text = catalog_to_yaml(cat)
        # 人工确认后加 write: true（插入在该接口块内、与 name 同级缩进），再回读应保留
        yaml_text = yaml_text.replace(
            "- name: ticket_submit", "- name: ticket_submit\n  write: true")
        cat2 = parse_catalog(yaml_text)
        assert cat2.get_api("ticket_submit").write is True


class TestReport:
    def test_report_text(self):
        _, report = import_openapi(json.dumps(BLADEX_DOC))
        text = generate_report_text(report)
        assert "4 个接口" in text
        assert "ticket_submit" in text  # 疑似写接口列出
        assert "write: true" in text

    def test_invalid_doc(self):
        with pytest.raises(CatalogError, match="paths"):
            import_openapi('{"foo": 1}')

    def test_all_filtered_out(self):
        with pytest.raises(CatalogError, match="没有可导入"):
            import_openapi(json.dumps(BLADEX_DOC), exclude=[".*"])

    def test_include_filter(self):
        cat, report = import_openapi(json.dumps(BLADEX_DOC), include=["^/blade-ticket/"])
        assert report["imported"] == 3
        assert {a.name for a in cat.apis} == {"ticket_list", "ticket_detail", "ticket_submit"}


class TestBaseUrlHint:
    """内网 base_url 检测提示。"""

    def test_private_ip_hint(self):
        import json as J
        _, report = import_openapi(J.dumps(BLADEX_DOC))  # servers: 172.16.x.x
        assert "内网" in report["base_url_hint"]

    def test_public_url_no_hint(self):
        import json as J
        doc = J.loads(J.dumps(BLADEX_DOC))
        doc["servers"] = [{"url": "https://api.example.com"}]
        _, report = import_openapi(J.dumps(doc))
        assert report["base_url_hint"] == ""
