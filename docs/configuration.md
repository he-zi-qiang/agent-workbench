# Agent Workbench 配置管理基线

本文件记录正式项目当前采用的配置边界：

- `config.default.toml`：可提交、无真实密钥的默认值；
- `config.test.toml`：测试环境深度合并覆盖，专门开启确定性 failpoint；
- `config.production.toml`：无密钥的 production 合同覆盖，缺少部署注入时
  必须失败关闭；
- `config/ownership.yaml`：283 个配置叶子字段的唯一 owner 与生命周期登记；
- `.env.example`：本地开发需要注入的 DSN、模型 ID 和密钥名称；
- `src/agent_workbench/bootstrap/settings.py`：Pydantic Settings 类型、来源优先级、脱敏快照和跨域不变量；
- `tests/config/test_settings.py`：配置契约测试；
- `pyproject.toml` 与 `uv.lock`：运行时和测试依赖的唯一声明与解析结果。

正式项目锁定 Python `>=3.12,<3.13`。
当前架构基线为 `1.3`，配置 schema 为 `1.14`；两者是不同版本轴，架构基线不随
配置 schema 走。schema 每一次抬升都对应一条 ADR：

| schema | 原因 | 依据 |
|---|---|---|
| `1.1` → `1.2` | 模型 Provider 从 Anthropic 换成 DeepSeek，并新增 `model.base_url` | — |
| `1.2` → `1.3` | 无接地对话成为显式形态，新增 `chat.retrieval_shape` | [ADR-018](./adr/0018-ungrounded-chat-shape.md) |
| `1.3` → `1.4` | 提示词与工具参数记进事件流，新增 `runtime.record_step_inputs` | [ADR-019](./adr/0019-run-step-transparency.md) |
| `1.4` → `1.5` | 接上外部检索，新增 `[research]`；Task 授权信封改为按配置决定 | [ADR-020](./adr/0020-external-web-search.md) |
| `1.5` → `1.6` | `RunBudget` 去掉跨字段校验，`max_tool_calls` 与 `max_steps` 相互独立 | [ADR-022](./adr/0022-tool-ceiling-closes-the-toolbox.md) |
| `1.6` → `1.7` | 新增 `[mcp]`；其解析出的工具名会写进 Task 授权信封 | [ADR-025](./adr/0025-mcp-adapter.md) |
| `1.7` → `1.8` | MCP server 新增显式工具 allowlist，使 API 提交与 Worker 启动发现能确定性取交集 | [ADR-025](./adr/0025-mcp-adapter.md) |
| `1.8` → `1.9` | 新增 `[sandbox]`；开启它会把 `sandbox_run` 写进 Task 授权信封，并把信封风险上限抬到 `external` | [ADR-029](./adr/0029-ephemeral-sandbox.md) |
| `1.9` → `1.10` | 新增 `[[mcp.servers]].audience`；它决定一个 server 的工具进哪个 Agent，因此这一版的配置文件能改变运行中的图里谁能调什么 | [ADR-027](./adr/0027-read-outward-write-inward.md) |
| `1.10` → `1.11` | `max_steps` 上限放宽；1.11 的配置文件可以写 `max_steps = 500`，1.10 的二进制会在校验时拒绝它 | [ADR-030](./adr/0030-working-nodes-are-governed-by-cost.md) |
| `1.11` → `1.12` | 新增顶层 `[triage]`（默认关闭）；它决定一次提交走哪种形态 | [ADR-036](./adr/0036-triage-decides-the-shape.md) |
| `1.12` → `1.13` | 新增 `rag.graph`（默认关闭，两个冻结 Literal）；开启后图谱臂参与候选提名 | [ADR-037](./adr/0037-the-graph-nominates-chunks.md) |
| `1.13` → `1.14` | **方向相反的一次**：`evaluation.ragas_enabled` 只接受 `false`，`evaluation.rag_metrics` 只接受 `RETRIEVAL_METRICS` 里的键。上面每一条都是新文件向旧二进制要它没有的行为，这一条反过来——停止加载的是 1.13 那份文件（`ragas_enabled = true`、或它默认那 19 条指标名）。没有任何评测因此跑得不同，因为这两个字段从来没有被读过；抬版买到的只是"配置不再承诺一个这份二进制产不出的裁判" | [ADR-039](./adr/0039-a-metric-name-is-a-promise.md) |

[ADR-021](./adr/0021-chat-web-search.md) 把 `[research]` 从 Task 扩到 Chat 的兜底
分支，没有再抬 schema：它复用同一组字段，只是多了一个消费方。

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
- `model.<profile>.tool_calling_required=true` 只要求**开场 provider turn**选择工具；
  最新消息已经是 ToolResult 时，工具仍继续广告但 `tool_choice` 回到 auto，允许模型继续
  调另一个工具或完成回答；
- LangChain 只能作为 model/tool adapter，不能启用 AgentExecutor 或 Memory；
- LlamaIndex 只能 ingestion/retrieval，不能生成最终回答或二次融合；
- dense+sparse fusion **只发生一次**，且这一次在本进程内做
  （`rag.retrieval.fusion_owner = "application"`，见
  [ADR-033](./adr/0033-fusion-ranks-are-ours.md)）：适配器并发发出两条单臂查询，
  各自先定序，再对这两份原始名次算一次 RRF。取代 ADR-016 中"融合只在
  Qdrant Query API 里发生一次"这一条——**变的是在哪里融合，不是融合几次**；
- Qdrant 是可重建派生索引，ACL 事实仍以 PostgreSQL 为准；
- 模型增量只有一个所有者：
  `event_stream.model_delta_mode="ephemeral_sse_coalesced"`；delta 只实时
  发送，不写 `run_events`；
- Shell Tool、写工具和导出到 telemetry 的正文默认关闭：
  `observability.record_prompt_body` 与 `observability.record_tool_result_body`
  是单值 `Literal[False]`，因为 OTel span 会离开这个系统、去到一个没有租户边界的
  collector。

这比“默认值写成 false”更强：错误的环境覆盖会让进程启动失败，而不是
悄悄改变运行语义。

**`runtime.record_step_inputs` 不在这张表里，它是一个真正的开关。**
ADR-019 把提示词和工具参数写进**运行自己的事件流**——那条流按 tenant + owner
鉴权，只有拥有这个 Task/Session 的 principal 读得到，和 `ModelCompleted.text`
（早就在里面）同一个口径。默认 `false`，因为打开它会改变这个部署存了用户的什么；
打开它不会放松上面那条 telemetry 的限制，两者互不影响。

**`workflow.export_requires_approval` 同样是一个真正的开关**（ADR-038）。默认
`true`；关掉它，图从 `review` 直接走到 `export`。

它没有放在上面那张表里，也没有放进 `[policy]`，因为它守的**不是一条授权边界**。
export 把草稿写进这个租户自己的 artifact store，owner 是提交者本人；写完之后文件
没有离开这个部署，读取仍然逐 principal 鉴权，要到人手上还得有人点一次下载——那是
一次独立的、已鉴权的读。这道闸门拦住的事情是**一个文件出现在提交者自己的附件列表
里**。`[policy]` 里那些 `Literal` 拦的是"某个 principal 能不能碰到某个东西"，两者
不是一类；把一个能关的开关混进那张表，会让"这张表里的东西关不掉"这句话不再为真。

关掉它是**跳过闸门，不是自动批准**：不开审批行、不写 `approval_id`、不写
`approval_decision`。系统自己填一条 `TaskApprovalDecided{approved}` 会让审计记录
说一件没发生过的事，而事后无法与真的区分。相应地，导出的报告头部写
`- Approved by: not required by this deployment`，而不是编一个 id、也不是把这行
删掉。

值在 Task **提交时冻结**进 `TaskState` 并作为图通道传递，和 `wants_report` 同理：
路由是状态的纯函数，一个停在闸门前的 Task 不能在恢复时走进另一张图。

    [workflow]
    export_requires_approval = false   # 单人机器；仓库默认仍是 true

**关掉它意味着这条路径上不再有任何人工确认**，这一点要说清楚而不是绕过去：
ADR-015 当初把 `approval_required_risks` 置空，理由正是「v1 的人已经站在图边界上
了」——关掉这个开关，图边界上的人也不在了。没有变的是 `artifact:export` scope、
授权信封对 `export_artifact` 的点名、`write` 风险上限、artifact 的租户与所有者
边界、ledger 的一次性保证；变的是那一层人工确认。判断它可以接受的依据是 export
不外发（写进本租户自己的 store，谁也收不到），所以**一旦 export 开始外发，这个
判断就失效**——详见 ADR-038 §3.1 与 §4。

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
Qdrant dense 臂 + sparse 臂（两次并发的单臂查询）
→ 本进程内一次 RRF（adapters/vector/fusion.py，ADR-033）
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

`evaluation.rag_metrics` 只能列出 `evaluation.metrics.RETRIEVAL_METRICS`
真的会算的名字（`recall_at_1`、`recall_at_3`、`full_coverage_at_3`、`mrr`、
`retrieval_latency_ms`），多写一个名字在**配置加载阶段**就失败——和
`testing.allowed_failpoints` 同一个形状。这一条是修出来的：这份清单曾经有 19 项，
代码实现其中 2 项，Answer/拒答/Citation/rerank/token/cost 那些名字对应的判定器
根本不存在，而一个测试还在断言它们必须在场。Rerank、Answer、拒答、Citation、
分阶段延迟、token 与 cost 是路线图，写在
[实施计划](implementation-plan.md)里，不写在这份"什么在跑"的文件里。

`evaluation.ragas_enabled` **必须是 `false`**，写 `true` 会让配置加载失败并说明
原因：仓库里没有 RAGAS 依赖、runner 或 judge 校准集。它的默认值一度是 `true`，
于是"答案有没有被判分"这个问题在配置里得到了代码兑现不了的肯定答复。等 runner
落地，这条校验和它一起去掉。

Task 与 Multi-Agent 指标分开配置，但两者的 runner 同样尚未落地。LLM judge 默认
关闭，启用时必须固定 `model_id/model_revision/prompt_version/temperature`
并配置人工校准集。

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
- `max_tokens_per_agent_invocation`：单次物理 Agent invocation 的 token 上限；
- `max_cost_micro_usd_per_agent_invocation`：单次 invocation 的**成本**上限（微美元）；
- `max_seconds_per_agent_invocation`：单次**尝试**的墙钟上限。

invocation 计数必须持久化并受 fencing 保护，不能只放在可能回滚的
checkpoint 中，否则崩溃重放可能绕过预算。

### 成本与时限上限（ADR-030）

后两项默认**不设**，不设时行为与 ADR-030 之前逐字节相同（仍由步数与 token 约束）。
它们是给"会干活的节点"用的：一个反复读写、跑脚本、改了再跑的节点，第 3 步可能读 200 字节、
第 7 步可能读 200KB，**用步数管它们等于假装两者一样贵**。

两条各有一个前提，撞上了别当成 bug：

- **成本上限要求模型 profile 配了价格**（`[model.main.pricing]`，见下）。没配价格却设成本
  上限，run 会被**拒绝**并指名是哪个 profile 缺价——因为没有价格时花费恒为 0，那个上限
  永远不会触发，接受一个不可能生效的上限比拒绝它更糟。
- **墙钟是"一次尝试"的，不是一个 Task 的**。deadline 在每次 invocation 解析时按当时的时钟
  盖上去，所以崩溃重放后的新尝试重新拿满额度。反过来（把 deadline 存进 Task）会让一个熬过
  了外部故障的节点永远跑不完。

`runtime.max_steps` 的域上限同时从 100 放宽到 1000，**默认值仍是 12**。它的角色变了：
不再是"这个节点该做多少事"，而是防失控循环的兜底。默认不动，是因为 12 是每个 chat run 和
每个 v1 节点被实测过的值，为了服务会迭代的节点而给所有人调高，会改掉没人要求改的 run。

### 模型价格 `[model.main.pricing]` / `[model.compact.pricing]`

可选，默认不配。仓库**不出厂任何价格数字**——价格是某个部署与供应商之间的事实，发一个猜
出来的数字会让每份配置里都躺着一个看起来权威、其实不是的值。

四项费率，单位是**微美元每百万 token**（供应商就是按百万 token 公布的，照抄最不容易抄错）：

```toml
[model.main.pricing]
input_micro_usd_per_mtok = 270000        # $0.27 / Mtok
output_micro_usd_per_mtok = 1100000      # $1.10 / Mtok
cache_read_micro_usd_per_mtok = 27000    # $0.027 / Mtok
# cache_write_micro_usd_per_mtok 默认 0：不单独计费的供应商就是 0
```

一处容易算错的地方：**`input_tokens` 里含缓存命中的部分**（供应商的口径，DeepSeek 的
`prompt_tokens` 就是整个 prompt，`prompt_cache_hit_tokens` 是其中被缓存的那部分）。计价时
先把缓存部分从输入里减掉再按输入价计，否则同一批 token 会被按两种费率各收一次——后果是
**缓存过的 prompt 比没缓存的更贵**，成本上限恰好会在那些开了缓存来省钱的部署上最早触发。

默认 TOML 中所有实验项均关闭。v1 production profile 只要发现任一
Optional Lab 为 true 就拒绝启动。

- CrewAI 只能在 `environment=test` 的独立 benchmark 进程启用；
- 动态 Agent、mailbox、runtime mid-loop resume 不进入 v1 主链路；
- MCP Adapter 已按 [ADR-025](./adr/0025-mcp-adapter.md) 实现为 Task Worker 的
  Optional Lab；它的目录与调用结果都不是 Task 恢复的事实源，提交时授权信封和
  PostgreSQL 副作用账本才是。Langfuse、Redis Streams 和高级 compaction 仍只保留
  扩展点。

### 9.1 MCP Optional Lab

MCP 默认关闭，且只进入 Task Worker 的 `writer/synthesize` Agent：

```toml
[optional_labs]
mcp_adapter = true

[[mcp.servers]]
alias = "office"
transport = "http"
endpoint = "https://mcp.internal.example/mcp"
tools = ["render_document", "lookup_template"]
retryable_effects = true
timeout_seconds = 30
```

| 字段 | 约束 | 运行语义 |
|---|---|---|
| `alias` | 小写字母开头，只含小写字母/数字/下划线，最长 24 | 本地工具名前缀与 scope：`mcp:<alias>` |
| `transport` | 第一版只能是 `http` | 使用官方 SDK 的 Streamable HTTP；不派生本地进程 |
| `endpoint` | HTTPS，或本机 loopback HTTP；禁止 userinfo/query/fragment | 只由 Task Worker 建立连接 |
| `tools` | 非空显式 remote-name allowlist；归一化后不得碰撞 | API 据此冻结具体授权名，Worker 与启动目录取交集 |
| `audience` | `research` 或 `synthesis`，默认 `synthesis` | 这个 server 的工具进哪个 Agent：`research` → `researcher_external`，`synthesis` → `writer` |
| `retryable_effects` | 必填；无默认 | `true` 表示全部 allowlisted tool 在整个节点重放时都可再次调用；`false` 不进入 Task 可调用路径 |
| `timeout_seconds` | `1..600` | 同时约束启动发现与单次工具调用 |

`audience` 的默认值是**防回归**的，不是随手选的：这个字段出现之前写的每一份配置都意味着
`synthesis`——ADR-025 把动态目录只给了 `writer/synthesize`。默认成 `research` 会在升级时把
Word 渲染器从 writer 身上悄悄挪走。

它不改变 Task 授权信封：信封里仍然列出全部已配置的名字。**信封是 Task 的上限，audience 是
哪个 Agent 够得到它**——两者混为一谈的话，一个在 `synthesis` 期间提交的 Task 会用不了后来
改成 `research` 的工具。

profile 是按 Worker **实际注册到**的工具加宽的，不是按配置。启动时连不上的 server 什么都不
贡献；按配置加宽会让节点去请求一个网关解析不到的工具，那是"节点必崩"而不是"少一个能力"。

开启 lab 但 `servers=[]` 合法；配置 server 却不开 lab 会拒绝启动。远端临时不可达会让
该 Worker 在本次生命周期内不带该 server 的工具，并留下结构化日志，不会把远端后来新增
的工具热插进既有注册表。调用者还必须持有对应 `mcp:<alias>` scope。

结果同时受三个既有配置约束：

- `runtime.tool_result_artifact_threshold_bytes`：文本何时从模型上下文转入 artifact；
- `policy.max_tool_result_bytes`：SDK 已解析结果的语义总上限；
- `artifact_store.max_artifact_bytes`：归一化后单一 artifact 的存储上限。

结果上限在官方 SDK materialize 响应后执行，不是 HTTP body 或进程内存硬上限。本版不支持
stdio、OAuth、热更新、MCP Tasks、Tool 级动态审批和跨 Worker 进程的全局串行。完整边界、
命名、schema、安全重放和多 content-block 映射见
[MCP Adapter 实施计划](./archive/mcp-adapter-plan.md)。

### 9.2 一次性沙箱

`[sandbox]` 由 [ADR-029](./adr/0029-ephemeral-sandbox.md) 引入，默认关闭，开启后只进入
Task Worker 的 `writer/synthesize` Agent：

```toml
[sandbox]
enabled = true
endpoint = "http://127.0.0.1:8766/mcp"
timeout_seconds = 180
```

| 字段 | 约束 | 运行语义 |
|---|---|---|
| `enabled` | 默认 `false` | 关闭时 Worker 不探测，`sandbox_run` 也不进授权信封 |
| `transport` | 只能是 `http` | 与 `[mcp]` 同一条理由：不派生本地进程 |
| `endpoint` | 沙箱 MCP server 的地址 | 只由 Task Worker 连接 |
| `timeout_seconds` | `1..600` | 同时约束启动探测与单次调用 |

**这一段不描述隔离。** 无网络、只读根、非 root、内存/CPU/进程数/墙钟上限全部是
`apps.sandbox_mcp.executor` 里的常量，配置够不到——ADR-029 §3.2 的立场是断网是整条
重放保证成立的前提，不是可调的加固项。这里只说"连哪个沙箱"。

开启后信封同时发生两件事：allowlist 多出 `sandbox_run`，且 `max_tool_risk` 抬到
`external`（`sandbox_run` 声明 `risk="external"`，只加名字不抬上限的信封仍会拒绝它）。

**两个部署前提**，缺了不会让 Worker 起不来：

```bash
docker pull python:3.12-slim
agent-sandbox-mcp
```

Worker 启动时做一次探测，而且是**真发一次 `run_python`**——能连上的 socket 既不证明有
容器运行时，也不证明容器能起来。探测失败（连不上，或连上了但运行时不可用）会记
`sandbox_probe_failed` / `sandbox_runtime_unavailable`，**不注册这个工具，进程照常启动**
（ADR-029 §3.6）。此时信封里仍有 `sandbox_run`——它是提交时按配置冻结的——所以 Agent
profile 是按**实际注册到的工具**加宽的，不是按配置。两者若反过来，节点会去请求一个网关
解析不到的工具。

可直接使用的本地 profile 见 `config/config.sandbox-local.toml`。

## 10. 外部检索

`[research]` 由 [ADR-020](./adr/0020-external-web-search.md) 引入，
[ADR-021](./adr/0021-chat-web-search.md) 把它从 Task 扩到 Chat：

| 字段 | 默认 | 约束 |
|---|---|---|
| `research.enabled` | `false` | 见下，默认值是承重的 |
| `research.provider` | `"deepseek"` | 单值 `Literal`，v1 只有这一个 Provider |
| `research.base_url` | `https://api.deepseek.com/anthropic` | 与 `model.base_url` 走同一条 endpoint 校验：只能 HTTPS（loopback 除外），禁止 userinfo、query string 和 fragment |
| `research.model_id` | `"deepseek-chat"` | 非空 |
| `research.max_uses` | `5` | `1..20`，单次 `external_search` 调用内的搜索次数 |
| `research.timeout_seconds` | `60` | `1..600` |

四件需要单独记住的事：

- **它没有自己的 API key。** 搜索在 Provider 侧执行，走的是同一把
  `secrets.deepseek_api_key`。`research.base_url` 之所以和 `model.base_url`
  分开，是因为只有 Anthropic-compatible 那条路径讲 Messages 协议，而 Runtime
  其余调用走的是 OpenAI-compatible 的那条——同一个服务的两条路径。
- **`enabled = false` 不只是保守，它是承重的。** Task 授权信封由
  `projections.task_authorization_envelope` 按这个字段选出，并**随 Task 一起存
  下、每次恢复重新施加**。一个从没开过外部检索的部署，不能因为升级就让历史
  Task 的信封变宽。
- **Chat 侧还要过 scope。** Chat 的 `web_search` 出现在两条**没有证据**的路径上，
  且都是模型可以不用的工具：`routed` 检索后判定语料库没覆盖的兜底分支
  （[ADR-021](./adr/0021-chat-web-search.md)），以及用户没选知识库的自由回答
  （`answer_mode = "direct"`，[ADR-023](./adr/0023-direct-chat-reaches-the-web.md)）。
  `routed` 的**接地**分支永远够不到它——语料库能回答的问题从语料库回答，这是
  `routed` 可测量的前提。调用方没有 `external:search` scope 时，policy 会逐次
  拒绝，run 照常给出无证据的回答（[ADR-022](./adr/0022-tool-ceiling-closes-the-toolbox.md)
  之后不再 502），只是会先把工具额度耗在被拒的提议上。
- **`enabled = true` 但没有真实 key 是启动错误，所有环境都查，不只是
  production。** 见 `Settings` 的跨域校验：enabled-without-a-key 在配置文件里
  读起来像"联网搜索已经能用"，却要到第一次搜索才失败关闭——这正是这个项目
  反复清除的那类"配置描述了一个并不存在的系统"的缺陷。**因此被 Git 跟踪的
  `config.local.toml` 不能把它打开**：那会让每一个没有 key 的 checkout 都启动
  不了。要用就在自己那次会话的 shell 里开：

  ```bash
  export AW_SECRETS__DEEPSEEK_API_KEY=sk-...
  export AW_RESEARCH__ENABLED=true
  ```

## 11. 使用与验证

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
