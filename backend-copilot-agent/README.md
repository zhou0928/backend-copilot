# Backend Copilot Agent

A Dify **Agent strategy plugin**: a plan → execute → replan multi-step pipeline that, together with the [backend-copilot](../backend-copilot) tools plugin, queries any registered backend API in natural language. Read-only guardrails by default.

> This plugin is the strategy only — it does not include the API catalog or HTTP tools. Install and configure the **backend-copilot** tools plugin first (catalog YAML, credentials, and the `http_request` tool all come from it). See its [README](../backend-copilot/README.md).
>
> 中文文档：[README.zh-CN.md](README.zh-CN.md)

```
User: "Of the tickets filed last week, which are still unassigned?"
  ↓
① Plan    LLM generates a step plan from the catalog (parallel steps supported)
        step1: ticket_search(status=submitted, time_range=last_week)
        step2: for each ticket, call ticket_detail to check the assignee field
  ↓
② Execute call http_request → backend APIs step by step; results stored in scratchpad
  ↓
③ Replan  on step failure/invalid params, adjust the plan automatically (budgeted, default max 3)
  ↓
④ Answer  natural-language summary written to the output variable
```

## How it works

The strategy is driven by the pipeline modules under `core/`:

| Module | Responsibility |
|---|---|
| `planner` | LLM generates a structured JSON plan; each step declares which tool to call and with what params |
| `executor` | Runs the plan, sequential or parallel scheduling, trips the circuit breaker on budget exhaustion |
| `replanner` | Re-plans from the scratchpad on step failure or invalid plans, with its own budget |
| `scratchpad` | Intermediate results between steps; later steps can reference earlier outputs |
| `budget` | Time budget (`run_with_budget`) and LLM call budget (`BoundedCaller`) |
| `tool_allowlist` | Filters available tools by allowlist |
| `subagent_pool` | Step-level executor abstraction, swappable implementations |

## Usage

### 1. Prerequisite: install & configure the backend-copilot tools plugin

Install `backend-copilot.difypkg`, then fill in the backend address, catalog YAML, and credentials on its authorization page. See the [backend-copilot README](../backend-copilot/README.md).

### 2. Install this plugin

Dify → Plugins → Install → Local file: `backend-copilot-agent.difypkg`. Or via GitHub: install from `zhou0928/backend-copilot`, pick the release.

### 3. Configure an Agent node in your workflow

Add an **Agent** node and pick the **Backend Copilot** strategy:

| Parameter | Required | Default | Description |
|---|---|---|---|
| Model | ✅ | — | An LLM that supports tool-call |
| Query | ✅ | — | User question, e.g. "How many open tickets this week?" |
| API catalog (YAML) | ✅ | — | Paste the catalog YAML (same content as in the tool credentials); templating supported |
| Backend tools | ✅ | — | Attach the `http_request` tool from backend-copilot |
| Read-only mode | | on | On: APIs marked `write: true` are always blocked; off: they may run only when the user explicitly asks |
| Allowed tools | | empty | Allowlist; empty = all attached tools |
| Instruction | | — | Answer style, constraints, e.g. "output as a table, amounts to 2 decimals" |
| Max plan steps | | 20 | Maximum steps in one plan |
| Max replans | | 3 | Maximum automatic replans |
| Execution budget (seconds) | | 600 | Timeout circuit breaker |
| Output variable | | `output` | Variable the final answer is written to, for downstream nodes |

The strategy enables **history-messages**: in Chatflows, multi-turn conversation context is carried in.

### 4. Ask

Just ask in natural language. The Agent reads the catalog, plans, calls, and summarizes on its own — no per-API node wiring needed.

## Read-only guardrails (three layers)

1. **Catalog layer**: APIs are read-only by default; write APIs must be explicitly marked `write: true` in the catalog YAML
2. **Credential layer**: the tools plugin's "Allow write" master switch defaults to `false`
3. **Strategy layer**: "Read-only mode" defaults to on, blocking all write APIs; when off, they run only on explicit user request

## Example catalogs

Reuse the three catalogs from the tools plugin's `examples/` (paste into the "API catalog" parameter):

- `bladex.catalog.yaml` — BladeX (tickets/workflow, 6 read-only APIs)
- `ruoyi.catalog.yaml` — RuoYi (users/departments/roles etc.)
- `demo.catalog.yaml` — JSONPlaceholder (public demo, no auth)

## FAQ

**Q: "Backend tools not found"?**
The Agent node's "Backend tools" parameter must have the `http_request` tool from backend-copilot attached, and that plugin's credentials (backend URL + catalog YAML) must be configured.

**Q: Planning keeps failing or steps are messy?**
① Make sure the model supports tool-call (the strategy requires the `tool-call&llm` scope); ② write clear `description` for each API — plan quality depends directly on catalog descriptions; ③ use "Instruction" to constrain the output format.

**Q: Budget exhausted mid-run?**
Defaults are 600s / 20 steps. For complex queries, raise the execution budget and step cap, or narrow "Allowed tools" to reduce wasted attempts.

**Q: Want to restrict the Agent to a few APIs?**
Two ways: ① set an allowlist in "Allowed tools"; ② trim the catalog YAML to only the needed APIs (recommended — smaller catalog, better planning).

**Q: Multi-turn context is lost?**
The strategy enables history-messages; make sure conversation variables are wired into the Agent node in your workflow.

## Local development

```bash
# core/ is shared with the tools plugin; tests live in the main package:
cd ../backend-copilot && uv sync && uv run python -m pytest -q

# Package
./package-plugin.sh backend-copilot-agent
```

## License

See LICENSE in the repository; privacy statement in PRIVACY.md.
