# Backend Copilot（后端副驾）

一套 Dify 插件，让 Agent 用**自然语言查询你的后端业务数据**：只需粘贴一份接口目录 YAML，无需写任何代码。

支持 BladeX、RuoYi、自研 Spring Boot 等任意 REST 后端。**默认只读护栏**——写接口必须双重确认才会执行。

```
用户："本周有多少未处理工单？"
  ↓
Agent 读取接口目录 → 规划步骤（查待办列表 → 按状态过滤 → 统计）
  ↓
调用 http_request 工具请求你的后端（自动带鉴权）
  ↓
汇总回答："本周未处理工单共 17 条，其中 3 条已超 48 小时未响应……"
```

## 插件组成

| 包 | 类型 | 说明 |
|---|---|---|
| `backend-copilot` (v0.0.3) | Tools | 通用 REST 连接器：`http_request` 调用器 + 接口目录 + 四种鉴权 + OpenAPI 导入 |
| `backend-copilot-agent` (v0.2.0) | Agent Strategy | 通用后端 Agent 策略：规划 → 执行 → 重规划 Pipeline，复用上面的工具 |

两个包均提供 `.difypkg` 文件，可离线安装到自托管 Dify（≥ 1.9.0）。

## 核心特性

- **声明式接口目录**：一份 YAML 描述你后端的所有接口（路径/参数/鉴权/分页），Agent 据此自动选择和调用
- **四种鉴权**：`none` / `bearer`（含 OAuth2 password 模式自动登录换 token、过期自动刷新）/ `basic` / `apikey`，支持 BladeX 的 Basic 头 + Tenant-Id + 验证码预取 + SM2 国密加密
- **只读护栏**：接口默认只读；写接口需目录标记 `write: true` **且** 凭证表单开启 `allow_write` 双重放行
- **上下文保护**：列表结果按 `max_rows` 自动截断并附总数，防止撑爆 LLM 上下文
- **业务信封识别**：`{code, success, data, msg}` 类返回 HTTP 200 但业务失败的场景，按配置识别为错误
- **OpenAPI 一键导入**：从 Swagger JSON/YAML 自动生成接口目录
- **Agent 策略自带 Pipeline**：计划（支持并行步骤）→ 执行 → 失败重规划（默认上限 3 次），带时间预算（默认 600s）熔断

## 快速开始

### 1. 安装插件

Dify → 插件 → 安装插件 → 本地文件，依次安装两个 `.difypkg`：

- `backend-copilot.difypkg`
- `backend-copilot-agent.difypkg`

### 2. 编写接口目录

复制 `examples/demo.catalog.yaml` 为起点，或直接选用现成示例：

```yaml
version: 1
base_url: http://host.docker.internal:9998   # 容器内访问宿主机服务用 host.docker.internal
auth:
  type: bearer
  token_endpoint: /blade-auth/oauth/token    # 留空则不需要登录换 token
  token_params:
    grant_type: password
    scope: all
  token_body_format: form
  token_payload:
    username: "{{username}}"                 # 占位符在安装凭证表单时替换
    password: "{{password}}"
  token_extra_headers:
    Authorization: "Basic c2FiZXI6c2FiZXJfc2VjcmV0"
    Tenant-Id: "000000"
  token_path: data.access_token
defaults:
  timeout: 30
  max_rows: 50
  extra_headers:                             # 每个业务请求都带的头
    Tenant-Id: "000000"
    Blade-Requested-With: BladeHttpRequest
  business_code_path: code                   # 业务码字段（HTTP 200 但业务失败的场景）
  business_code_ok: 200
apis:
  - name: ticket_search                      # Agent 通过此名称选用接口
    description: "分页查询工单列表，支持按关键字与状态过滤"   # 写清楚，Agent 靠它理解接口用途
    method: GET
    path: /blade-ticket/ticket/list
    params:
      current: { type: int, required: false, description: "页码，从 1 开始" }
      size: { type: int, required: false, description: "每页条数" }
      keyword: { type: string, required: false, description: "标题关键字" }
    pagination: { page_param: current, size_param: size, total_path: data.total }
    result_path: data.records                # 从响应中提取数据的 JSON 路径

  # 写接口示例：必须显式 write: true 才可能被 Agent 调用
  - name: ticket_create
    description: "创建新工单"
    method: POST
    path: /blade-ticket/ticket/submit
    write: true
    params:
      title: { type: string, required: true, description: "工单标题" }
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `base_url` | ✅ | 后端地址；Dify 容器访问宿主机服务用 `host.docker.internal` |
| `auth.type` | ✅ | `none` / `bearer` / `basic` / `apikey` |
| `auth.token_endpoint` | | OAuth2 登录端点，配置后自动换 token 并缓存刷新 |
| `auth.token_path` | | 从登录响应提取 token 的路径，如 `data.access_token` |
| `defaults.max_rows` | | 列表截断上限（默认 50） |
| `defaults.extra_headers` | | 每个请求附加的头（如租户 ID） |
| `defaults.business_code_path/ok` | | 业务信封校验 |
| `apis[].name` | ✅ | 接口标识，Agent 按名选用 |
| `apis[].description` | ✅ | **写清楚用途，Agent 靠它决定何时调用** |
| `apis[].params` | | 参数名 → 类型/必填/描述 |
| `apis[].pagination` | | 分页参数映射 |
| `apis[].result_path` | | 响应数据提取路径 |
| `apis[].write` | | 写操作标记，缺省为只读 |

### 3. 配置工具凭证

Dify → 插件 → Backend Copilot → 授权，填入：

| 字段 | 说明 |
|---|---|
| 后端服务地址 | 即目录里的 `base_url` |
| 接口目录（YAML） | 粘贴上一步编写的完整 YAML |
| 用户名 / 密码 / Token / API Key | 替换目录中的 `{{username}}` 等占位符 |
| 允许写操作 | 默认 `false`；开启后目录中 `write: true` 的接口才可用 |

### 4. 使用 Agent 策略

在工作流（Chatflow / Agent 节点）中选择 **Backend Copilot** 策略：

| 参数 | 默认 | 说明 |
|---|---|---|
| 模型 | 必填 | 需支持 tool-call 的 LLM |
| 查询 | 必填 | 用户问题，如"本周有多少未处理工单？" |
| 接口目录（YAML） | 必填 | 与工具凭证中相同的目录内容 |
| 后端工具 | 必填 | 挂载本插件的 `http_request` 工具 |
| 只读模式 | 开 | 开启后写接口一律拦截；关闭后仅在用户明确要求写入时调用 |
| 补充指令 | | 回答风格、约束等额外要求 |
| 计划步骤上限 | 20 | 单次计划的最大步骤数 |
| 重规划上限 | 3 | 失败自动重规划的次数上限 |
| 执行时间预算（秒） | 600 | 超时熔断 |

配置完成后直接用自然语言提问即可。

## 三份示例目录

| 示例 | 对接后端 | 预置接口 |
|---|---|---|
| `examples/bladex.catalog.yaml` | BladeX（基于 oneLineCar 真实接口） | 工单搜索/详情/评论、流程待办/已发/统计（6 个只读） |
| `examples/ruoyi.catalog.yaml` | RuoYi | 用户列表/详情、部门树、角色列表、登录日志、服务器监控 |
| `examples/demo.catalog.yaml` | JSONPlaceholder（公开演示，无需鉴权） | 文章列表/详情 |

## 常见问题

**Q: 插件容器访问不到我的后端？**
Dify 插件运行在容器里，`localhost` 指容器自身。访问宿主机服务请用 `http://host.docker.internal:<端口>`（Docker/OrbStack 通用）。

**Q: 安装时提示 `plugin_unique_identifier is not valid`？**
`manifest.yaml` 的 `author` 字段需与发布者 ID 一致。

**Q: Agent 一直不调用接口？**
检查接口的 `description` 是否写清楚了用途——Agent 完全依赖描述来匹配用户问题。另外确认目录 YAML 与凭证表单中的一致。

**Q: 写接口被拦截？**
三重确认：① 目录中该接口标记 `write: true`；② 凭证表单"允许写操作"设为 `true`；③ 策略参数"只读模式"关闭（或用户明确要求写入）。

**Q: 返回 200 但 Agent 说接口报错？**
这是业务信封失败被正确识别。检查 `business_code_path` / `business_code_ok` 是否与你的后端约定一致。

**Q: 登录换 token 失败？**
BladeX 类网关要求登录请求带特定头（Basic、Tenant-Id），确认 `token_extra_headers` 配置完整；`grant_type`/`scope` 类参数需放 `token_params`（走 URL query 而非 body）。

## 本地开发

```bash
# 安装依赖并跑测试（74 例）
uv sync && uv run python -m pytest -q

# 打包（自动处理 .venv 排除）
./package-plugin.sh backend-copilot
./package-plugin.sh backend-copilot-agent
```

> 注意：`dify plugin package` 不会排除 `.venv`，会导致超 50MB 限制，请使用仓库自带的 `package-plugin.sh`。

## License

见仓库 LICENSE；隐私说明见 PRIVACY.md。
