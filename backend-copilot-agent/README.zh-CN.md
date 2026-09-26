# Backend Copilot Agent（后端副驾驶 Agent）

Dify **Agent 策略插件**：规划 → 执行 → 按需重规划的多步 Pipeline，配合 [backend-copilot](../backend-copilot) 工具插件，用自然语言查询任意已登记的后端接口。默认只读护栏。

> 本插件只是策略，不含接口目录和 HTTP 工具——需要先安装并配置 **backend-copilot** 工具插件（接口目录 YAML、凭证、http_request 工具都由它提供）。目录编写与凭证配置见其 [README](../backend-copilot/README.md)。

```
用户问题："上周提交的工单里，哪些还没人处理？"
  ↓
① 规划   LLM 根据接口目录生成步骤计划（可含并行步骤）
        step1: ticket_search(status=submitted, time_range=last_week)
        step2: 对每条工单调用 ticket_detail 检查处理人字段
  ↓
② 执行   逐步调用 http_request → 后端接口，中间结果存入 scratchpad
  ↓
③ 重规划 步骤失败/参数无效时自动调整计划（预算内，默认最多 3 次）
  ↓
④ 汇总   输出自然语言回答，写入 output 变量
```

## 工作原理

策略内部由 `core/` 下的 Pipeline 模块驱动：

| 模块 | 职责 |
|---|---|
| `planner` | LLM 生成结构化 JSON 执行计划，每步声明调用哪个工具、传什么参数 |
| `executor` | 按计划执行步骤，支持串行与并行两种调度，超预算自动熔断 |
| `replanner` | 步骤失败、计划无效时基于 scratchpad 重新规划，带独立预算 |
| `scratchpad` | 步骤间中间结果暂存，后续步骤可引用前面步骤的输出 |
| `budget` | 时间预算（`run_with_budget`）与 LLM 调用预算（`BoundedCaller`） |
| `tool_allowlist` | 按白名单过滤可用工具 |
| `subagent_pool` | 步骤级执行器抽象，便于替换实现 |

## 使用步骤

### 1. 前置：安装并配置 backend-copilot 工具插件

安装 `backend-copilot.difypkg`，在插件授权页填好后端地址、接口目录 YAML、账密。详见 [backend-copilot README](../backend-copilot/README.md)。

### 2. 安装本插件

Dify → 插件 → 安装插件 → 本地文件，安装 `backend-copilot-agent.difypkg`。

### 3. 在工作流中配置 Agent 节点

工作流编排里添加 **Agent** 节点，策略选择 **后端副驾（backend-copilot）**：

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| 模型 | ✅ | — | 需支持 tool-call 的 LLM |
| 查询 | ✅ | — | 用户问题，如"本周有多少未处理工单？" |
| 接口目录（YAML） | ✅ | — | 粘贴目录 YAML（与工具插件凭证中相同的内容），支持模板 |
| 后端工具 | ✅ | — | 挂载 backend-copilot 插件的 `http_request` 工具 |
| 只读模式 | | 开 | 开：目录中 `write: true` 的接口一律拦截；关：仅在用户明确要求写入时调用 |
| 允许的工具 | | 空 | 白名单，留空 = 全部已挂载工具 |
| 补充指令 | | — | 回答风格、口径约束等，如"用表格输出，金额保留两位小数" |
| 计划步骤上限 | | 20 | 单次计划的最大步骤数 |
| 重规划上限 | | 3 | 失败自动重规划次数上限 |
| 执行时间预算（秒） | | 600 | 超时熔断 |
| 输出变量 | | `output` | 最终回答写入的变量名，供下游节点引用 |

策略启用 **history-messages** 特性：在 Chatflow 中可携带多轮对话上下文。

### 4. 提问

直接用自然语言提问。Agent 会自行查目录、规划、调用、汇总，无需为每个接口单独编排节点。

## 只读护栏（三层防线）

1. **目录层**：接口默认只读，写接口必须在目录 YAML 中显式标记 `write: true`
2. **凭证层**：工具插件凭证表单的"允许写操作"总开关默认 `false`
3. **策略层**：本策略"只读模式"默认开启，写接口一律拦截；关闭后也仅在用户明确要求写入时才会调用

## 示例目录

直接复用工具插件 `examples/` 下的三份目录（内容粘贴到"接口目录"参数即可）：

- `bladex.catalog.yaml` — BladeX（工单/流程，6 个只读接口）
- `ruoyi.catalog.yaml` — RuoYi（用户/部门/角色等）
- `demo.catalog.yaml` — JSONPlaceholder（公开演示，无需鉴权）

## 常见问题

**Q: 提示找不到后端工具？**
Agent 节点的"后端工具"参数必须挂载 backend-copilot 插件的 `http_request` 工具，且该插件的凭证（后端地址 + 目录 YAML）已配置。

**Q: 规划总是失败或步骤混乱？**
① 确认模型支持 tool-call（策略要求 `tool-call&llm` 范围）；② 接口目录中每个接口的 `description` 要写清用途，规划质量直接取决于目录描述质量；③ 可用"补充指令"约束输出格式。

**Q: 执行中途报预算耗尽？**
默认 600 秒 / 20 步。复杂查询可调大"执行时间预算"和"计划步骤上限"，或用"允许的工具"收窄工具范围减少无效尝试。

**Q: 想限制 Agent 只用某几个接口？**
两种方式：① 策略参数"允许的工具"填白名单；② 直接精简目录 YAML，只保留需要的接口（推荐，目录越小规划越准）。

**Q: 多轮对话记不住上文？**
策略已启用 history-messages；确认工作流中把会话变量正确接入 Agent 节点。

## 本地开发

```bash
# 核心模块与工具插件共享（core/ 目录内容一致），测试在主包：
cd ../backend-copilot && uv sync && uv run python -m pytest -q

# 打包
./package-plugin.sh backend-copilot-agent
```

## License

见仓库 LICENSE；隐私说明见 PRIVACY.md。
