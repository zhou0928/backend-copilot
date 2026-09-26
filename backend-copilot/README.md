# Backend Copilot

A set of Dify plugins that lets an Agent query **your backend business data in natural language**: paste one API catalog YAML — no code needed.

Works with BladeX, RuoYi, custom Spring Boot, or any REST backend. **Read-only guardrails by default** — write APIs require double opt-in.

```
User: "How many open tickets this week?"
  ↓
Agent reads the catalog → plans steps (list todos → filter by status → count)
  ↓
Calls the http_request tool against your backend (auth handled automatically)
  ↓
Answers: "17 open tickets this week, 3 of them unresponded for over 48h..."
```

> 中文文档：[README.zh-CN.md](README.zh-CN.md)

## Plugin composition

| Package | Type | Description |
|---|---|---|
| `backend-copilot` (v0.0.3) | Tools | Universal REST connector: `http_request` invoker + API catalog + 4 auth types + OpenAPI import |
| `backend-copilot-agent` (v0.2.0) | Agent Strategy | Universal backend agent strategy: plan → execute → replan pipeline, reusing the tools above |

Both ship as `.difypkg` files, installable offline into self-hosted Dify (≥ 1.9.0).

## Key features

- **Declarative API catalog**: one YAML file describing all your backend APIs (path/params/auth/pagination); the Agent picks and calls them automatically
- **Four auth types**: `none` / `bearer` (incl. OAuth2 password grant with auto login & token refresh) / `basic` / `apikey`; supports BladeX Basic header + Tenant-Id + captcha pre-fetch + SM2 encryption
- **Read-only guardrails**: APIs are read-only by default; a write API requires both `write: true` in the catalog **and** `allow_write` in credentials
- **Context protection**: list results auto-truncated at `max_rows` (default 50) with total count, so the LLM context never blows up
- **Business envelope detection**: `{code, success, data, msg}` responses that return HTTP 200 but fail business-wise are recognized as errors
- **OpenAPI one-click import**: generate a catalog automatically from Swagger JSON/YAML
- **Strategy pipeline built in**: plan (parallel steps supported) → execute → auto replan on failure (default cap 3), with an execution time budget (default 600s)

## Quick start

### 1. Install plugins

Dify → Plugins → Install → Local file, install both:

- `backend-copilot.difypkg`
- `backend-copilot-agent.difypkg`

Or via GitHub: install from `zhou0928/backend-copilot`, pick the release.

### 2. Write an API catalog

Start from `examples/demo.catalog.yaml`, or pick a ready-made example. Full annotated example:

```yaml
version: 1
base_url: http://host.docker.internal:9998   # from the plugin container, use host.docker.internal for host services
auth:
  type: bearer
  token_endpoint: /blade-auth/oauth/token    # omit if no login-for-token is needed
  token_params:
    grant_type: password
    scope: all
  token_body_format: form
  token_payload:
    username: "{{username}}"                 # placeholders replaced from the credential form
    password: "{{password}}"
  token_extra_headers:
    Authorization: "Basic c2FiZXI6c2FiZXJfc2VjcmV0"
    Tenant-Id: "000000"
  token_path: data.access_token
defaults:
  timeout: 30
  max_rows: 50
  extra_headers:                             # headers sent with every request
    Tenant-Id: "000000"
    Blade-Requested-With: BladeHttpRequest
  business_code_path: code                   # business envelope field (HTTP 200 but business failure)
  business_code_ok: 200
apis:
  - name: ticket_search                      # the Agent selects APIs by this name
    description: "Page through tickets, filter by keyword and status"  # be specific — the Agent relies on this
    method: GET
    path: /blade-ticket/ticket/list
    params:
      current: { type: int, required: false, description: "Page number, starting at 1" }
      size: { type: int, required: false, description: "Page size" }
      keyword: { type: string, required: false, description: "Title keyword" }
    pagination: { page_param: current, size_param: size, total_path: data.total }
    result_path: data.records                # JSON path to extract data from the response

  # Write API example: must be explicitly marked write: true to be callable
  - name: ticket_create
    description: "Create a new ticket"
    method: POST
    path: /blade-ticket/ticket/submit
    write: true
    params:
      title: { type: string, required: true, description: "Ticket title" }
```

Field reference:

| Field | Required | Description |
|---|---|---|
| `base_url` | ✅ | Backend address; use `host.docker.internal` for host services from the Dify container |
| `auth.type` | ✅ | `none` / `bearer` / `basic` / `apikey` |
| `auth.token_endpoint` | | OAuth2 login endpoint; when set, tokens are fetched and refreshed automatically |
| `auth.token_path` | | JSON path to the token in the login response, e.g. `data.access_token` |
| `defaults.max_rows` | | List truncation limit (default 50) |
| `defaults.extra_headers` | | Headers attached to every request (e.g. tenant id) |
| `defaults.business_code_path/ok` | | Business envelope validation |
| `apis[].name` | ✅ | API identifier used by the Agent |
| `apis[].description` | ✅ | **Describe the purpose clearly — the Agent matches user questions against it** |
| `apis[].params` | | Param name → type/required/description |
| `apis[].pagination` | | Pagination parameter mapping |
| `apis[].result_path` | | JSON path to extract data from the response |
| `apis[].write` | | Write flag; absent means read-only |

### 3. Configure tool credentials

Dify → Plugins → Backend Copilot → Authorize:

| Field | Description |
|---|---|
| Backend Base URL | Same as `base_url` in the catalog |
| API Catalog (YAML) | Paste the full catalog YAML from the previous step |
| Username / Password / Token / API Key | Replaces `{{username}}` etc. placeholders in the catalog |
| Allow write | Default `false`; only when enabled can APIs marked `write: true` run |

### 4. Use the Agent strategy

In a workflow (Chatflow / Agent node) choose the **Backend Copilot** strategy:

| Parameter | Default | Description |
|---|---|---|
| Model | required | An LLM that supports tool-call |
| Query | required | User question, e.g. "How many open tickets this week?" |
| API catalog (YAML) | required | Same catalog content as in the tool credentials |
| Backend tools | required | Attach the `http_request` tool from this plugin |
| Read-only mode | on | On: write APIs are always blocked; off: they may run only when the user explicitly asks |
| Instruction | | Extra requirements such as answer style or constraints |
| Max plan steps | 20 | Maximum steps in one plan |
| Max replans | 3 | Maximum automatic replans on failure |
| Execution budget (seconds) | 600 | Timeout circuit breaker |

Then just ask questions in natural language.

## Example catalogs

| Example | Backend | Predefined APIs |
|---|---|---|
| `examples/bladex.catalog.yaml` | BladeX (real oneLineCar APIs) | Ticket search/detail/comments, workflow todo/sent/stats (6 read-only) |
| `examples/ruoyi.catalog.yaml` | RuoYi | User list/detail, dept tree, roles, login logs, server monitor |
| `examples/demo.catalog.yaml` | JSONPlaceholder (public demo, no auth) | Post list/detail |

## FAQ

**Q: The plugin container can't reach my backend?**
Plugins run inside a container where `localhost` is the container itself. Use `http://host.docker.internal:<port>` to reach host services (works on Docker/OrbStack).

**Q: Install fails with `plugin_unique_identifier is not valid`?**
The `author` field in `manifest.yaml` must match the publisher ID.

**Q: The Agent never calls any API?**
Check each API's `description` — the Agent relies entirely on it to match user questions. Also verify the catalog YAML matches the one in credentials.

**Q: Write APIs are blocked?**
Three checks: ① the API is marked `write: true` in the catalog; ② credential form "Allow write" is `true`; ③ the strategy's "Read-only mode" is off (or the user explicitly asked for a write).

**Q: HTTP 200 but the Agent reports an error?**
That's the business envelope failure being correctly detected. Verify `business_code_path` / `business_code_ok` match your backend's convention.

**Q: Login-for-token fails?**
Gateways like BladeX require specific headers on the login request (Basic, Tenant-Id) — check `token_extra_headers`. Params like `grant_type`/`scope` belong in `token_params` (sent as URL query, not body).

## Local development

```bash
# Install deps and run tests (74 cases)
uv sync && uv run python -m pytest -q

# Package (handles .venv exclusion automatically)
./package-plugin.sh backend-copilot
./package-plugin.sh backend-copilot-agent
```

> Note: `dify plugin package` does not exclude `.venv`, which busts the 50MB limit — use the bundled `package-plugin.sh`.

## License

See LICENSE in the repository; privacy statement in PRIVACY.md.
