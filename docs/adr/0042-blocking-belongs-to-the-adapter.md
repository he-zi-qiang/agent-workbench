# ADR-042：阻塞是 Adapter 的属性，不是调用点的

- 决策点：WP08-12 要的 `AdapterCallRunner` 到底是「所有 Model/Tool/外部 SDK 调用
  的唯一入口」还是别的什么；有界池的界从哪个数来、放哪个 section、几个字段；
  `Semaphore` 与 `ThreadPoolExecutor.max_workers` 是同一个数还是两个数；池饱和
  之后是拒绝还是排队；哪些调用本批挪出事件循环、哪些明确不挪；加了配置字段要不要
  抬 schema 版本
- 状态：**接受**，收窄 `docs/implementation-plan.md:900` 那句「唯一入口」，理由
  写在 §1
- 日期：2026-08-11
- 影响：新增 `adapters/concurrency/call_runner.py`（专用
  `ThreadPoolExecutor` + 等大 `Semaphore` + 排队超时）；新增两个配置字段
  `coordination.blocking_call_slots`（默认 2）与
  `coordination.blocking_call_queue_timeout_seconds`（默认 30）；
  `config/ownership.yaml` 新建一组，owner=`adapters.blocking_call_runner`、
  lifecycle=`startup`；新增 `BlockingCallRunnerConfig` 投影进三个进程的
  `RuntimeConfig`；四个 Adapter 的五条路径改走 runner。**配置 schema 保持
  `1.14`、零迁移、零 `Literal` 变动。**`LocalArtifactStore.put` / `put_stream`
  一处不动；`DocumentParserPort.parse` 一处不动；30+ 个调用点一处不动。
- 依赖：[ADR-036](./0036-triage-decides-the-shape.md)（新增整个 `[triage]` 段并
  抬版 `1.11` → `1.12`：本条据以说明先例不自洽的一半）、
  [ADR-038](./0038-the-export-gate-guards-a-list-not-a-boundary.md)（新增带默认值
  的配置叶子而明写「配置 schema 不变」：本条跟随的那条先例）、
  [ADR-039](./0039-a-metric-name-is-a-promise.md)（`config_schema_version` 的
  pin 机制，以及指标命名的 `_ms` 后缀规矩）

## 1. 决策：倒转计划里的那句话

`docs/implementation-plan.md:900` 写的是：

> `AdapterCallRunner` 是所有 Model/Tool/外部 SDK 调用的唯一入口

本条把它改成：**`AdapterCallRunner` 是阻塞 Adapter 离开事件循环的唯一出路。**

这是一次明确的**收窄**，不是遗漏。三条理由：

### 1.1 边界

Tool Loop 只有一个所有者是冻结不变量（`multi_agent.worker_executor =
"custom_runtime"`）。把工具派发改成物理穿过一个 runner 对象，等于在自研 Runtime
与它的工具之间插进第二个持有者。那件事需要它自己的 ADR，而它买到的东西是零。

### 1.2 事实：两条主干上，计划要的东西已经成立

对着代码核：

- **Model 调用全仓只有 1 个点**：`runtime/agent_runtime.py:518`，且 527 行已被
  `asyncio.timeout(deadline.seconds)` 包住；
- **Tool handler 派发全仓只有 1 个点**：`runtime/tool_executor.py:74`，且 73 行
  已被 `asyncio.timeout(limit)` 包住（tool 声明超时 ∩ run 剩余预算取小）。

也就是说，计划要的「原生 async Adapter 必须遵守 deadline/cancellation contract」
在这两条主干上**事实上已经成立**，缺的只是一个名字。给它套一个 runner 对象，
改的是称呼不是行为。

### 1.3 工程：真正散着的是别的口子，而改它们只换来一个名字

- `ExternalSearchPort.search`：2 个点（`tools/web_search.py:112` 与
  `application/task_research.py:203`，后者绕开 `ToolExecutor` 直接从 research
  node 调）；
- Embedding / Sparse：约 10 个点；
- `ArtifactStore`：约 22 个点。

把这 30+ 个点全改成物理穿过一个对象，是改 30+ 处换一个名字。

**阻塞性是 Adapter 自身的属性，不是调用点的属性。**注入到会阻塞的那几个 Adapter
里，改动面是个位数文件；做成"名义上的唯一入口"吃不下，也不该做。

## 2. 本批挪的是哪五条路径

runner 注入给 4 个 Adapter 的 5 条路径：

| 位置 | 今天是什么 |
|---|---|
| `adapters/embedding/bge.py:218` | `await asyncio.to_thread(run)`，默认池 |
| `adapters/embedding/bge_sparse.py:187` | 同一形状，默认池 |
| `adapters/reranking/bge_reranker.py:140` | 同一形状；三处中唯一有调用方超时的 |
| `adapters/artifacts/local.py` 的 `get` | 阻塞文件读，**根本没进任何线程池** |
| `adapters/artifacts/local.py` 的 `_read` | 同上 |

runner 落在 `adapters/concurrency/call_runner.py`。落点不是随手选的：
`tests/architecture/test_dependency_boundaries.py` 只允许 `_config` / `adapters` /
`apps` / `bootstrap` / `interfaces` / `workers` 六个外层包持有
`ThreadPoolExecutor`。

## 3. 界是两个新配置字段，不是一个也不是四个，而且不能从现有数字派生

**不能从 `multi_agent.max_parallel_agent_invocations` 派生。** 那是 LangGraph
里 agent 节点的扇出上限（`task_handlers.py:346` 的 `BoundedParallelExecutor`），
**只在 task worker 存在**；而阻塞调用最密集的进程是 **API**——它同时持
dense + sparse + reranker，而 task worker 根本没建 reranker。

**不能从 `coordination.worker_concurrency` 派生。** 那是 lane 数，被
`guard_connection_budget` 约束（`settings.py:1168`），语义是「能同时持几个 PG
guard 连接」，跟「能同时跑几个 torch forward」没有关系。

torch/MPS 上的合理并发是 1-2，跟这两个数字都无关。所以只能是新字段：

- `coordination.blocking_call_slots`（`int`，默认 2，`ge=1 le=32`）
- `coordination.blocking_call_queue_timeout_seconds`（`float`，默认 30，`ge=1`）

**第二个字段不是可选的。**有界池 + 无排队上限，等于把无界队列从
`ThreadPoolExecutor` 的 `SimpleQueue` 搬到信号量上——而本条的全部理由本来就是
「无界排队今天不可观测」。

排队上限**只管等一个 slot，不管调用本身**，所以一次合法的慢调用不会被它杀掉。

默认 2 的理由要写出来：torch/MPS 上一次 forward 已经在内部用满核，第二个 slot
买的是「一次慢调用不挡住另一条请求」，**第三个买不到什么**。

## 4. 放 `[coordination]`，理由不是「找不到别的地方」

`[runtime]` / `[multi_agent]` / `[rag]` 三个前缀都在
`config/ownership.yaml:10-23` 的 `task_snapshot_allowlist` 里（已核对）。放进去
会让「这台机器给阻塞调用开几个线程」变成**每个 Task 的语义**，并改动全体 Task 的
`run_semantics_revision`（`settings.py:1400-1413` 整段取 `public["..."]`）。

这跟 `docs/status.md:257` 否掉新增 `rag.enabled` 开关是同一条推理：**不要把部署
状态伪装成语义。**

`coordination.*` 是**整段**不在 allowlist 里，比依赖「`[app]` 段恰好只列了
`config_schema_version` 与 `architecture_baseline` 两个显式叶子」更稳。

而且这两个字段的作用，是让事件循环保持足够响应、以致心跳和租约仍然诚实——
**它们是存活性参数**，`[coordination]` 是对的家。

## 5. 一个全局池大小，不按 dense/sparse/rerank/io 分成四个字段

老实承认这是妥协。三个进程要挡的东西不一样：

| 进程 | 它的阻塞源 |
|---|---|
| API | embed + rerank + 文件读 |
| 摄取 Worker | parse + embed |
| task worker | embed + 文件读 |

一个统一大小的池会同时过大和过小，对最贵的那类偏小。

分四个字段每个都要登记 owner、写默认值、有人解释默认值怎么来——买到的准确度不值
那个配置面。

## 6. `Semaphore` 与 `max_workers` 相等，但两个都不能删

`max_workers` **真正限制并发**；`Semaphore` 用来**产生可观测的排队与超时信号**
——`ThreadPoolExecutor` 的 work queue 是无界的 `SimpleQueue`，既不可观测也无法
超时。

写死这一点，否则后人会看到「两个相等的数」而把其中一个删掉，于是背压信号消失、
只剩越来越长的延迟。

## 7. 饱和的后果是排队 + 超时，不是拒绝

三处模型推理与 artifact 只读路径**全是只读幂等的**。拒绝会把一次慢检索变成一次
失败检索，而调用方要的是结果不是失败。

## 8. 只搬只读幂等的调用；`put` / `put_stream` 不进池

`docs/implementation-plan.md:907` 自己写着「不可取消的非幂等写调用不能放进普通
线程池」。`LocalArtifactStore.put` / `put_stream` 正踩这一条：它的
quarantine → replace 语义在被取消后**有半个文件留在盘上**——`finally` 里有
`unlink`，但线程被取消后 `finally` 何时跑没人管。

所以本批**不挪它们**。`local.py:103-108` 与 `189-195` 那两处 docstring（今天写的
是「有界执行器属于协调工作包」）只把账**从「协调工作包」改挂到本 ADR 名下**，
并写明**本批只还了一半，不假装还了**。

## 9. 为什么值得做：默认池里排在嵌入后面的是 DNS

`asyncio.to_thread` 与 `loop.getaddrinfo` **共用同一个默认 executor**：CPython 的
`BaseEventLoop.getaddrinfo` 就是 `run_in_executor(None, socket.getaddrinfo, …)`，
httpx → anyio 也直接转发给它。

于是：API 进程里 32 个并发嵌入把默认池占满时，**DeepSeek 与 Qdrant 的 DNS 解析会
排在它们后面**——一个 CPU 密集的检索请求能把出站 HTTP 连接建立饿死。

而且默认池的排队是无界的，所以**今天不存在任何背压信号**，只有越来越长的延迟。

## 10. 顺手把一条今天真空成立的性质钉成回归

「await 被取消后，迟到的线程结果进了 EventLog / checkpoint / Ledger」——这个风险
**今天不存在**，而且**不是靠谨慎避开的**，是靠 CPython 语义免费得到的：
`_copy_future_state` 在目标 future 已 `cancelled` 时直接 `return`，值被丢弃。
三处 `to_thread` 的返回值也只交给直接 `await` 它的那一行。

**正因为它免费才必须钉住。**一旦 runner 返回一个带元数据的结果对象、调用方顺手把
它写进事件，这个属性会在没人察觉的情况下失效——没有任何测试会红，因为从来没有
测试证明过它。

## 11. 一条不修但要写下来的事实：摄取的迟到写是被确定性 id 兜住的

`workers/ingestion.py:200` 在 `await apply`（含 Qdrant upsert）**之后**才 recheck
guard。guard 丢失时 upsert 已经发出去了——**那是复查，不是围栏**。
`VectorIndexPort` 上没有任何 epoch / fencing。

今天后果有限，靠的是两件事而不是围栏：`chunk_id` 由 `document_version` + index
identity 派生（`ingestion.py:88-91`），不同版本不撞点；`last_applied_revision` 的
更新带 `where last_applied_revision < revision`（`ingestion.py:336`），是单调的。

本条之后没人会再去看这段代码，所以把它写进 ADR。

## 12. 后果

### 12.1 得到的

- 三处已有 offload 与 artifact 只读路径第一次有并发上限与背压；
- 默认线程池不再被嵌入占满，DNS 解析不再排在它们后面（§9）；
- 三笔「interim」欠账（`bge.py:197-206` 一笔、`local.py:103-108` 与 `189-195`
  两笔）**还了两笔半**。

### 12.2 代价，逐条写在明处

1. **一个统一的池大小同时过大和过小**（§5）。reranker 只在 API 进程
   （`task_worker/composition.py` 里 grep 不到 reranker），PDF parser 只在摄取
   Worker（`TextDocumentParser` 全仓只在 `apps/ingestion_worker/composition.py:160`
   构造过一次，已核对），task worker 只有 embed + artifact 读。接受这个妥协，
   好过四个字段各自解释默认值怎么来。
2. **池默认 2 会降低 API 高并发下的嵌入吞吐。**这是有意的（今天默认池的排队是
   无界且不可观测的），但它是一次**可感知的性能变化**。`docs/status.md` 里要给
   本机实测数并注明测量环境——而 RAG 评测正占着 MPS，测量要另行安排。
3. **`LocalArtifactStore.put` / `put_stream` 一处不挪**（§8）。API 进程的大文件
   上传仍然会阻塞循环；task worker 的循环上也仍然留着这条阻塞源
   （`apps/task_worker/composition.py:272` 确实构造了它）。**所以
   [ADR-041](./0041-a-late-heartbeat-may-not-renew.md) 的误判风险没有被本条清空**
   ——两份 ADR 都写了这句，因为「先做本条循环就干净了」这个前提是假的。
4. **embed 两条路径仍然没有调用方超时**（今天只有 reranker 在
   `application/retrieval.py:268` 有）。有界池没让它变差，但排队上限只截断
   **等 slot**，不截断**调用本身**。补调用方超时需要它自己的数字，**写成没做**。
5. **摄取 Worker 的 lease / heartbeat 继续复用 coordination 的 90/20**
   （`projections.py:866-868`），不给它独立字段。一份 100 MB PDF 占用一个 slot
   的时长仍可能超过 lease，接受「解析超 lease 就被 reclaim、靠 advisory guard 挡住
   重复索引」——**但这要配一条测试证明 guard 确实挡住了**，而不是靠运气。
6. **【本批明确不做的那一半】`DocumentParserPort.parse` 不改成 async**，两个同步
   调用点（`application/ingestion.py:114` 与 `application/graph_enrichment.py:64`）
   本批**一处不动**。理由写在明处：它是一次**跨层 port 契约变更**（动
   `tests/contracts/test_port_contracts.py` 与所有 fake parser），而它的收益——
   摄取 Worker 的心跳 / guard 在解析期间真的会跑——**只有在给 ingestion worker
   装上心跳自查时才兑现，而那是下一批**（ADR-041 §4）。本批做它等于付了代价拿
   不到收益。
7. **「唯一入口」这条计划字面不实现**（§1）。30+ 个调用点不改。这是本批对计划的
   一处明确收窄，`docs/implementation-plan.md` 的对齐里也要说出来。

## 13. 配置影响：新增两个字段，不抬 schema 版本

### 13.1 落地清单五处

1. `bootstrap/settings.py` 的 `CoordinationSettings` 加两个字段；
2. `config/ownership.yaml` 新建一组，owner=`adapters.blocking_call_runner`
   （要匹配 `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`），
   **lifecycle=`startup`——必须不是 `task_snapshot`**：`coordination.*` 不在
   allowlist 里，登记成 `task_snapshot` 会被
   `test_task_snapshot_is_the_planned_positive_allowlist` **双向**打红；
3. `config/config.default.toml` 的 `[coordination]` 段；
4. `docs/configuration.md` 写字段语义；
5. 投影：`BlockingCallRunnerConfig` 进三个进程的 `RuntimeConfig`。

按先例往
`tests/architecture/test_config_ownership.py::test_high_risk_fields_keep_their_narrow_owners`
加一行 `blocking_call_slots`——这个字段设成 32 就把循环饥饿问题原样还回来了，
属于「能打坏一条安全性质的字段」。

**不给 `validate_architecture_and_environment` 加交叉校验**：`blocking_call_slots`
与 `guard_connection_budget` / `worker_concurrency` 之间**没有真实约束**，硬编一个
会是假的不变量。

会因为漏登记而红的测试（精确名）：
`tests/architecture/test_config_ownership.py::test_every_pydantic_settings_leaf_has_exactly_one_owner`、
`::test_task_snapshot_is_the_planned_positive_allowlist`、
`::test_manifest_has_a_small_explicit_schema`。CI 的 quality job 还会跑三次
`agent-config-check`（development / test / production）。

### 13.2 抬版：不抬，并且要老实说清先例不自洽

判据写在
`tests/config/test_settings.py::test_the_configuration_schema_version_is_pinned`
的 docstring 里：**一份新版本文件在老二进制上要一个它给不了的行为**（或反向，
新二进制拒绝老文件）。

按字面，一份写了新键的文件喂给老二进制会被 `extra="forbid"` 拒绝，这就是「新文件
向老二进制要一个它给不了的行为」——**但同样的话对 ADR-038 加的
`workflow.export_requires_approval` 也成立**，而 ADR-038 明写「配置 schema 不变」，
实测 `e808b34` 前后都是 `Literal["1.13"]`；而 ADR-036 加了整个 `[triage]` 段却
抬了 `1.11` → `1.12`。

所以本 ADR **不说**「两条先例其实一致」——那是一句没有支撑的和解。明确表态：

> **两条先例在这个判据下不可调和。本条跟随更窄的那条（ADR-038：既有 section 内
> 新增带默认值的叶子不抬版）。理由是——若每新增一个带默认值的叶子都抬一次版，
> 版本串会在几乎每一批里变化，从而不再指示任何东西。**

这段话要补进那份 docstring（它是版本串唯一的理由清单）。
[ADR-040](./0040-a-task-pays-before-it-calls.md) §5 引用本节，并在那边补一条更窄
的判据：「第一次执行一条一直声明着的语义」也不构成抬版理由。

## 14. 什么会让这条决定重来

**`LocalArtifactStore` 被换成真的对象存储。** §8 拒绝挪 `put` / `put_stream` 的
理由是"本地非幂等写在取消后会留下半个文件"。一个 async 的对象存储 SDK 让这个理由
整条消失——那时该做的不是把写也塞进线程池，而是让那条路径根本不需要线程池。

**有人要给 `blocking_call_slots` 分进程调不同的值。** §5 承认了一个全局数字是
妥协。真的需要时，正确的形状不是分成四个字段，而是让**投影**在三个进程上给出
不同的默认——因为进程要挡什么是**部署拓扑**决定的，不是运维旋钮。

**embed 路径补上调用方超时。** §12.2 第 4 条写成了没做。做它需要一个新的数字，
而那个数字应当和 `application/retrieval.py:268` 的 `rerank_timeout_seconds` 放在
同一个地方、由同一条推理决定——不是再往 `[coordination]` 里加一个。
