# Agent Workbench 配置管理基线

本文件记录正式项目当前采用的配置边界：

- `config.default.toml`：可提交、无真实密钥的默认值；
- `config.test.toml`：测试环境深度合并覆盖，专门开启确定性 failpoint；
- `config.production.toml`：无密钥的 production 合同覆盖，缺少部署注入时
  必须失败关闭；
- `config/ownership.yaml`：237 个配置叶子字段的唯一 owner 与生命周期登记；
- `.env.example`：本地开发需要注入的 DSN、模型 ID 和密钥名称；
- `src/agent_workbench/bootstrap/settings.py`：Pydantic Settings 类型、来源优先级、脱敏快照和跨域不变量；
- `tests/config/test_settings.py`：配置契约测试；
- `pyproject.toml` 与 `uv.lock`：运行时和测试依赖的唯一声明与解析结果。

正式项目锁定 Python `>=3.12,<3.13`。
当前架构基线为 `1.3`，配置 schema 为 `1.2`（`1.1` → `1.2` 的原因是模型
Provider 从 Anthropic 换成 DeepSeek，并新增 `model.base_url`）；两者是不同
版本轴。

配置字段对应的代码所有者、工作包与集成测试见
[代码实施计划 v1.0](./implementation-plan.md)。

正式代码库映射为：

```text
config/
├── config.default.toml
├── config.test.toml
├── config.production.toml
└── ownership.yaml
src/agent_workbench/bootstrap/settings.py
.env.example
tests/config/test_settings.py
tests/architecture/test_config_ownership.py
```

## 1. 加载规则

调用方只使用 `load_settings()`，不要在业务模块中直接调用
`Settings()`，也不要到处读取 `os.environ`。

优先级从高到低固定为：

```text
测试 init override
> 进程环境变量
> mounted secret files
> .env（仅 development/test）
> AW_CONFIG_FILE 指定的 TOML overlay
> config.default.toml
```

嵌套环境变量使用双下划线：

```bash
AW_COORDINATION__LEASE_DURATION_SECONDS=120
AW_RUNTIME__MAX_STEPS=16
AW_RAG__RETRIEVAL__RERANK_TOP_K=10
```

`AW_CONFIG_FILE` 在 `load_settings()` 调用时解析，而不是在 Python 模块
import 时解析。overlay 应使用绝对路径；默认文件先加载，overlay 通过
`deep_merge=True` 覆盖，环境变量最终胜出。

`agent-config-check` 还提供三个仓库内命名 profile：

```text
development → 只加载 config.default.toml
test        → 再叠加 config.test.toml
production  → 再叠加 config.production.toml
```

命名 profile 与任意 `--config` overlay 互斥。该命令只验证配置合同、版本、
脱敏和跨域不变量，不会启动模型、数据库、向量库或任何尚未实现的 Adapter。
production profile 故意不提交 DSN、API key、真实模型 ID 和 revision；未由
部署环境或 mounted secret 完整注入时，检查必须失败。

生产环境不读取 `.env`。生产密钥应由部署系统作为环境变量或 mounted
secret 注入；TOML、Git、事件、日志和 trace 中都不能出现真实密钥。
加载器会直接拒绝 TOML 中的 `[secrets]` 表以及三个 PostgreSQL DSN 字段，
避免“文档说不提交密钥，实际配置仍然接受密钥”的落差。

若使用 plain mounted-secret 目录，文件名沿用相同前缀和双下划线，例如：

```text
/run/secrets/AW_DATABASE__DSN
/run/secrets/AW_DATABASE__GUARD_DSN
/run/secrets/AW_DATABASE__LISTEN_DSN
/run/secrets/AW_SECRETS__DEEPSEEK_API_KEY
```

每个文件只保存对应值，通过 `AW_SECRETS_DIR=/run/secrets` 选择目录。

密钥来源不采用“谁优先谁静默获胜”。仍保留环境变量高于 mounted secret
的通用顺序，但对全部 `SecretStr` 和三个 DSN 执行冲突检测：

```text
只出现一个来源               → 接受
process env 与文件值相同      → 接受，但只报告字段名和来源
process env 与文件值不同      → 拒绝启动，不记录值或摘要
```

这样既不会让遗留环境变量悄悄覆盖轮换后的 secret 文件，也不会简单反转
优先级后让 stale secret 文件成为新的静默赢家。

为保证比较的是最终叶子字段，`AW_DATABASE={...}`、`AW_SECRETS={...}` 这类
父级 JSON 环境变量一律拒绝；所有嵌套设置只接受
`AW_SECTION__FIELD=value`。环境变量名按大小写归一后若重复也拒绝启动，
避免预检值与 Pydantic 最终取值不一致。mounted-secret 目录同样只允许上文
列出的叶子文件名；`AW_DATABASE`、`AW_SECRETS` 或其他 `AW_*` 文件会被
拒绝，不能借 JSON 文件绕过逐字段检查。development/test 的 `.env` 在
构造 Settings source 前执行同一套叶子键和大小写重复预检。

### 1.1 pydantic-settings 安全下限

`pyproject.toml` 强制 `pydantic-settings>=2.14.2,<3`。这是因为
[GHSA-4xgf-cpjx-pc3j / CVE-2026-58203](https://github.com/pydantic/pydantic-settings/security/advisories/GHSA-4xgf-cpjx-pc3j)
影响 `>=2.12.0,<2.14.2`：当显式启用
`secrets_nested_subdir=True` 时，旧版可能跟随越出 `secrets_dir` 的目录
symlink，并绕过目录大小限制。

本项目采用扁平 `AW_DATABASE__DSN` 文件名，在 `SettingsConfigDict` 中显式
锁定：

```text
secrets_nested_subdir = false
secrets_dir_max_size = 1 MiB
```

加载器还要求所读取的扁平 secret 文件解析后仍位于 `secrets_dir` 内，并
限制单文件大小。Kubernetes 指向目录内部的 projected-secret symlink 可以
使用；越出目录的链接会被拒绝。完整项目仍应由 `uv.lock` 或等价 lockfile
固定实际解析版本，而不是只依赖版本范围。

依赖声明和 lockfile 仍不是运行时证据。进程启动时必须通过
`importlib.metadata.version("pydantic-settings")` 读取**实际安装版本**，
并在初始化任何 secret source 之前校验其满足
`>=2.14.2,<3`；包缺失、版本无法解析或落在区间外都必须拒绝启动。启动
日志只能记录包名和版本号，不能顺带输出 Settings 或 secret source 内容。

### 1.2 环境与部署范围是两条独立轴

`app.environment` 表示运行语义（`development/test/production`），新增的
`app.deployment_scope` 表示信任边界，只允许：

```text
local   单机开发或测试环境；Qdrant 没有暴露到远程/共享网络
remote  任意共享、跨主机、staging 或 production 部署
```

二者不能互相猜测，启动时固定执行以下交叉校验：

```text
app.environment == "production"  → app.deployment_scope 必须为 "remote"
app.deployment_scope == "remote" → qdrant.url 必须为 HTTPS
                                  → qdrant.api_key_required 必须为 true
                                  → Qdrant API key 必须已经注入
```

因此，不能用 `environment=development` 给远程 Qdrant 绕过鉴权。`local`
只用于未暴露的本机/单机 Compose profile；只要 Qdrant 位于远程或共享
环境，就必须显式选择 `remote`，无论环境名是否为 production。为了防止
误标，`local` profile 的 Qdrant host 还被限制为
`localhost/127.0.0.1/::1/qdrant`。

`qdrant.url`、`artifact_store.endpoint` 和
`observability.otel_exporter_endpoint` 会出现在脱敏后的诊断配置中，因此
只接受普通 HTTP(S) service URL，禁止 userinfo、query string 和 fragment；
API key、token 与 exporter header 必须走独立的 `SecretStr` 字段。Pydantic
同时启用 `hide_input_in_errors`，使被拒绝的原始 URL/DSN/secret 不会被
校验错误再次带入启动日志。

## 2. 为什么需要三个 PostgreSQL DSN

| 配置 | 用途 | 连接规则 |
|---|---|---|
| `database.dsn` | 普通短事务、Repository、checkpoint 短事务 | 可以直连，也可按部署评估 session/transaction pool |
| `database.guard_dsn` | task-scoped advisory lock | 必须直连或 session pooling；取得锁后 pin 同一物理连接直到 Task 退出 |
| `database.listen_dsn` | `LISTEN/NOTIFY` 唤醒 | 每进程一条专用 session；禁止 transaction pooling |

guard 连接断开意味着 session lock 已经丢失。旧 Worker 必须立即取消
Graph 执行，不能自动重连并沿用旧 `lease_epoch` 继续写入。重新执行只能
回到 Task Registry，重新 claim、取得新 epoch 和 advisory lock。

每个运行中 Task 占一条 guard 连接，所以启动校验强制：

```text
worker_concurrency <= guard_connection_budget
claim_batch_size <= min(worker_concurrency, guard_connection_budget)
```

生产部署还要把普通连接池、guard 预算、每进程一条 listener、迁移连接和
运维余量相加，确保没有超过 PostgreSQL 或代理的全局连接上限。

## 3. 被固化的架构不变量

下列字段虽然出现在 TOML 中便于审计，但在 `settings.py` 中使用单值
`Literal` 或强校验，不能通过环境变量关闭：

- Task Registry、事件事实源、checkpointer 均为 PostgreSQL；
- claim 策略为 `FOR UPDATE SKIP LOCKED`；
- advisory lock 始终启用，并绑定同一物理 session；
- `FencedCheckpointer` 和 Tool Execution Ledger 始终启用；
- lease 时间使用 PostgreSQL 时钟，fencing token 是单调 `lease_epoch`；
- advisory-lock key 使用稳定 signed-int64 映射，禁止 Python `hash()`；
- `LISTEN/NOTIFY` 只携带 cursor，只负责唤醒，正文从 `run_events` 回放；
- Task 控制平面只有 LangGraph；
- Agent Tool Loop 只有自研 `claude_like` Runtime；
- `ModelPort` 保持 provider-neutral，但 v1 可配置的生产 Adapter 只有
  DeepSeek；新增或更换 Provider 时升级配置 schema，而不是接受一个无法启动的
  provider 字符串；
- `model.base_url` 只能是 HTTPS，除非指向 loopback：每一次请求都带着 provider
  API key；它同时禁止 userinfo、query string 和 fragment；
- `model.base_url` 是部署状态，不进入 Task 恢复快照——恢复一个旧 Task 不该
  连回它当初的端点，迁移端点也不该改变一个在跑的 Task 的语义；
- LangChain 只能作为 model/tool adapter，不能启用 AgentExecutor 或 Memory；
- LlamaIndex 只能 ingestion/retrieval，不能生成最终回答或二次融合；
- dense+sparse fusion 只由 Qdrant Query API 的 RRF 执行；
- Qdrant 是可重建派生索引，ACL 事实仍以 PostgreSQL 为准；
- 模型增量只有一个所有者：
  `event_stream.model_delta_mode="ephemeral_sse_coalesced"`；delta 只实时
  发送，不写 `run_events`；
- Shell Tool、写工具和可观察正文默认关闭。

这比“默认值写成 false”更强：错误的环境覆盖会让进程启动失败，而不是
悄悄改变运行语义。

## 4. Lease 与故障注入校验

### 4.1 Chat 固定执行 lease

`[chat]` 与 `[coordination]` 不是同一组配置。前者约束同步、无 checkpoint 的
Chat 请求；后者属于可恢复 Task Worker。

```text
chat lease_until
= PostgreSQL claim 时刻
  + api.request_timeout_seconds
  + chat.orphan_grace_seconds
```

`api.request_timeout_seconds` 约束完整 Chat use case，不只约束单次模型 HTTP。
`chat.reaper_poll_seconds` 和 `chat.reaper_batch_size` 控制 API lifespan 内的
terminal-only running reaper 与 pending-release recovery；后者重新进入当前
ACL/revision 发布栅栏，不重跑模型。`chat.orphan_action="terminal_fail"` 与
`chat.automatic_retry=false` 使用单值 Literal 锁死：当前没有 checkpoint、attempt
event 和副作用 ledger，过期 Turn 只能失败，不能自动重放。

running reaper 必须调用 `ChatExpirationCoordinator`，不能直接调用
ConversationStore 写过期行。claim 不机会式回收；`prepare_release`、普通 failure
writer 和 cleanup 在 session/Turn 锁内复核 PostgreSQL 时间，发现 lease 已到期只报
`ChatTurnLeaseExpiredError`、不写新事实；协调器已提交时只观察既有终态。唯一
expiry writer 为每个 Turn 打开独立 PostgreSQL 事务，以
`FOR UPDATE SKIP LOCKED` 选择候选，并把
`failed(deadline, stale_execution)`、lease 清理和 durable `ChatTurnExpired` 一起
提交；失败候选整体回滚，跨轮稳定扫描游标确保小 batch 下仍会推进到后续项。

answer release 与 expiry 统一调用 `chat_turn_terminal_event_key(turn_id)`，实际键为
`chat-turn:{sha256(turn_id)}:terminal`。SHA-256 使任意合法长度的 Turn ID 都映射到
EventLog 的 128 字节上限内，也保证 answer/expiry 不会用两个键各自提交。
`ChatTurnExpired` 是 Chat ledger 终态观察，不是 Runtime `RunFailed`。Memory double
只承诺单进程可观察语义：Event 失败时 Turn 仍为 `running` 且 session 不释放，不宣称
PostgreSQL 级耐久性。

生产 deadline 只取 PostgreSQL 时钟。测试通过可控 clock 或条件推进
`lease_until`，不用真实等待。`orphan_grace` 是清理余量，不是 heartbeat，也不能
续租。`chat.disconnect_poll_seconds` 只负责观察 ASGI 断开；发现断开后会同时设置
协作式 token 并取消实际 Chat task。

`0009_chat_turn_lease` 是明确的停机迁移，不是 rolling migration。旧应用不会写
`lease_until`，因此运行该迁移前必须先停止旧副本；需要滚动发布时应另行拆成
add-nullable、双写/backfill、validate/enforce 三阶段。

### 4.2 Task lease 与故障注入

配置校验使用以下安全关系：

```text
lease_duration
> heartbeat_interval × (max_missed_heartbeats + 1)
  + lease_grace
```

heartbeat 必须在独立协程中运行，不能被模型或 Tool 调用阻塞。所有重要写
入——Registry、checkpoint、run event、approval、Tool ledger 和外部副作
用提交——仍须校验 `lease_owner + lease_epoch`；advisory lock 不能替代
fencing 和幂等键。

failpoint 使用双重门禁：

```text
app.environment == "test"
testing.failpoints_enabled == true
testing.allow_fault_injection == true
testing.allowed_failpoints 非空
```

`allowed_failpoints` 是测试进程可激活的白名单，不代表启动时把所有点同时
阻塞。字符串集合与基线 12.5 完全一致，未知名称启动即失败。它不能由
HTTP 请求开启。并发测试需要真实 PostgreSQL、命名 barrier、
`LeaseController` 和可控时钟，不用随机 `sleep` 碰运气。

## 5. RAG 版本与索引迁移

配置固定以下候选漏斗：

```text
Qdrant dense + sparse
→ Qdrant RRF
→ BGE reranker
→ answer context + citations
```

启动时校验：

```text
rerank_top_k <= fused_top_k
answer_context_k <= rerank_top_k
fused_top_k <= dense_top_k + sparse_top_k
```

Embedding/reranker revision、dense dimension、vector field、parser、chunker
或 index schema 变化时，创建新的 `write_collection`，完成回填与评测后
原子切换 `read_alias`。不要向同一 collection 混写不同版本的向量。

API Chat 启动前会单独校验 `write_collection` 和当前 `read_alias` 的目标：
两者都必须匹配 dense vector dimension、cosine distance 和命名 sparse vector。
`read_alias` 指向不同的、但 schema 兼容的 generation 是正常的 blue/green 状态，
不会被启动过程改回 write collection。默认
`qdrant.allow_local_bootstrap=false`；只有显式开启的 local/test profile 才能在
write collection 缺失时创建它，并为缺失的 read alias 首次绑定 write collection。
remote/production 缺 collection、缺 alias 或 schema 不匹配一律启动失败，绝不静默创建
或重写路由。
生产环境中的 embedding 和 reranker revision 必须分别是 Hugging Face
解析后的完整 **40 位十六进制 commit SHA**；校验格式为
`^[0-9a-fA-F]{40}$`，持久化前统一规范化为小写。分支、tag、`main`、
`latest`、短 SHA 和其他可移动 revision 一律拒绝。

RAG 评测配置覆盖 Retrieval、Rerank、Answer、拒答、Citation、分阶段延迟、
token 和 cost；Task 与 Multi-Agent 指标分开配置。LLM judge 默认关闭，
启用时必须固定 `model_id/model_revision/prompt_version/temperature` 并配置
人工校准集。

任何 `deployment_scope=remote` 的环境都强制 Qdrant 使用 HTTPS、
`api_key_required=true` 且密钥已经注入；只有 `local` development/test
profile 才允许无鉴权的本机 Qdrant。

## 6. 文档上传的数据面

`api.max_control_request_body_bytes=2 MiB` 只限制 JSON、metadata 和控制请求，
不承载 PDF 正文。上传固定走：

```text
create-upload(size/hash/metadata)
→ ArtifactStore 数据面上传
→ complete + HEAD/hash/size/tenant 校验
→ document metadata + outbox
```

S3-compatible backend 返回 presigned URL；本地 backend 使用独立流式端点，
边读边写 quarantine，不把整个文件放进内存。真实文件上限由
`artifact_store.max_artifact_bytes` 控制，服务端生成对象 key，校验完成后
才能被 ingestion 引用。

## 7. 配置快照、策略与恢复

进程启动后 Settings 是 immutable，但不能把整份 `public_config()` 当成
可恢复输入。每次提交 Task 时分开保存：

```text
task_runs.run_semantics_revision         TEXT
task_runs.run_semantics_snapshot         JSONB
task_runs.graph_version                   TEXT
task_runs.submitted_policy_revision       TEXT
task_runs.submitted_policy_fingerprint    TEXT
task_runs.submitted_authorization_envelope JSONB
task_runs.submitted_principal_scopes       JSONB
task_runs.resolved_qdrant_collection      TEXT
task_runs.resolved_qdrant_index_version   TEXT
task_runs.resolved_qdrant_index_generation_id UUID
```

- 不使用知识库的 Task 可用 `Settings.task_run_semantics_snapshot()` 和
  `Settings.task_run_semantics_revision()` 的无索引形式，只保存
  model/runtime/graph/multi-agent/RAG 等决定性语义；
- 涉及知识库的 Task 在打开 PostgreSQL 短事务前把 Qdrant read alias 解析
  为具体值，再在同一个提交事务中锁定可保留的
  `qdrant_index_generations` 行，写入 `resolved_qdrant_collection`、
  `resolved_qdrant_index_version`、`resolved_qdrant_index_generation_id`
  和由 collection/version 生成的
  `Settings.task_run_semantics_snapshot(...)` /
  `Settings.task_run_semantics_revision(...)`；
- 可恢复快照绝不保存 Qdrant alias，也绝不在 resume 时恢复或重新解析
  alias；alias 只负责为**新 Task**选择当前索引；
- resume 始终查询 `task_runs` 中保存的 generation ID 与具体
  collection/index version，并校验它们与语义快照一致；目标已不存在或版本不兼容时必须
  fail closed / `waiting_migration`，不能回退到当前 alias；
- `public_config()` 只用于脱敏诊断，不能反序列化为恢复配置；
- API、DSN、secret、endpoint、coordination、event stream、ArtifactStore、
  observability、evaluation、testing、`optional_labs.*` 和 `policy.*` 不从
  旧 snapshot 恢复；关闭的 Lab 不能被旧 Task 快照重新打开；
- 恢复时沿用旧的确定性语义和 graph version，但每次 claim/resume、Tool
  dispatch 与副作用提交都重新读取当前 Policy、ACL 和 Tool Registry；
- 新的决定性语义配置只影响新 Task。v1 不做通用配置或 Policy 热更新：
  current Policy 是当前进程启动时由 immutable Settings 装配的安全下限；
  `policy.revision` 是人工发布标签，必须与排除该标签后由规则派生的
  `Settings.policy_fingerprint()` 一起形成 `policy_identity()`。Policy
  变更必须停止新 claim、排空/取消旧 Worker，再以同一 identity 重启全部
  Worker，禁止新旧 identity 混跑。动态紧急撤权使用当前 ACL 与 Tool
  Registry。

有效授权采用 deny-overrides 的最严格交集：

```text
submitted_authorization_envelope ∩ submitted_principal_scopes ∩ current_policy/current_ACL/current_tools
```

allowlist 取交集、denylist 取并集、approval requirement 取 OR、安全上限取
min。新 Policy 部署完成后，收紧在下一个 claim/resume、dispatch 或不可逆
提交前授权边界生效，但不能追溯撤销已经 dispatch 的外部效果；放宽也不能
扩大旧 Task 的原始授权。无法安全合并或 policy schema 不兼容时 fail
closed / waiting_migration。
事件与 Tool ledger 同时记录 `effective_policy_revision` 和
`effective_policy_fingerprint`，不记录敏感规则正文。这样即使人工修改规则
却忘记提升 revision，也会被一致性门禁发现。

`submitted_principal_scopes` 是当前 local/dev Header identity resolver 在
**提交时**解析到的权限上限，旧 Task 迁移为 `[]` 并因此保持拒绝。它用于避免
同一 idempotency key 的重试因权限变宽而扩大既有 Task；不是生产授权系统，也不
能感知之后的 scope 撤销。生产部署必须在每次 claim/dispatch 再与 live IdP/ACL
resolver 的当前结论求交集，不能把该快照描述为实时权限。

日志中只能输出 `public_config()`，禁止打印整个 Settings 对象。

## 8. 请求级覆盖边界

请求只允许在系统上限以内下调：

- step、token、Tool 次数和超时预算；
- dense/sparse/fused/rerank `top_k`；
- 预先登记的 model profile。

请求不能覆盖：

- DSN、tenant、Qdrant collection/alias；
- Embedding、fusion owner、graph version；
- policy、Tool 白名单、审批规则；
- lease、fencing、advisory lock；
- Optional Lab、failpoint 或任何安全开关。

建议单独定义 `RunOverrides` DTO，并通过 `min(requested, global_limit)`
生成本次 run 的有效预算；不要把请求字典直接 merge 进 Settings。

## 9. Multi-Agent 预算与 Optional Lab

固定 Graph 的结构限制与运行时调用预算使用不同名字：

- `static_agent_node_limit`：启动时校验 compiled graph，实际节点数从图派生；
- `max_parallel_agent_invocations`：同时进入自研 AgentExecutor 的上限；
- `max_agent_invocation_attempts_per_task`：含 retry/reclaim 的持久总次数上限；
- `max_tokens_per_agent_invocation`：单次物理 Agent invocation 的 token 上限。

invocation 计数必须持久化并受 fencing 保护，不能只放在可能回滚的
checkpoint 中，否则崩溃重放可能绕过预算。

默认 TOML 中所有实验项均关闭。v1 production profile 只要发现任一
Optional Lab 为 true 就拒绝启动。

- CrewAI 只能在 `environment=test` 的独立 benchmark 进程启用；
- 动态 Agent、mailbox、runtime mid-loop resume 不进入 v1 主链路；
- Langfuse、MCP、Redis Streams 和高级 compaction 先保持扩展点，不成为
  Task 恢复的事实源。

## 10. 使用与验证

本地开发：

```bash
cp .env.example .env
uv run --frozen agent-config-check --profile development
```

确定性协调测试：

```bash
uv run --frozen agent-config-check --profile test
uv run --frozen pytest -q
```

依赖：

```bash
uv sync --frozen --group dev --no-editable
```

提交前至少执行：

```bash
uv lock --check --offline
UV_OFFLINE=1 uv run --no-sync ruff format --check .
UV_OFFLINE=1 uv run --no-sync ruff check .
UV_OFFLINE=1 uv run --no-sync pyright
UV_OFFLINE=1 uv run --no-sync pytest -q
```

`config/ownership.yaml` 使用 JSON-compatible YAML，使架构测试无需额外
YAML parser 即可读取。CI 会递归提取 Pydantic Settings 的全部叶子字段，
要求每个字段恰好登记一次，并强制生命周期只能是
`startup/live/task_snapshot/test_only/lab`。Task snapshot 使用正向
allowlist；`testing.*` 只能是 `test_only`，`optional_labs.*` 只能是 `lab`。

配置测试至少覆盖：transaction pooling 拒绝、lease/heartbeat 关系、guard
连接预算、Qdrant 单一融合、RAG top-k 漏斗、生产 revision pin、生产
revision 的完整 40 位 HF commit SHA、`production → remote`、任意 remote
Qdrant 的 HTTPS/鉴权要求、实际安装的 `pydantic-settings>=2.14.2,<3`、
Optional Lab 拒绝、failpoint 名称/双门禁、密钥来源冲突、扁平 secret
加载和脱敏 fingerprint。Task 提交/恢复测试还必须证明 alias 切换后旧 Task
仍使用原 `resolved_qdrant_collection/resolved_qdrant_index_version` 与
generation reservation，非终态引用阻止旧 collection GC。

这些配置测试不能代替协调集成测试。M3b 仍需在真实 PostgreSQL 中用
`pg_backend_pid()` 证明 guard 全程使用同一物理连接，并主动终止该 backend，
断言旧 Worker 随即停止 Registry、checkpoint、event 和副作用写入。
