"""catalog 模块测试：加载、校验、路径参数、摘要生成。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tools.catalog import CatalogError, load_catalog, parse_catalog


DEMO = """
version: 1
base_url: https://api.example.com
auth:
  type: none
defaults: {timeout: 20, max_rows: 30}
apis:
  - name: list_items
    description: 分页查询列表
    method: GET
    path: /items
    params: {status: {type: int, required: false, description: 状态}}
    pagination: {page_param: current, size_param: size, total_path: data.total}
    result_path: data.records
  - name: get_item
    method: GET
    path: /items/{id}
    params: {id: {type: int, required: true, description: ID}}
    result_path: data
"""


class TestCatalogLoad:
    def test_load_demo_example_file(self):
        c = load_catalog(Path(__file__).parent.parent / "examples" / "demo.catalog.yaml")
        assert c.base_url == "https://jsonplaceholder.typicode.com"
        assert [a.name for a in c.apis] == ["list_posts", "get_post"]

    def test_parse_from_string(self):
        c = parse_catalog(DEMO)
        assert c.defaults.max_rows == 30
        assert c.get_api("list_items") is not None
        assert c.get_api("nope") is None

    def test_path_param_extraction(self):
        c = parse_catalog(DEMO)
        assert c.get_api("get_item").path_param_names == ["id"]
        assert c.get_api("list_items").path_param_names == []

    def test_llm_summaries(self):
        c = parse_catalog(DEMO)
        s = c.list_api_summaries()
        assert s[0]["name"] == "list_items"
        assert "状态" in s[0]["params"]["status"]

    def test_error_file_not_found(self):
        with pytest.raises(CatalogError, match="不存在"):
            load_catalog("/no/such/file.yaml")

    def test_error_bad_yaml(self):
        with pytest.raises(CatalogError, match="YAML"):
            parse_catalog("a: [unclosed")

    def test_base_url_trailing_slash_stripped(self):
        c = parse_catalog("version: 1\nbase_url: http://x.com/\napis: [{name: a, path: /x}]")
        assert c.base_url == "http://x.com"  # 尾斜杠自动清理

    @pytest.mark.parametrize("bad,why", [
        ("version: 1\nbase_url: example.com\napis: [{name: a, path: /x}]", "base_url 缺协议"),
        ("version: 1\nbase_url: http://x.com\napis: [{name: a, path: items}]", "path 缺斜杠"),
        ("version: 1\nbase_url: http://x.com\nauth: {type: basic}\napis: [{name: a, path: /x}]", "basic 缺账密"),
        ("version: 1\nbase_url: http://x.com\nauth: {type: bearer}\napis: [{name: a, path: /x}]", "bearer 缺 token"),
        ("version: 1\nbase_url: http://x.com\napis: []", "apis 为空"),
    ])
    def test_error_validation(self, bad, why):
        with pytest.raises(CatalogError):
            parse_catalog(bad)


class TestNewFields:
    """M1 新增：token_extra_headers/token_params/token_body_format、extra_headers、业务码、占位符替换。"""

    def test_auth_new_fields(self):
        from tools.catalog import AuthConfig
        a = AuthConfig(type="bearer", token_endpoint="/t",
                       token_params={"grant_type": "password"},
                       token_extra_headers={"Tenant-Id": "000000"},
                       token_body_format="form")
        assert a.token_params["grant_type"] == "password"
        assert a.token_extra_headers["Tenant-Id"] == "000000"
        assert a.token_body_format == "form"

    def test_extra_headers_merge(self):
        c = parse_catalog("""
base_url: https://x.com
defaults:
  extra_headers: {Tenant-Id: "000000", A: "1"}
apis:
  - name: a1
    path: /a
    extra_headers: {A: "2", B: "3"}
""")
        api = c.get_api("a1")
        assert c.defaults.merged_headers(api) == {"Tenant-Id": "000000", "A": "2", "B": "3"}

    def test_business_code_fields(self):
        c = parse_catalog("""
base_url: https://x.com
defaults: {business_code_path: code, business_code_ok: 200}
apis:
  - {name: a1, path: /a}
""")
        assert c.defaults.business_code_path == "code"
        assert str(c.defaults.business_code_ok) == "200"

    def test_placeholder_substitution(self):
        c = parse_catalog("""
base_url: https://x.com
auth:
  type: bearer
  token_endpoint: /login
  token_payload: {username: "{{username}}", password: "{{password}}", note: "{{missing_keeps_literal}}"}
  token_path: token
apis:
  - {name: a1, path: /a}
""", credentials={"username": "admin", "password": "pw123"})
        assert c.auth.token_payload["username"] == "admin"
        assert c.auth.token_payload["password"] == "pw123"
        # 无对应凭证的占位符原样保留
        assert c.auth.token_payload["note"] == "{{missing_keeps_literal}}"

    def test_placeholder_none(self):
        c = parse_catalog("""
base_url: https://x.com
apis:
  - {name: a1, path: /a}
""")
        assert c.get_api("a1").path == "/a"  # 不传 credentials 也正常
