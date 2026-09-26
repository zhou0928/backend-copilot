"""Backend Copilot agent strategy: 通用后端 Agent。

复用 plan-executor 的 Planner -> Executor -> Replanner 引擎（core/），
在其上增加两件事：
1. 接口目录注入：解析用户粘贴的 catalog YAML，把接口清单写进规划提示词，
   让规划器知道有哪些接口、参数怎么填；
2. 只读护栏：目录中未标记 write:true 的接口禁止写方法调用；readonly
   模式（默认开）下，计划步骤里出现写接口直接拦截并触发重规划。

与 plan-executor 的差异：参数更聚焦（无 files/context/并行），规划提示词
默认模板面向「查接口目录→调用→组织回答」。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator, Iterable, Iterator
from typing import Any

from dify_plugin.entities.parameters import I18nObject
from dify_plugin.entities.tool import (
    CommonParameterType,
    ToolDescription,
    ToolInvokeMessage,
)
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessageRole,
    SystemPromptMessage,
    TextPromptMessageContent,
    ToolPromptMessage,
    UserPromptMessage,
)
from dify_plugin.interfaces.agent import (
    AgentStrategy,
    AgentInvokeMessage,
    AgentToolIdentity,
    ToolEntity,
    ToolProviderType,
)

from core.budget import BudgetTimeout
from core.executor import Executor, ExecutionOutcome, StepExecutionError
from core.plan import InvalidPlanError, Plan
from core.planner import Planner
from core.replanner import BudgetExhaustedError, Replanner
from core.scratchpad import Scratchpad, validate_plan
from core.subagent_pool import LocalStepExecutor
from core.text import ThinkFilter, strip_think, truncate_middle
from core.tool_allowlist import filter_allowed_tools

from tools.catalog import ApiConfig, Catalog, CatalogError, parse_catalog

PROGRESS_LABEL = "progress"
ANSWER_VAR = "output"
_DEFAULT_EXECUTION_SECONDS = 600
_RESERVE_FINAL_ANSWER_SECONDS = 60
_LLM_RETRY_ATTEMPTS = 2
_LLM_BACKOFF_SECONDS = 0.5

PLANNING_PROMPT = """你是后端查询专家。用户的后端系统接口目录如下，请把用户问题拆解为对接口的调用步骤，最终用自然语言回答用户。

接口目录：
{tools}

要求：
1. 最多 {max_steps} 步；优先组合接口获取事实，最后一步用纯推理（tool 为 null）汇总回答。
2. 调用接口使用唯一工具 backend_api_call：input_mapping 里必须包含 api_name（接口名，取自目录）
   与 params（JSON 字符串，参数名和取值按目录说明构造；无参数传 "{{}}"）。
3. 目录中标了「写操作」的接口{write_rule}
4. 不确定参数含义时，宁可先用最小参数调用看返回结构，再在后续步骤补齐。
5. input_mapping 的值必须是字符串，用 "{{变量名}}" 引用前面步骤的 output_var 或初始变量 query。
6. 只输出 JSON，格式：
{"steps": [{"id": 1, "description": "...", "tool": "backend_api_call或null", "input_mapping": {"api_name": "ticket_search", "params": "{\\"keyword\\": \\"{{query}}\\"}"}, "output_var": "变量名"}]}"""

READONLY_RULE = "一律禁止调用：当前处于只读模式。"
WRITE_ALLOWED_RULE = "可以调用，但仅当用户问题明确要求写入时。"

FINAL_ANSWER_SYSTEM = """你是后端数据助手。基于接口返回的数据回答用户问题。
要求：用中文回答；数据用简洁的列表/表格呈现；标明数据来源接口；数据为空时如实说明。不要编造目录之外的信息。"""

FAST_PATH_SYSTEM = """你是后端数据助手。当前没有可用工具或接口目录为空。
基于已有信息用中文回答；无法从接口获取的数据要如实说明，不要编造。"""


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            getattr(part, "data", "") or "" for part in content if getattr(part, "type", None) == "text"
        )
    return str(content)


def _chunk_text(chunk: Any) -> str:
    delta = getattr(chunk, "delta", None)
    if delta is None:
        return ""
    return _flatten_content(getattr(getattr(delta, "message", None), "content", None))


def _coerce_history_message(raw: dict[str, Any]) -> Any:
    role = raw.get("role")
    if role == PromptMessageRole.ASSISTANT.value:
        return AssistantPromptMessage(**raw)
    if role == PromptMessageRole.SYSTEM.value:
        return SystemPromptMessage(**raw)
    if role == PromptMessageRole.TOOL.value:
        return ToolPromptMessage(**raw)
    return UserPromptMessage(**raw)


class _LLMCaller:
    """(system, user) -> str，带一次重试；.stream() 增量输出；usage 累计。"""

    def __init__(self, invoke: Any, stream: Any, usage: dict[str, Any]) -> None:
        self._invoke = invoke
        self._stream = stream
        self.usage = usage

    def __call__(self, system: str, user: str) -> str:
        last: Exception | None = None
        for attempt in range(_LLM_RETRY_ATTEMPTS):
            try:
                return self._invoke(system, user)
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < _LLM_RETRY_ATTEMPTS - 1:
                    time.sleep(_LLM_BACKOFF_SECONDS)
        assert last is not None
        raise last

    def stream(self, system: str, user: str) -> Iterator[str]:
        return self._stream(system, user)


class BackendCopilotAgentStrategy(AgentStrategy):
    def __init__(self, runtime, session) -> None:
        super().__init__(runtime, session)
        self._session = session
        self._catalog: Catalog | None = None

    # ---------------- 目录 ----------------

    def _load_catalog(self, catalog_yaml: str) -> Catalog | None:
        """解析用户粘贴的目录 YAML；失败不崩溃，返回 None 并记录原因。"""
        if not catalog_yaml or not catalog_yaml.strip():
            return None
        try:
            return parse_catalog(catalog_yaml)
        except CatalogError as e:
            raise ValueError(f"接口目录无效：{e}") from e

    def _readonly_guard(self, api_name: str, params_json: str) -> str | None:
        """只读护栏：返回拒绝原因（None=放行）。

        目录中未标记 write:true 的接口，若规划器试图以写方法调用则拦截；
        readonly 模式下所有 write:true 接口也拦截。
        """
        if self._catalog is None:
            return None
        api = self._catalog.get_api(api_name)
        if api is None:
            return None  # 未知接口交给工具层报错（会提示可用接口）
        if api.write and self._readonly:
            return f"接口 {api_name} 是写操作，当前 Agent 处于只读模式，已拦截"
        return None

    # ---------------- 主入口 ----------------

    def _invoke(self, parameters: dict[str, Any]) -> Generator[AgentInvokeMessage]:
        model_config = parameters["model"]
        catalog_yaml = parameters.get("catalog_yaml") or ""
        self._catalog = self._load_catalog(catalog_yaml)
        self._readonly = bool(parameters.get("readonly", True))
        mounted_tools = [
            t if isinstance(t, ToolEntity) else ToolEntity.model_validate(t)
            for t in (parameters.get("tools") or [])
        ]
        tools = filter_allowed_tools(mounted_tools, parameters.get("allowed_tools"))
        goal = str(parameters.get("query") or "")
        instruction = parameters.get("instruction") or ""
        max_steps = self._clamp(int(parameters.get("max_steps") or 20), 1, 50, 20)
        max_replan = self._clamp(int(parameters.get("max_replan") or 3), 0, 10, 3)
        budget_seconds = int(parameters.get("max_execution_seconds") or _DEFAULT_EXECUTION_SECONDS)
        deadline = time.monotonic() + budget_seconds if budget_seconds > 0 else None
        output_variable = (parameters.get("output_variable") or ANSWER_VAR).strip() or ANSWER_VAR
        history = list((model_config or {}).get("history_prompt_messages") or [])

        usage: dict[str, Any] = {"usage": None}
        usage_lock = threading.Lock()
        plan_llm = self._make_llm_caller(model_config, history=history, usage=usage, usage_lock=usage_lock)
        step_llm = self._make_llm_caller(model_config, usage=usage, usage_lock=usage_lock)
        planner_llm = BoundedCaller(plan_llm, deadline)
        scratchpad = Scratchpad(initial={"query": goal})

        # 无工具或无目录：直接回答（规划无意义）
        if not tools or self._catalog is None:
            answer = ""
            answer_filter = ThinkFilter()
            try:
                for piece in plan_llm.stream(FAST_PATH_SYSTEM, goal):
                    answer += answer_filter.feed(piece)
            except Exception as e:  # noqa: BLE001
                yield self.create_text_message(f"[后端副驾] 模型调用失败：{e}\n")
                yield self.create_variable_message(output_variable, "")
                return
            answer = strip_think(answer + answer_filter.flush())
            scratchpad.set(output_variable, answer)
            yield self.create_text_message(answer)
            yield self.create_variable_message(output_variable, answer)
            yield self.create_json_message({"execution_metadata": {"usage": usage.get("usage")}})
            return

        # --- Phase 1: 规划（目录摘要注入规划提示词） ---
        catalog_summary = json.dumps(self._catalog.list_api_summaries(), ensure_ascii=False, indent=1)
        write_rule = READONLY_RULE if self._readonly else WRITE_ALLOWED_RULE
        planning_prompt = PLANNING_PROMPT.replace("{tools}", catalog_summary).replace("{max_steps}", str(max_steps)).replace("{write_rule}", write_rule)
        planner = Planner(llm=planner_llm, planning_prompt=planning_prompt, max_steps=max_steps)
        initial_vars = {"query"}
        try:
            plan = self._plan_with_validation(planner, goal, tools, instruction, initial_vars, budget=2, deadline=deadline)
        except InvalidPlanError as e:
            yield self.create_text_message(f"[后端副驾] 规划失败：{e}\n")
            yield self.create_variable_message(output_variable, "")
            return
        except Exception as e:  # noqa: BLE001 - 模型不可用等
            yield self.create_text_message(f"[后端副驾] 模型调用失败（规划层）：{e}\n")
            yield self.create_variable_message(output_variable, "")
            return

        yield self.create_text_message(f"📋 执行计划：\n{self._render_plan(plan)}\n\n")

        # --- Phase 2+3: 执行 / 重规划循环 ---
        replanner = Replanner(llm=planner_llm, max_replan=max_replan, max_invalid_plan=2)
        replan_count = 0
        current_plan = plan
        start_index = 0
        answer_streamed = False

        while True:
            outcome = yield from self._execute_with_progress(
                step_llm, tools, current_plan, scratchpad, start_index,
                max_result_chars=8000, deadline=deadline,
                streamed=answer_streamed,
            )
            if outcome.timed_out:
                answer = self._digest(scratchpad, output_variable)
                yield self.create_text_message(f"\n\n[后端副驾] 执行时间预算（{budget_seconds} 秒）已用尽，以上为部分结果。\n")
                yield from self._emit_tail(answer, output_variable, usage)
                return
            if outcome.completed:
                break
            failed = outcome.failed_step
            assert failed is not None
            if deadline is not None and time.monotonic() >= deadline:
                answer = self._digest(scratchpad, output_variable)
                yield from self._emit_tail(answer, output_variable, usage)
                return
            try:
                new_plan = replanner.replan(
                    original=current_plan, failed_step=failed, error=outcome.error or "unknown",
                    scratchpad=scratchpad, remaining_goal=goal,
                )
                replan_count += 1
                start_index = 0
                current_plan = new_plan
                yield self.create_text_message(f"📋 第 {replan_count} 次重规划：\n{self._render_plan(new_plan)}\n\n")
            except (InvalidPlanError, BudgetExhaustedError, BudgetTimeout) as e:
                answer = self._digest(scratchpad, output_variable)
                yield self.create_text_message(f"\n[后端副驾] 重规划失败（{e}），以下为尽力回答：\n\n")
                yield from self._emit_tail(answer, output_variable, usage)
                return

        # 成功收尾：输出最终变量内容
        final_var = output_variable if scratchpad.has(output_variable) else current_plan.steps[-1].output_var
        final_text = strip_think(str(scratchpad.get(final_var, "")))
        yield self.create_text_message(final_text)
        yield from self._emit_tail(final_text, output_variable, usage)

    # ---------------- helpers ----------------

    def _plan_with_validation(self, planner, goal, tools, instruction, initial_vars, budget, deadline=None):
        last_error = None
        for attempt in range(budget + 1):
            if attempt and deadline is not None and time.monotonic() >= deadline:
                raise last_error  # type: ignore[misc]
            try:
                plan = planner.plan(goal, tools, instruction, sorted(initial_vars))
                validate_plan(plan, initial_vars)
                return plan
            except InvalidPlanError as e:
                last_error = e
        assert last_error is not None
        raise last_error

    def _catalog_tools(self) -> list[ToolEntity]:
        """backend_api_call 的合成 ToolEntity，供规划器感知参数形态。"""
        identity = AgentToolIdentity(name="backend_api_call", provider="backend-copilot", provider_type=ToolProviderType.PLUGIN)
        params = [
            ToolParameter(name="api_name", label=I18nObject(en_US="API name", zh_Hans="接口名"),
                          human_description=I18nObject(en_US="API name from catalog", zh_Hans="目录中的接口名"),
                          type=CommonParameterType.STRING, form=ToolParameter.ToolParameterForm.LLM, required=True,
                          llm_description="接口名，必须取自接口目录"),
            ToolParameter(name="params", label=I18nObject(en_US="Params JSON", zh_Hans="参数 JSON"),
                          human_description=I18nObject(en_US="Parameters as JSON string", zh_Hans="JSON 字符串形式的参数"),
                          type=CommonParameterType.STRING, form=ToolParameter.ToolParameterForm.LLM, required=False,
                          llm_description='JSON 字符串，如 {"keyword": "订单"}；无参数传 "{}"'),
        ]
        return [ToolEntity(identity=identity, parameters=params,
                           description=ToolDescription(human=I18nObject(en_US="backend api call", zh_Hans="后端接口调用"),
                                                       llm="调用接口目录中登记的后端接口。参数: api_name（必填）, params（JSON 字符串）"))]

    def _tool_invoker(self, name: str, params: dict[str, Any]) -> str:
        """执行一次 backend_api_call：护栏 → 会话工具调用 → 截断。"""
        api_name = str(params.get("api_name") or "")
        raw = params.get("params") or "{}"
        try:
            call_params = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError as e:
            raise StepExecutionError(f"params 不是合法 JSON：{e}") from e
        if (denied := self._readonly_guard(api_name, raw)) is not None:
            raise StepExecutionError(denied)  # 触发重规划
        tool = next((t for t in self._tools if t.identity.name == name), None)
        if tool is None:
            raise StepExecutionError(f"工具 {name} 未挂载")
        parameters = {**(tool.runtime_parameters or {}), **call_params,
                      "api_name": api_name}
        try:
            response = self._session.tool.invoke(
                provider_type=tool.provider_type, provider=tool.identity.provider or "",
                tool_name=name, parameters=parameters, credential_id=tool.credential_id,
            )
        except Exception as e:  # noqa: BLE001
            raise StepExecutionError(f"工具 {name} 调用失败：{e}") from e
        parts = [self._render_tool_message(m) for m in response]
        parts = [p for p in parts if p]
        if not parts:
            raise StepExecutionError(f"工具 {name} 返回空结果")
        return truncate_middle("\n".join(parts), 8000)

    @staticmethod
    def _render_tool_message(message: Any) -> str:
        msg_type = getattr(message, "type", None)
        payload = getattr(message, "message", None)
        if msg_type == ToolInvokeMessage.MessageType.TEXT:
            return str(getattr(payload, "text", "") or "")
        if msg_type == ToolInvokeMessage.MessageType.JSON:
            data = getattr(payload, "json_object", None)
            return json.dumps(data, ensure_ascii=False) if data is not None else ""
        return ""

    def _execute_with_progress(self, step_llm, tools, plan, scratchpad, start_index,
                               max_result_chars, deadline, streamed=False):
        self._tools = tools
        executor = Executor(
            pool=LocalStepExecutor(llm=step_llm, tool_invoker=self._tool_invoker),
            deadline=deadline,
        )
        return executor.run(plan, scratchpad, start_index)

    def _make_llm_caller(self, model_config, history=None, usage=None, usage_lock=None):
        history = [_coerce_history_message(m) for m in (history or [])]
        usage = usage if usage is not None else {"usage": None}
        lock = usage_lock or threading.Lock()

        def build(system: str, user: str):
            return [SystemPromptMessage(content=system), *history, UserPromptMessage(content=user)]

        def record(chunk_usage):
            if chunk_usage is not None:
                with lock:
                    self.increase_usage(usage, chunk_usage)

        def invoke(system: str, user: str) -> str:
            result = self._session.model.llm.invoke(model_config=model_config, prompt_messages=build(system, user), stream=False)
            text = _flatten_content(result.message.content)
            if text.strip():
                record(result.usage)
                return text
            parts = []
            for chunk in self._session.model.llm.invoke(model_config=model_config, prompt_messages=build(system, user), stream=True):
                record(getattr(getattr(chunk, "delta", None), "usage", None))
                parts.append(_chunk_text(chunk))
            return "".join(parts)

        def stream(system: str, user: str):
            for chunk in self._session.model.llm.invoke(model_config=model_config, prompt_messages=build(system, user), stream=True):
                record(getattr(getattr(chunk, "delta", None), "usage", None))
                piece = _chunk_text(chunk)
                if piece:
                    yield piece

        return _LLMCaller(invoke, stream, usage)

    def _digest(self, scratchpad, output_variable) -> str:
        """预算耗尽/重规划失败时的尽力回答：拼装 scratchpad 已有成果。"""
        state = scratchpad.variables()
        results = {k: v for k, v in state.items() if not k.endswith("_error") and k != "query"}
        if not results:
            return "（无可用结果）"
        return json.dumps(results, ensure_ascii=False, default=str)

    def _emit_tail(self, answer, output_variable, usage):
        yield self.create_variable_message(output_variable, answer)
        yield self.create_json_message({"execution_metadata": {"usage": usage.get("usage")}})

    @staticmethod
    def _render_plan(plan: Plan) -> str:
        lines = []
        for s in plan.steps:
            tool = s.tool or "（汇总回答）"
            lines.append(f"  步骤{s.id}: {s.description} [工具: {tool}] → {s.output_var}")
        return "\n".join(lines)

    @staticmethod
    def _clamp(value: int, lo: int, hi: int, default: int) -> int:
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value

