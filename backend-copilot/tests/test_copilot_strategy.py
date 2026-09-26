"""backend-copilot 策略层测试：目录注入、只读护栏、提示词渲染。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import yaml

from agent_strategies.copilot import (
    PLANNING_PROMPT,
    READONLY_RULE,
    WRITE_ALLOWED_RULE,
    BackendCopilotAgentStrategy,
)
from tools.catalog import parse_catalog


@pytest.fixture()
def catalog():
    return parse_catalog(Path(__file__).parent.parent.joinpath("examples", "bladex.catalog.yaml").read_text(encoding="utf-8"))


class GuardProbe(BackendCopilotAgentStrategy):
    """绕过 SDK 构造器，只测护栏纯逻辑。"""

    def __init__(self, cat, readonly=True):  # noqa: D107
        self._catalog = cat
        self._readonly = readonly


class TestCatalogInjection:
    def test_summary_contains_all_apis(self, catalog):
        summaries = json.dumps(catalog.list_api_summaries(), ensure_ascii=False)
        for name in ("ticket_search", "ticket_detail", "workflow_todo"):
            assert name in summaries

    def test_planning_prompt_renders(self, catalog):
        prompt = (
            PLANNING_PROMPT
            .replace("{tools}", json.dumps(catalog.list_api_summaries(), ensure_ascii=False))
            .replace("{max_steps}", "20")
            .replace("{write_rule}", READONLY_RULE)
        )
        assert "ticket_search" in prompt
        assert "20" in prompt
        assert "只读模式" in prompt

    def test_write_rule_variants(self):
        assert "禁止" in READONLY_RULE
        assert "明确要求写入" in WRITE_ALLOWED_RULE


class TestReadonlyGuard:
    def test_readonly_blocks_write_api(self, catalog):
        cat = parse_catalog(catalog.model_dump_json() if hasattr(catalog, "model_dump_json") else json.dumps(catalog.model_dump()))
        # 把第一个接口改为 write
        dump = cat.model_dump()
        dump["apis"][0]["write"] = True
        cat_w = parse_catalog(dump)
        probe = GuardProbe(cat_w, readonly=True)
        denied = probe._readonly_guard(dump["apis"][0]["name"], "{}")
        assert denied is not None and "只读模式" in denied

    def test_write_allowed_when_not_readonly(self, catalog):
        dump = catalog.model_dump()
        dump["apis"][0]["write"] = True
        cat_w = parse_catalog(dump)
        probe = GuardProbe(cat_w, readonly=False)
        assert probe._readonly_guard(dump["apis"][0]["name"], "{}") is None

    def test_read_api_passes(self, catalog):
        probe = GuardProbe(catalog, readonly=True)
        assert probe._readonly_guard("ticket_search", "{}") is None

    def test_unknown_api_delegates_to_tool_layer(self, catalog):
        probe = GuardProbe(catalog, readonly=True)
        assert probe._readonly_guard("no_such_api", "{}") is None


class TestCatalogParsingErrors:
    def test_invalid_yaml_raises_valueerror(self):
        from agent_strategies.copilot import BackendCopilotAgentStrategy as S

        class P(S):
            def __init__(self):
                pass

        with pytest.raises(ValueError, match="接口目录无效"):
            P()._load_catalog("base_url: not-a-url\napis: []")
