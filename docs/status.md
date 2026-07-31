# 实施状态

## 2026-07-30 HITL Approval 收尾（未合并）

分支 `pr-050-postgres-checkpointer`，在下节 2026-07-29 快照之上再加四个提交
（`7014046`、`33ebbbb`、`a257e45`、`25895ca`），完成
[待办清单](./followup-checklist-2026-07-29.md) 的 **2.1 全部内容**。

| 落地 | 事实 |
|---|---|
| Graph interrupt | `approval` 节点真正 `interrupt()`；恢复时**回查账本**，不信任 resume payload |
| 拒绝路径 | `approval` 成为条件节点：approved → export，rejected → 终态失败（**不**导出） |
| Worker | reconciliation 第 5/6 分支由真实 interrupt 驱动；无账本时 park 而非猜测 |
| Approval API | `GET /v1/approvals/{id}`、`POST /v1/approvals/{id}/decisions`；跨 owner/tenant 一律 404 同正文 |
| 发现路径 | 新增 `TaskApprovalRequested` 事件进 Task timeline（无列举端点） |
| 唤醒 | `NOTIFY task_ready`，四处入队事务内发送，payload 只有 `task_id` |

统一门禁（真实 PostgreSQL `127.0.0.1:5433` + Qdrant `127.0.0.1:6333`）：

```text
ruff format --check .                 passed（304 files）
ruff check .                          passed
pyright                               0 errors / 0 warnings
alembic 唯一 head                     0018_approvals
pytest（真实服务）                    1569 passed / 11 skipped
pytest（无外部服务）                  1074 passed / 506 skipped
```

两行 pytest 是同一套测试的两种环境，不能相加。

破坏验证四轮共 34 处，第一轮抓住 31 处；三处漏网已按性质分类并补测，最终 34/34。
详情与分类见待办清单 2.1 条目。

**仍未做**：`task_ready` 的监听端（Worker 仍轮询，属 3.5 同批工作）；
`tool_executions` 副作用 ledger（2.2）。

## 2026-07-29 当前工作分支快照（未合并）

本节记录分支 **`pr-050-postgres-checkpointer` 自 `5d943af` 之后的 A–F 汇合增量**，
不是 `main` 的发布快照。下面的历史章节保留各增量当时的基线和证据；当前结果以
本节记录的统一门禁为准。

| 修复组 | 当前状态 | 已落地事实 | 仍需验证或补全 |
|---|---|---|---|
| **A：Task 工作流终态语义** | **完成** | 显式成功/失败 disposition；revision 预算计数；critic 拒绝且预算耗尽时失败关闭 | 统一回归已通过 |
| **B：Task 提交幂等与租户隔离** | **完成** | tenant-scoped 幂等键、输入 fingerprint、冲突复用已存身份；Task API 按 owner/tenant 隐藏越权资源 | 统一回归已通过 |
| **C：单 Worker 纵向切片** | **完成** | TaskInput Artifact、Task API/CLI、独立 `agent-task-worker` 入口、poll loop 与显式 demo composition | 生产身份仍未实现，入口只适合受控环境 |
| **D：真实 Task Agent handlers** | **主体完成并通过回归** | `plan`/`critic` 结构化处理、内部检索/evidence Artifact、TaskRunContext、取消与授权上下文装配；外部检索经过 Tool/Policy 边界 | 真实外部搜索 Provider 尚未实现；当前 Adapter 在 Provider 缺失时失败关闭 |
| **E：可靠 Task Core** | **主体完成并通过状态测试** | PostgreSQL `SKIP LOCKED` claim、lease/heartbeat/epoch、stale reclaim、retry/dead-letter、专用 advisory guard、fenced checkpointer、生命周期事件及确定性 failpoint | HITL 与外部副作用 ledger 属于后续工作，不在本组已完成范围 |
| **F：产品化补全** | **部分完成** | Qdrant 启动不变量、常驻摄取入口及 claim/heartbeat/fencing、Task 生命周期时间线、本机 Compose 演示拓扑 | HITL Approval、真实外部搜索、OTel/Langfuse、CrewAI 对比、UI、生产身份与生产部署仍未完成 |

当前汇合工作树已于 2026-07-29 通过统一门禁：

```text
ruff format --check .                 passed（287 files）
ruff check .                          passed
pyright                               0 errors / 0 warnings
agent-config-check                    development / test 均为 status=ok
docker compose config --quiet         passed
alembic 唯一 head                     0016_task_principal_scopes
pytest（无外部服务）                  1054 passed / 409 skipped
pytest（真实 PostgreSQL + Qdrant）    1452 passed / 11 skipped
```

两行 pytest 是同一套测试的两种环境，不能相加。真实状态测试使用 PostgreSQL
`127.0.0.1:5433` 和 Qdrant `127.0.0.1:6333`；11 项跳过中 10 项需要真实 BGE
embedding/sparse 权重，1 项是只适用于非锁定 recovery read 的契约变体。下文
2026-07-28 的门禁数字仍只属于其注明的历史提交。

## 文档基线

状态：**已纳入 Git 版本管理**。

- [架构与技术选型基线 v1.3](./architecture-baseline.md)；
- [代码实施计划 v1.0](./implementation-plan.md)；
- [配置管理契约 schema 1.2](./configuration.md)；
- [2026-07-25 仓库核验报告](./repository-audit-2026-07-25.md)；
- [2026-07-27 仓库复核报告](./repository-audit-2026-07-27.md)。

这些文档描述目标架构和增量计划，不代表其中列出的产品能力已经实现。

## 当前基线与编号对应

主分支基线：**`main@f5800d2`**（2026-07-28）。PR-047～PR-049 与后续 WP06
状态订正文档已全部合入。

**PR-035～PR-049 的全部增量都已经合入 `main`。** 下面各节里写的"尚未合入 `main`"
是当时开发分支上的状态，已按实际合并结果订正；每节保留的测试证据仍是**该增量当时**
的门禁数字，不是当前数字。

本文件的 `PR-0NN` 是**增量编号**——一个编号对应一次行为变化；GitHub 的 `#NN` 是
**合并编号**——一次合并可以携带多个增量。两套编号不相等，也不能互相推算：

| 文内增量 | GitHub 合并 | 提交 |
|---|---|---|
| PR-033 摄取 worker | `#49` | `4d03f69` |
| PR-034 `knowledge_search` Tool | `#50` | `de426e2` |
| PR-035 安全发布基线 | `#51` | `7025425` |
| PR-036 ～ PR-043 Chat 可靠性 | `#52` | `07f4a27` |
| PR-044 Task 工作流状态 | `#53` | `3538e26` |
| PR-045 Reranker | `#54` | `3b7829b` |
| PR-046 Sparse 加载守卫 | `#55` | `e93d7a1` |
| 文档基线订正 | `#56` | `260ca0e` |
| PR-047 控制流与 fan-in reducer | `#60` | `4ec04d2` |
| PR-048 Agent node | `#58` | `0830e55` |
| PR-049 LangGraph adapter | `#59` | `341cbf5` |

### 2026-07-28 全仓门禁复核（`main@341cbf5`）

```text
ruff format --check .    passed（214 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
agent-config-check       development / test / production 均为 status=ok
alembic 唯一 head        0009_chat_turn_lease

pytest（无外部服务）              907 passed / 260 skipped
pytest（真实 PostgreSQL + Qdrant） 1156 passed /  11 skipped
```

**两行 pytest 是同一套测试的两种环境，不能相加。** 第二行是本项目第一次真正跑通
需要外部服务的那 260 项——它们全部通过。此前每一轮增量都只能写"真实 PostgreSQL
用例因未配置 DSN 而跳过"，也就是说 lease、`SKIP LOCKED` 回收、原子发布、迁移这些
不变量一直靠**跳过的测试**撑着。现在它们是实测过的。

剩下的 11 项跳过全部需要真实 BGE 权重
（`AGENT_WORKBENCH_TEST_EMBEDDING_MODEL` 未设置）。

复现方式（PostgreSQL 镜像 digest 与 CI 固定的一致）：

```bash
docker run -d --name aw-postgres -p 5433:5432 \
  -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=ci-only \
  -e POSTGRES_DB=agent_workbench_test \
  postgres:16@sha256:33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20
export AGENT_WORKBENCH_TEST_DSN="postgresql+asyncpg://agent:ci-only@127.0.0.1:5433/agent_workbench_test"
export AGENT_WORKBENCH_TEST_QDRANT_URL="http://localhost:6333"
alembic upgrade head && pytest -q
```

容器映射到 **5433** 而不是 5432：开发机上可能已经跑着原生 PostgreSQL，它会遮蔽
Docker 的端口映射，症状是 `role "agent" does not exist`——一个看起来像配置错误、
实际是端口被占的失败。

此前记录的 1 项 deselect 是当时沙箱禁止 `socket.bind()` 所致，属于**环境差别而不是
代码变化**；本轮环境允许该调用，loopback 真实性测试正常执行并通过。

`production` profile 需要 CI 同款环境变量（固定 model ID、40 位 embedding/reranker
revision、DeepSeek 与 Qdrant 密钥）才能通过；只用 `.env.example` 会在 Qdrant 密钥
这一项失败关闭，这是配置契约的预期行为。

## 2026-07-28 PR-058 WP07-03：Task 提交时的语义与授权身份落库

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。迁移 `0012`。

`task_runs` 增加五列：`run_semantics_snapshot`、`run_semantics_revision`、
`submitted_policy_revision`、`submitted_policy_fingerprint`、
`submitted_authorization_envelope`。确定性快照的机制**此前已在 settings 层实现**
（`run_semantics_snapshot()` / `task_run_semantics_revision()` / `policy_identity()`），
本轮做的是把它**持久化**并让提交路径带上它。

### 快照与 policy 身份分开存

快照是**恢复时沿用**的东西；policy **每次 claim、每次 dispatch 都重新求值**。两列 policy
身份只记录"调用方当时是在哪套规则下被授权的"，因此有效授权始终是
"提交时的 envelope ∩ 当前规则"，而不是"当时允许什么就一直允许什么"。

envelope 读回来是**模型**而不是裸 dict：一个当成 JSON 读的授权上限，是没有任何东西会再校验
一遍的上限。

### 三处"不能是可选"的地方

**`TaskSubmission` 的这些字段是必填的。** 一个可以省略语义的提交会产出一个"恢复时无物可恢复"
的 Task，而这个疏漏只会在恢复的那一刻才被发现。

**列是 NOT NULL 且无默认值**，所以迁移在表里已有行时会直接失败。这是故意的：在这些列存在之前
提交的 Task 没有"提交时语义"，回填一个占位符等于**编造**一份——而恢复时会把编造的那份当真。

**`semantics` 是注入的 callable 而不是一个值**：涉及知识库的 Task 要在**每次提交**时解析
Qdrant alias（WP07-04），不是启动时解析一次。

### 幂等的判定范围跟着变宽

同一个 dedup key 配**不同的语义修订或不同的 policy 指纹**，现在是
`TaskSubmissionConflictError`。否则调用方的工作会在它从未要求过的语义下运行——正是快照要防的
那个失败，只不过是从幂等路径而不是从恢复路径进来的。

### 有牙验证

4 处破坏。其中**两处是我自己写坏的空操作**（改了等价代码 / 加了个 `# noqa`），不算发现；
如实记下来是因为"破坏了但测试没红"如果不追究，就会被当成覆盖率证明。真正的两处：

| 破坏 | 结果 |
|---|---|
| 语义不算作"提交是什么"的一部分 | 2 条测试失败 |
| 快照列改成 nullable | 迁移漂移测试 2 条失败（第一轮漏跑，因为它不在我选的目标文件里） |

本轮门禁：

```text
ruff check / format      passed（235 files）
pyright                  0 errors / 0 warnings
alembic 唯一 head        0012_task_submitted_semantics
pytest（无外部服务）              955 passed / 363 skipped
pytest（真实 PostgreSQL + Qdrant） 1307 passed /  11 skipped
```

### 本轮明确未做

- **没有任何地方从 settings 构造 `SubmittedSemantics`**：服务收一个 callable，组装点仍不存在；
- Qdrant alias 解析与 generation reservation（WP07-04）没做，所以三个 `resolved_*` 列还没有；
- 快照内容本身没有新增校验（"与两个 resolved 字段不一致时 fail closed"要等 WP07-04）。

## 2026-07-28 PR-057 WP07 开始：Task 与它的开场事件同事务提交

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP07-02 的第一半，
也是 WP07 的第一条退出条件：**状态和事件同事务提交**。

新增一个事件类型 `TaskSubmitted`，`PostgresTaskRegistry.submit` 用**同一个连接、同一个事务**
写入 `task_runs` 行和这条事件。二者要么一起提交，要么一起回滚——不会存在"有 Task 但没有任何
东西说明它为什么存在"，也不会存在"事件描述了一个被回滚掉的 Task"。

事件只带**提交决定了什么**，不带它引用的东西：objective 在 `input_ref` 后面。事件会被重放进
时间线和 SSE 帧，调用方提交的正文没有理由在那里被复述一遍。

日志是**默认构造**而不是可选注入：没有办法造出一个"跳过事件"的 registry，只能换一个日志。

### 两处被测试逼出来的修正

**冲突检查必须在 append 之前。** 同一个 dedup key 配不同请求，本来会先撞上事件的
`event_key` 幂等校验，报成 `EventKeyConflictError`——一个关于本项目**自己记账方式**的错误，
扔给一个只是犯了普通错误的调用方。挪到 append 前之后，它是 `TaskSubmissionConflictError`。

**时间线不再有"空"这个状态。** 一个刚开出来的 Task，时间线上已经有它自己的开场事件了——
因为二者同事务。相关测试从"没有事件"改成断言**第一条就是 `TaskSubmitted`**。

### 有牙验证：6 处破坏，5 处被抓住

| 破坏 | 失败测试数 |
|---|---|
| 事件不带幂等 key | 1 |
| 冲突检查挪到 append 之后 | 2 |
| 事件写进另一条流 | 6 |
| 干脆不写事件 | 11 |
| `TaskSubmitted` 改成 transient | 38 |
| **append 改成自己开事务** | **0** |

最后一行是**没有修掉的**，如实记下来：`PostgresEventLog.append` 内部就是
`begin()` + `append_durable_in_transaction`，所以从外部看，"用调用方的事务"和"自己开一个"
在 append 那一刻**完全一样**——两者此时都还没提交。要区分只能在 `submit` 里加一个仅供测试
存在的接缝。没有加。改为覆盖它确实成立的两条性质：**append 失败则 Task 不存在**，
以及**重复提交不追加第二条事件**；再加一条从**第二个连接**观察、断言事务外看不到该事件。

本轮门禁：

```text
ruff check / format      passed
pyright                  0 errors / 0 warnings
pytest（无外部服务）              955 passed / 359 skipped
pytest（真实 PostgreSQL + Qdrant） 1303 passed /  11 skipped
```

### WP07 剩下的

WP07-01（仓储与状态机）与 WP07-05/06（`events`/`event_streams`、per-stream sequence、
cursor codec）此前已经落地。仍未做：WP07-02 的 `NOTIFY task_ready` 与输入存储、
WP07-03 语义快照与 submitted policy identity、WP07-04/08 Qdrant generation reservation
与 Task-aware GC、WP07-07 其余 durable 事件。

## 2026-07-28 PR-056 统一事件时间线：WP06 最后一条退出条件

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-09。
`TaskService.timeline()` 按游标切片返回一个 Task 的事件。

### "统一"是指**一条流**

一个 Task 的事件全部落在**它自己的 workflow thread** 这一条流上，所以
`(stream_id, sequence)` 一个游标就等于"这个 Task 到此为止的全部"，重连的客户端也只回传
一个值。Task 与 thread 是一对一——Registry 的唯一约束**双向**保证——这正是让单游标成立的
前提。流 ID 的推导只写在 `task_stream_id()` **一个地方**，写入方和读取方不可能对"事件去哪了"
产生分歧；把它改成所有 Task 共用一条流会让测试失败。

### 三个不显眼但要紧的地方

**授权检查在读日志之前，且用的是同一条。** 时间线如果对别人的 Task 给出不同答案，泄露的恰好
是 Task 查询拒绝泄露的那件事——这个 id 存在。所以它先走 `get()`，两者同样是 `NotFoundError`。

**外来游标是拒绝，不是忽略。** 游标是客户端提供的值；忽略它等于对一个"想接着看另一条流"的
客户端，从头把**这个** Task 的历史端上去。

**空切片不推进游标。** 返回流末尾会跳过"这次读和下次读之间到达"的事件；没投递出去，位置就
没有移动。

### 有牙验证里的又一个诚实结果

7 处破坏，第一轮 6 处。漏网的是"**limit 不设上限**"——原测试传了一个超大 limit 然后断言
"返回 3 条"，可库里本来就只有 3 条，**上限有没有生效根本看不出来**。

它没有靠"多存 500 条事件"来修，而是搬到了能观察的地方：用一个**记录被要求了什么**的假日志，
直接断言服务传给日志的 limit 是被夹住之后的值。PostgreSQL 那侧只保留"limit=0 被拒绝"。
补完后 7 处全部被抓住。

| 破坏 | 失败测试数 |
|---|---|
| 先读日志再做授权 | 1 |
| 忽略外来游标 | 1 |
| 游标不推进 | 3 |
| 空切片把游标推到流末尾 | 2 |
| limit 不设上限 | **第一轮 0**，改测试后 1 |
| 没有日志时返回空时间线 | 1 |
| 所有 Task 共用一条流 | 1 |

本轮门禁：

```text
ruff format --check .    passed（234 files）
ruff check .             passed
pyright                  0 errors / 0 warnings

pytest（无外部服务）              955 passed / 353 skipped
pytest（真实 PostgreSQL + Qdrant） 1297 passed /  11 skipped
```

### WP06 退出条件

**全部满足。** 事件时间线这一条按计划的说法"M3a 可用内存/测试 EventLog，WP07 替换为
PostgreSQL durable EventLog，不改变接口"——本轮直接用了已经存在的 `PostgresEventLog`，
所以那次替换已经不需要了；接口是 `EventLogPort`，两种实现同一个口子。

### 本轮明确未做

- **没有 HTTP/CLI 入口**：时间线只有服务方法，`event_stream.replay_source` 那套 SSE
  与 `Last-Event-ID` 是 WP09；
- Worker 目前**不往这条流里写**任何东西：写入的是 Agent 节点的 `EventSink`，而组装
  Worker → 节点 → sink 的那个组装点还不存在（WP06 之后）。所以真实运行里这条流现在是空的，
  测试用真实 `EventLogPort` 追加的是 Agent 运行会产生的同一种事件；
- Task 生命周期本身还没有事件类型（`TaskSubmitted` 等）：`EventPayload` 是封闭联合，
  加成员要动 schema 版本，属于 WP07-07。

## 2026-07-28 PR-055 TaskService 与可切换的 FakeExecutor

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。
补完 WP06-07 的 TaskService/查询接口，并完成 WP06-08。

### TaskService：两件仓储**故意不做**的事

**thread_id 与 graph_version 由它铸造，不由调用方给。** 仓储收下这两个字段是因为它们是行上的
事实；**选**它们是决定。让调用方给 thread，重试就可能为同一个 dedup key 给出**不同**的 thread，
唯一约束会直接拒绝——本该幂等的重试变成报错。让调用方给版本，客户端就能把自己钉在一个没人再部署
的图上。

**按 id 读是一条授权边界。** "不存在"和"不是你的"必须给出**同一个**答案——答案不同本身就是泄露，
它确认了这个 id 存在。所以两者都是 `NotFoundError`，没有 "forbidden"。归属**两项都查**：只查
tenant 会把一个租户的 Task 暴露给租户内每个人；只查 owner 会让跨租户的同名 id 撞进别人的 Task。
有一条测试直接断言两种拒绝的**类型、code 与消息逐字相同**。

### WP06-08：切换在节点注入，不在配置

M3a 验收门槛是"**单个 Agent 节点**可切换 FakeExecutor/自研 Runtime"。本轮把
`FakeAgentExecutor` 作为**正式代码**发布（不再只是测试替身），因为它换来的东西不是测试：整条
Task 链路——提交、领取、跑图、checkpoint、崩溃后恢复——可以在**没有 provider、没有 key、没有
费用**的情况下端到端跑完。

**没有**去放宽 `runtime.executor`。那个单值 `Literal` 编码的是一条已冻结的不变量——
**Tool Loop 只有一个所有者**。加一个 `"fake"` 值等于推翻它，而按项目规矩那需要先写 ADR、
升 config schema 版本，不是顺手加个开关。有一条测试**正面钉住**这一点：
`RuntimeSettings.model_validate({"executor": "fake"})` 必须失败。

fake 只做边界真正承诺的两件事：**返回终态结果**、**观察取消**。取消返回 `cancelled` 结果而不是
抛异常——调用方是个无论如何都要记录和路由的图节点，这正是只走happy path 的替身最容易漏掉的一条。
其余全部是请求的函数：同一请求给同一结果（CI 不依赖模型），不同请求给**不同的 sha256**
（常量摘要会让任何做去重的东西把两次无关的运行当成同一次），并且**记账不为零**——一次不花钱的运行
会让所有预算检查静默通过。

### 有牙验证

9 处破坏，全部被抓住，0 处漏网：

| 破坏 | 失败测试数 |
|---|---|
| 读取时忽略 tenant / 忽略 owner | 1 / 2 |
| "不是你的"与"不存在"给不同答案 | 1 |
| thread_id 跨提交复用 | 1 |
| graph_version 不来自服务的决定 | 1 |
| fake 取消时抛异常而不是返回 | 1 |
| 每个 fake artifact 同一个摘要 | 1 |
| fake 运行不记账 | 1 |
| fake 忽略脚本化的回答 | 2 |

本轮门禁：

```text
ruff format --check .    passed（233 files）
ruff check .             passed
pyright                  0 errors / 0 warnings

pytest（无外部服务）              953 passed / 344 skipped
pytest（真实 PostgreSQL + Qdrant） 1286 passed /  11 skipped
```

新增 15 条测试，**全部不需要外部服务**。

### 本轮明确未做

- **WP06-09 事件时间线**：Task 查询目前返回 Task 本身，不返回统一事件时间线；
  那是 WP06 最后一条未满足的退出条件；
- TaskService 不写入 `input_ref` 指向的东西——提交事务与输入存储是 WP07-02；
- 没有 HTTP/CLI 入口调用它；组装点仍然不存在。

## 2026-07-28 PR-054 单 Worker runner：整条链第一次跑通

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-07 的第四步。
`TaskWorker` 把仓储、恢复判定和工作流适配器接起来。

### 它自己不做任何决定

领任务是仓储的条件更新，判断两个事实合起来是什么意思是 `reconcile`，跑图是适配器的。
这里只剩把它们连起来的循环，以及**什么时候停**。

**"跑图"被表达成"再判断一次"**，而不是第二套状态机。Worker 启动一张图之后再问同一个问题：
位置变了，答案就跟着变——从 `start` 变成 `settle_succeeded` 或 `wait_for_approval`。
在 `ainvoke` 后面直接写"然后标记成功"，就是把恢复判定又抄了一遍措辞不同的版本，而改动原版之后
还在跑的正是那份抄件。测试直接断言一次成功运行产生的 action 序列是
`["start", "settle_succeeded"]`——**两次判断，不是一次**。

循环**有上界**（3 次）。既不结束也不等待的图，Worker 结算不了；对它无限循环比如实记下来更糟，
所以预算用尽就进 `failed`。

每轮都**重新读 Registry**，不信任领取时那一行：图跑着的时候落下来的取消，正是判定的第一个分支
要看到的事实。

### 端口多了一个 `inspect`

判定需要"这个 thread 停在哪"，而**不能靠试着 resume 再读异常**。所以
`TaskWorkflowPort` 增加 `inspect(thread_id) -> CheckpointPosition | None`，
`CheckpointPosition` 也从 application 移到端口——端口不能反向依赖 application，而"图停在哪"
本来就是这个边界的词汇。

适配器实现里有一处**诚实的取舍**：checkpoint 的版本若未记录或本进程建不出来，就**只报版本、
不报待执行节点**——待执行节点是 LangGraph 对那张图的计算，要拿它就得先编译那张图。文档写明
这不含糊：这种位置在任何人读它的待执行节点之前就已经被判定 park 了。

### 有牙验证：11 处破坏，第一轮 10 处

漏网的那一处是**"inspect 把建不出来的 checkpoint 报成当前图的版本"**——没有测试失败，因为
现有用例里"Registry 的版本建不出来"总是先一步挡住，从来没走到"Registry 版本没问题、
但**checkpoint** 是另一张建不出来的图写的"。

补的测试构造了这个状态：先用一个**有** v9 的 Worker 把它跑起来（checkpoint 记下 v9），
再把 Registry 的版本改成 v1 并重新排队，然后交给一个**没有** v9 的 Worker。这时若谎报成 v1，
一个读不出来的 checkpoint 会显得"已完成"，Task 被结算成 succeeded——补上后 11 处全部被抓住。

| 破坏 | 失败测试数 |
|---|---|
| 信任领取时的行，不重新读 Registry | 1 |
| 只判断一次，什么都不结算 | 3 |
| 判断次数无上界 | 1 |
| 抛异常的图被留在 running | 2 |
| 失败原因带上 provider 的异常正文 | 1 |
| park 时不带原因 | 2 |
| 已终态的 Task 再结算一次 | 1 |
| `start` 与 `resume` 对调 | 4 |
| `inspect` 报告没有 checkpoint | 6 |
| `inspect` 报告它没读过的待执行节点 | 2 |
| `inspect` 谎报建不出来的 checkpoint 的版本 | **第一轮 0**，补测试后 1 |

### 整条链的证据

第一个 Worker 死在 `critic`，Task 进 `failed`；把它的 engine、saver、workflow、registry、
handler 闭包**全部丢弃**；重新排队后，一个**从零构建**的 Worker 领走它并跑完——
action 序列是 `["resume", "settle_succeeded"]`（不是 `start`），第二个进程里
`understand` 与 `research_internal` 的调用次数是 **0**。

（重新排队这一步是测试手工做的：按 lease 过期自动重排是 WP08 的 reaper，这条测试关心的是
之后发生什么，不是谁触发的。）

失败原因只记**异常类型**不记异常正文：provider 的异常文本里有请求体和 prompt 片段，而这个字符串
会进事件和 API 响应。有测试断言 `"RuntimeError" in detail` 且 `"died mid-run" not in detail`。

本轮门禁：

```text
ruff format --check .    passed（229 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0011_task_runs

pytest（无外部服务）              938 passed / 344 skipped
pytest（真实 PostgreSQL + Qdrant） 1271 passed /  11 skipped
```

### 本轮明确未做

- **没有轮询循环、没有 `LISTEN/NOTIFY`**：只有 `run_once()`。什么时候再调一次是调用方的事，
  长驻循环与唤醒属于协调；
- **没有 lease、没有 advisory lock、没有 fencing**，所以这个 Worker **只能跑一个**。多 Worker
  是 WP08，`FencedCheckpointer` 也在那里；
- `load_state` 是注入的 callable 而不是端口：`input_ref` 指向什么由提交事务（WP07-02）决定，
  在这里发明一个存储就是替那次改动先做了决定；
- TaskService 与查询接口（含统一事件时间线，WP06-09）还没有；
- `approval_decision` 恒为 `None`——本版没有任何图能中断，审批是 WP10。

## 2026-07-28 PR-053 Task Registry 仓储：状态机是数据，SQL 由它推导

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-07 的第三步。
`TaskRegistry` 端口 + `PostgresTaskRegistry`。转换全部是**条件 UPDATE**，`WHERE` 里的
合法来源状态**从领域的转换表推导**（`sources_for`），不在 SQL 里重写一遍——规则写两遍，
留下来跑的总是没被改的那一遍。

实施计划 §7.4 的"接口层不能直接写状态字符串"落实为：方法按**发生了什么**命名
（`mark_succeeded` / `park_for_migration` / `await_approval` / `cancel`），没有
`transition(to=...)` 这种把状态字符串又还给调用方的口子。

`ALLOWED_TRANSITIONS` 里终态**没有任何出边**——"迟到的审批不能复活已取消的 Task"落到实处
就是这一条。`waiting_migration` 同样没有出边：计划里没写谁执行迁移、怎么执行，凭空加一条边
就是发明一个没人设计过的流程。这两点都有测试钉住。

### 破坏验证发现的三件事，两件是真缺陷

第一轮 11 处破坏只抓住 8 处。三处漏网各自的结论不同，都不是"补个断言"能了事的：

**一、`start_next` 的 `status = 'queued'` 条件被删掉，没有测试失败。**
写代码时的注释说"单 Worker 下它和子查询等价"。**实测证明这句话是错的**：让另一个事务先把该行
改成 `running` 但不提交，再跑 claim——

```text
带 status 条件：返回 None
去掉 status 条件：返回 task_x   ← 同一个 Task 被派发了两次
```

PostgreSQL 在行被并发更新后会**重新校验 UPDATE 的限定条件**，但**不会重跑限定条件里的
子查询**，于是 `task_id = (SELECT ... WHERE status='queued')` 仍然匹配一个已经不是 queued
的行。这个条件是唯一挡住重复派发的东西。注释已按实测改写，并补了对应测试。

**二、`submit` 改成"先读后插"，没有测试失败。**原来的并发测试用 `asyncio.gather` 起 5 个
提交，实测它们**确实并发**（全部开始早于任何一个结束），但在没有屏障的情况下几乎总是自然串行，
race 根本没发生。改成**确定性**写法：另一个事务先插入冲突行且不提交，`submit` 会阻塞在唯一
索引上，提交后它只能走"输掉"的那条分支。此时——

```text
ON CONFLICT DO NOTHING：返回赢家的 Task
先读后插：            IntegrityError
```

（用强制屏障单独验过一次：5 个"先读后插"并发，4 个 IntegrityError。）

**三、`_move` 里"该带原因/不该带原因"的检查删掉后没有测试失败——因为它根本到不了。**
五个公开方法的**签名**已经决定了要不要 `reason`，多传或少传都是 `TypeError`。那段检查是
死代码，删掉了。

换上的是一个**真的够得着**的检查：`reason=""`。空串既满足列的 `NOT NULL`，也满足
`status_detail` 的 CHECK，于是**写得进去、读不回来**——`TaskRun.status_detail` 要求非空。
这是失败最糟糕的形状。现在用 `TaskRun` 同一个类型去校验，两边不可能各说各话；删掉这个检查会让
2 条测试失败。

补完之后 11 处破坏全部被抓住，0 处漏网。

本轮门禁：

```text
ruff format --check .    passed（227 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0011_task_runs

pytest（无外部服务）              938 passed / 334 skipped
pytest（真实 PostgreSQL + Qdrant） 1261 passed /  11 skipped
```

### 本轮明确未做

- Worker 还没有；仓储只是它要用的东西；
- 没有 lease、没有 `SKIP LOCKED`、没有 advisory lock、没有 priority——多 Worker 是 WP08。
  但上面那条"重复派发"的实测结论已经预先钉住了 WP08 里最容易写错的地方；
- `waiting_migration` 与 `waiting_approval` 的出边留给各自的流程（迁移程序、WP10 审批）。

## 2026-07-28 PR-052 Task Registry 的行，以及一处订正

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-07 的第二步：
`task_runs` 的产品生命周期，schema 与约束。仓储与 Worker 还没有。

### 订正：状态少了一个

[PR-051](#2026-07-28-pr-051-worker-的恢复判定基线-95-的七种情形) 的
`TaskStatus` 写了七个状态，并声称"这是基线点过名的全部"。**实施计划 §7.4 列的是八个**，
多一个 `dead_letter`（重试预算耗尽的毒任务）。本轮补上，并归入终态——它的作用正是让毒任务
不再被反复领取。`reconcile` 不需要改：终态分支本来就按集合判断，全枚举测试也自动覆盖到它。

### 这一轮**不**加的列

按实施计划 §7.2 的分工，各自留给自己的工作包：lease、epoch、attempt、`available_at`
退避与 recovery reason 属于协调（WP08），而**单 Worker 一个都不需要**；run semantics
快照与 submitted policy identity 是 WP07-03；resolved Qdrant collection / index version /
generation reservation 是 WP07-04。分开落是本仓已有的做法——`0008_chat_turns` 落 Turn，
`0009_chat_turn_lease` 才落它的执行 lease。

### 两条硬约束

**一个 owner 的一个 submission key 不能开出第二个 Task。** "重复 submission key 返回同一
Task"是退出条件；*返回*同一个是仓储的事，*不可能存在第二个*是这条唯一约束的事。它按 owner
分域：全局唯一会让一个用户选的 key 拒绝掉另一个用户的提交，而且会告诉对方这件事发生了。

**`thread_id` 双向唯一。** 一个 Task 对应恰好一个 thread，一个 thread 背后恰好一个 Task，
因此 reconciliation 永远不会拿到"一个 checkpoint 对两行 Registry"。

`status_detail` 的约束是**双向**的：`waiting_migration` / `failed` / `cancelled` /
`dead_letter` 必须带原因，其余四个状态必须不带。只做前一半的话，一段解释会活过让它失效的那次
转换，然后被当成一个其实没问题的 Task 的当前说明来读。

### 有牙验证里的一个诚实结果

7 处破坏，**6 处被抓住，1 处没有**——而那 1 处是有意义的：单独删掉状态词汇表的 CHECK，
**没有任何测试失败**。原因是 `status_detail` 那条 CHECK 把状态配对成两组，一个未知状态两组
都不属于，于是它自己就把行挡下来了。也就是说词汇表 CHECK 在可观测行为上是**冗余**的。

它仍然保留：等哪天 `status_detail` 规则被放宽，词汇表就会在**没人察觉**的情况下失去约束。
这个结论写进了那条测试的文档，测试本身也改成同时验证"带原因"和"不带原因"两种未知状态写法，
而不是继续声称自己在验证那条已被覆盖的 CHECK。

| 破坏 | 失败测试数 |
|---|---|
| 单独删掉状态词汇表 CHECK | **0（冗余，已记录）** |
| 数据库词汇表里少 `dead_letter` | 1 |
| 需要人处理的状态可以不带原因 | 10 |
| 不需要人处理的状态可以留着原因 | 5 |
| 同一 owner 可以重复提交同一 key | 1 |
| submission key 改成全局唯一 | 1 |
| 两个 Task 共用一个 thread | 1 |

本轮门禁：

```text
ruff format --check .    passed（224 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0011_task_runs

pytest（无外部服务）              937 passed / 307 skipped
pytest（真实 PostgreSQL + Qdrant） 1233 passed /  11 skipped
```

## 2026-07-28 PR-051 Worker 的恢复判定：基线 §9.5 的七种情形

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-07 的第一步。

Task Registry 表示产品生命周期，LangGraph checkpoint 表示图执行位置；本项目不把两者
伪装成一个分布式事务，所以"领到一个 Task"就等于要判断**这两个事实合起来是什么意思**。
架构基线 §9.5 把这个判断列成了七种情形，本轮把它写成一个**全函数**。

### 为什么是纯函数

将来调用它的 Worker 要同时持有 advisory lock、lease 和 guard 连接，这七个分支里的每一个
经由真 Worker 去够都很贵——"图停在一个后来被拒绝的审批上"这种情形，要 lease、要锁、要一张
能 interrupt 的图，跑完还只覆盖七分之一。写成对值的判断后，全部分支微秒级可达，Worker
那边只剩真正需要 I/O 的部分。

这和本仓已有的做法一致：`workflows/research_graph.py`（纯声明）先落，
`adapters/langgraph/workflow.py`（执行）后落。

### 判定顺序不是随意的

终态**先于**读 checkpoint 判断：一个已取消、但 checkpoint 里还有待执行 node 的 Task，
不是"还没跑完的活"，是"被人停下的活"。反过来先读 checkpoint 就会把它变回 running。

`waiting_migration` **必须显式挡住**，不能落到后面：它不是终态，它的 checkpoint 看起来
完全可恢复，只读 checkpoint 会把它直接放回去跑，撤销掉别人正等着做的那个决定。

版本比较有两半，都进 `waiting_migration`：**本进程根本没有这张图**（未注册），和
**checkpoint 是另一张图写的**。第三种情况是 checkpoint **没记录**版本——它不比"版本不一致"
更可恢复，猜"大概就是 Task 注册的那个版本"是在最贵的地方做猜测。这一条正好接上
[PR-050（三）](#2026-07-28-pr-050三workflow-身份进-checkpoint端口层跨进程续跑)：
版本现在写在 checkpoint metadata 里，所以这个判断**在调用 resume 之前**就能做出，
而不是去 catch 一个异常。

"图已结束"与"停在审批"两条**互斥而不只是有序**：`CheckpointPosition` 拒绝被构造成
"没有待执行 node 却在等审批"，所以两个分支谁也藏不住谁。

### 一个诚实的说明

七种情形里有两种（停在审批、审批已有决定）描述的是 **M3a 还产生不出来的图**——`approval`
在 WP10 之前是无副作用占位节点。仍然实现它们，是因为另一条路是"对一张被中断的图静默回答
resume"，而且输入结构为了让其它分支成立本来就得携带审批信息。这一点写在模块文档里。

### 有牙验证

11 处破坏，全部被抓住，0 处漏网。其中最值得记的一条：把 `waiting_migration` 的显式分支
去掉后**只有 1 条测试失败**——而那条测试是做破坏验证之前**补上**的。第一版测试里
"totality" 那条只断言"每种输入都得到某个 action"，`waiting_migration` 落到 `resume`
一样满足它。全枚举不等于全断言。

| 破坏 | 失败测试数 |
|---|---|
| 终态检查挪到读 checkpoint 之后 | 5 |
| `waiting_migration` 落到后面 | 1 |
| 未注册的 graph version 照跑 | 1 |
| 没记录版本的 checkpoint 当作匹配 | 1 |
| 版本不一致照样 resume | 1 |
| 已结束的图去 resume 而不是结算 | 2 |
| 忽略审批决定 | 3 |
| 等审批时仍占住 Worker | 1 |
| `wait_for_migration` 不再改状态 | 2 |
| 允许"已结束且在等审批" | 1 |
| 决定里丢掉 approval id | 1 |

本轮门禁：

```text
ruff format --check .    passed（222 files）
ruff check .             passed
pyright                  0 errors / 0 warnings

pytest（无外部服务）              935 passed / 291 skipped
pytest（真实 PostgreSQL + Qdrant） 1215 passed /  11 skipped
```

新增 19 条测试，**全部不需要外部服务**，所以两列的 `passed` 同步 +19、`skipped` 不变。

### 本轮明确未做

- **没有 Worker 调用它**。claim/lease/advisory lock、`task_runs` 仓储与状态机
  （WP07-01）、TaskService 与查询接口都还没有；
- `TaskStatus` 只定义了基线点过名的七个状态，没有 `task_runs` 表、没有列、没有仓储。
  这里定义的是**词汇表和判定**，不是存储；
- 判定不涉及 Qdrant generation reservation（WP07-04）与取消传播的具体事件写入。

## 2026-07-28 PR-050（三）workflow 身份进 checkpoint，端口层跨进程续跑

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-06 的最后一步。
`LangGraphTaskWorkflow` 不再持有 `_thread_versions` 字典；"这个 thread 存在吗"和
"它是哪张图写的"两个问题都改成**问 checkpointer**。

### graph version 存哪里：选了第三个位置

三个候选：`task_runs.graph_version`（实施计划 §6，属 WP06-07）、adapter 自己的一张表、
或者**checkpoint 的 metadata**。选第三个。

机制是契约自带的：`get_checkpoint_metadata` 会把 `config["configurable"]` 上的标量
拷进每一个 checkpoint 的 metadata。所以 adapter 只要在调用图时把
`{"configurable": {"thread_id": ..., "graph_version": ...}}` 传下去，版本就自动、逐个
checkpoint 地持久化了——不需要 adapter 自己的表，也没有第二处需要保持同步的写入。

**这不是把 Task 产品状态复制过来。** `task_runs.graph_version` 记的是"这个 Task 被要求
用哪张图跑"；这里记的是"实际写下这个执行位置的是哪张图"。ADR-014 禁止的是 checkpointer
成为前者的第二个 writer，而后者本来就属于架构基线 §9 说的"图执行位置"。

代价也记下来：PR-049 时 `workflow.py` 里写着"LangGraph 的 checkpoint 不携带 graph
version"。**那句话现在不成立了**，因为这次让它携带了。

### 没记录版本的 checkpoint 拒绝续跑

不是本 adapter 写的 checkpoint（本次改动之前的行，或者绕过 adapter 直接驱动的图）
metadata 里没有 `graph_version`。这时 `resume` **失败关闭**，报出的
`checkpoint_graph_version` 是 `<unrecorded>`——一个 `GraphVersion` 的正则
（必须以字母数字开头）**永远匹配不上**的字符串，所以调用方拿它和任何真实版本比较都得到
"不同"，而这正是答案。猜"大概就是唯一注册的那个版本"是在最贵的时刻做的猜测。

版本比较**发生在编译图之前**：否则一个由本进程已不再注册的版本写的 checkpoint，会被报成
"未知版本"而不是"它由 v1 写的"。

`run` 的存在性检查也改成问 checkpoint，因此**另一个进程起过的 thread 不再是空闲的**。
它是检查不是锁：两个 first run 抢同一个 `thread_id` 由 Task lease（WP08）排除，不在这里。

### 有牙验证

6 处破坏，全部被抓住，0 处漏网：

| 破坏 | 失败测试数 |
|---|---|
| 版本不放进 config，于是没有 checkpoint 记录它 | 7 |
| 没记录版本时按调用方要的版本当作已记录 | 1 |
| `run` 不问 thread 是否已存在 | 3 |
| `resume` 完全不比较版本 | 4 |
| 版本比较挪到编译图之后 | 2 |
| `resume` 又去信任对象内存而不是 checkpoint | 6 |

端口层的关键证据：第一个进程死在 `critic`，随后 adapter、saver、engine、连接池、
handler 闭包**全部丢弃**，第二个进程只拿到 `thread_id` 和 graph version，
`resume` 跑完全程——`understand` 与 `research_internal` 在第二次运行里调用次数为 **0**，
返回的 `TaskWorkflowResult.state` 带着上一个进程采集的证据。

另有一条测试直接对 `workflow_checkpoints.metadata` 做 JSONB 谓词查询，按 graph version
选出 thread，并断言**没有任何 checkpoint 是未记录版本的**——这是把 metadata 存成 JSONB
而不是不透明字节换来的东西。

本轮门禁：

```text
ruff format --check .    passed（219 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0010_workflow_checkpoints

pytest（无外部服务）              916 passed / 291 skipped
pytest（真实 PostgreSQL + Qdrant） 1196 passed /  11 skipped
```

### 本轮明确未做

- **仍然没有组装点构造 `PostgresCheckpointSaver`。** adapter 的默认值依旧是
  `InMemorySaver`（含义现在是明确的："只在本进程内有效"），要跨进程的调用方自己传一个
  持久 saver 进来。真正的装配点是 WP06-07 的 Task Worker；
- `adelete_thread` 与 checkpoint 保留策略仍未做；
- 两个进程同时对一个 thread 调 `run` 只被"检查"挡住，不被"锁"挡住。

## 2026-07-28 PR-050（二）saver 本身：跨进程恢复第一次有了证据

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。对应 WP06-06。
`PostgresCheckpointSaver` 实现 `BaseCheckpointSaver` 的四个异步方法，写进
[PR-050（一）](#2026-07-28-pr-050一checkpointer-的表结构)落的三张表。

### 落在哪一层

文件是 `adapters/langgraph/checkpointer.py`，不是 `adapters/persistence/`。表可以住在
persistence 里——它们只是 SQL，而且必须和其它表共用一份 metadata，Alembic 漂移测试才
看得见它们；但 saver 本身**依赖 langgraph**，放进 persistence 就等于让持久化层依赖一个
工作流框架。`adapters/langgraph/` 仍然是"唯一允许 import langgraph 的包"。

### 三个值得记下来的决定

**一次事务写完 checkpoint 和它的 blob。** blob 只写了一半的 checkpoint 会恢复出一个
少了通道的状态，而且恰好在恢复的时候恢复出来——那是最没人盯着的时刻。

**`aput_writes` 有两条冲突规则，不是一条。** 普通写入**先到为准**：一个 task 重试时，
它先前已经落库的写入不能被替换，否则恢复出来的那一步会看到同一份工作的第二个结果。
error / interrupt / resume / scheduled 这些**负数槽位**相反，**后到为准**：最新的那个
才是该 task 的当前状态。实现上按 `idx` 正负拆成两批，分别
`ON CONFLICT DO NOTHING` 与 `DO UPDATE`。

**`get_next_version` 带随机后缀。** 基类默认发整数版本号。那样两个进程写同一个 thread
会为同一通道都算出 `n+1`，把不同的字节写进**同一个 blob 主键**，一方静默覆盖另一方。
随机后缀让它们的 key 不同；32 位零填充的计数器让 `>`——pregel 循环用它判断哪些节点
已经见过某个通道——仍然按数值排序。

同步的一半（`get_tuple` / `list` / `put` / `put_writes` / `delete_thread`）**显式拒绝**并
说明原因，而不是继承基类那个不带消息的 `NotImplementedError`：这里的同步入口只能自己
起事件循环，而本项目每个调用者都已经在一个循环里了。

`alist` **先读完再逐个 yield**。异步生成器如果跨 yield 持有连接，调用方消费多久就占用
多久，提前 break 还会直接泄漏一条。代价写在注释里：不带 `limit` 的列举会把一个 thread
的历史读进内存——上界是该 thread 跑过的步数。组装用三条查询（checkpoint、blob、write
各一条），不是 2n+1 条。

### 一个被实测纠正的预期

LangGraph 自己的序列化把**元组还原成列表**（`('a','b')` → `['a','b']`，嵌套的也一样）。
`InMemorySaver` 同样如此，所以这不是本实现的问题，而是契约的格式。`TaskState` 在
`model_validate` 时把它们收回元组，因此恢复后的运行察觉不到——测试断言的是**恢复出的
领域状态相等**，而不是逐个字段形状相等。第一版测试按形状比较，直接失败，这条记录的是
纠正后的理解。

### 有牙验证：13 处破坏，其中 1 处暴露了测试的漏洞

每次只改实现里的一件事，跑整份测试文件，还原后重新确认全绿：

| 破坏 | 被抓住的测试 |
|---|---|
| `aget_tuple` 取最旧而不是最新 | 3 条 |
| `aget_tuple` 忽略 namespace | 1 条 |
| 普通写入改成覆盖 | 1 条 |
| 特殊写入改成丢弃 | 1 条 |
| `empty` 通道被当成值还原 | 9 条 |
| `alist` 忽略 `before` / `filter` / `limit` / thread | 各 1 条 |
| 版本号不带随机后缀 | 1 条 |
| 同步一半悄悄能用 | 5 条 |
| 从不加载 pending writes | **第一轮：0 条** |
| 不记录 parent checkpoint | 1 条 |

倒数第二行是重点：**第一轮破坏验证发现"pending writes 根本不加载"没有任何测试会失败**。
那不是无关紧要的——`research_internal` 与 `research_external` 在同一步并行，若一个成功
一个崩溃，幸存者的结果已经记在该步的 checkpoint 上、但这一步没有完成；恢复时**只有失败
的那一支可以重跑**，重放幸存者会把它的预算算两遍、把它调用过的东西再调一次。补上这条
测试后 13 处破坏全部被抓住，0 处漏网。

### 跨进程恢复的证据

关键的一条测试：第一次运行在 `critic` 里抛异常；然后 handler、编译后的图、saver、engine、
连接池**全部丢弃**，只留下 `thread_id`；第二个"进程"从零重建，`ainvoke(None, config)`
接着跑完。断言是——第二次运行里 `understand` 与 `research_internal` 的调用次数为 **0**，
`critic` 为 1，最终状态带着**上一个进程采集的证据**。

配套的对照组用 `InMemorySaver` 做同一件事，证明它**做不到**——否则"从 checkpoint 恢复"
和"从输入把整张图重跑一遍"在断言里长得一模一样。

另有一组差分测试：同一张图、同一份输入，分别跑在 `InMemorySaver` 和本 saver 上，要求
两者的最终状态与整段 checkpoint 历史一致。参考实现比"作者自己写下的期望"更适合当 oracle。

本轮门禁：

```text
ruff format --check .    passed（218 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0010_workflow_checkpoints

pytest（无外部服务）              913 passed / 286 skipped
pytest（真实 PostgreSQL + Qdrant） 1188 passed /  11 skipped
```

新增 21 条测试，其中 6 条（同步拒绝、版本号）不需要数据库。

### 本轮明确未做

- **没有任何组装点构造它。** `LangGraphTaskWorkflow` 仍然默认 `InMemorySaver`，
  它的 `resume` 也仍然查**进程内存**里的 thread → graph version 映射，所以
  **端口层**的"重启后用原 `thread_id` 续跑"还没通——通的是 checkpointer 层。
  把两者接起来要先决定 graph version 存在哪里，那是下一次变化
  （已由上面的
  [PR-050（三）](#2026-07-28-pr-050三workflow-身份进-checkpoint端口层跨进程续跑)完成：
  版本进 checkpoint metadata）；
- `adelete_thread` 未实现（基类的 `NotImplementedError` 原样保留），checkpoint 保留与
  清理策略同样还没有；
- 并发写同一 thread 只靠随机版本号避免 blob 主键互相覆盖，**没有**跨进程的互斥；
  真正的互斥是 WP08 的 lease 与 guard 连接。

## 2026-07-28 PR-050（一）checkpointer 的表结构

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。对应 WP06-06 的第一步。
[ADR-014](./adr/0014-own-postgres-checkpointer.md) 决定自研 saver，本轮只落**它要写进去的表**，
saver 本身一行都还没写。

### 三张表不是设计选择，是契约的形状

`aput` 只收到 `new_versions`——本次真正变化的通道——所以通道值必须存在一张**按版本
索引**的表里，而不是每个 checkpoint 复制一份；`aput_writes` 记录的是"某个 task 已经
产出、但消费它的那一步还没 checkpoint"的中间结果，所以它需要自己的表。

LangGraph 序列化出来的东西**在这里保持不透明**：`dumps_typed` 返回 `(type, bytes)`，
`loads_typed` 收回同一个二元组，因此两半都存、都不解释。把它拆成"可读"的列，等于本
项目声称自己理解一个并不属于它的格式，而理解错的代价恰好在恢复 checkpoint 时兑现。

表名带 `workflow_` 前缀：生态里不带前缀的 `checkpoints` 正是官方 saver 用
`CREATE TABLE IF NOT EXISTS` 建的表，列结构与这里不同，不该被它悄悄认领。

### 实测：**不能**给 writes 加指向 checkpoints 的外键

这是本轮唯一一个"看起来显然该做、实测证明会坏事"的决定。LangGraph 默认
`durability="async"`，**不等 checkpoint 落库就发出下一步的 writes**。把 `aput` 人为拖慢
50 ms 后测量：

```text
durability=async   11 次 writes 调用，11 次都在对应 checkpoint 提交之前开始
durability=sync    11 次 writes 调用， 1 次在对应 checkpoint 提交之前开始
```

三种 durability 模式、一个抛异常的节点（ERROR write 路径）和一次 resume 都测过。
所以外键在这里**不是约束不变量，而是让正常运行失败**。它的缺席由一条测试固定下来，
测试的文档说明了原因，免得后来者把它当成疏漏"补上"。

### 有牙验证

12 处等价破坏，逐个只改数据库里的一件事，然后跑对应测试；全部失败，还原后全绿：

| 破坏 | 失败测试 |
|---|---|
| 给 writes 加 checkpoints 外键 | 写入早于 checkpoint 的用例 |
| blob 主键去掉 `version` | 一通道多版本共存的用例 |
| blob 主键加宽到不再唯一 | 同通道同版本只存一次的用例 |
| checkpoint 主键去掉 `checkpoint_ns` | 子图命名空间隔离的用例 |
| writes 主键去掉 `idx` | 负数索引与普通写共存的用例 |
| `idx >= 0` 约束 | 同上 |
| `payload` 改成 text | msgpack 非 UTF-8 的用例 |
| `metadata` 改成 bytea | 按 key 查询 metadata 的用例 |
| 空 blob payload 被拒 | "写入了空值"与"从未写入"区分的用例 |
| `checkpoint_id` 收窄到 16 字符 | 真实运行全量落库的用例 |
| `task_path` 收窄到 8 字符 | 同上 |
| `parent_checkpoint_id` 设为 NOT NULL | 同上 |

最后一栏那三条来自同一个测试：它把 v1 图**真跑一遍**，把 LangGraph 实际要求存的每一行
（11 个 checkpoint、40 个 blob、32 条 write）原样写进表里再读回来，逐字节比对。它证明的
不是"某个手搓的行能存下"，而是"契约真正产生的东西能存下"。

本轮门禁：

```text
ruff format --check .    passed（216 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
agent-config-check       development / test / production 均为 status=ok
alembic 唯一 head        0010_workflow_checkpoints

pytest（无外部服务）              907 passed / 271 skipped
pytest（真实 PostgreSQL + Qdrant） 1167 passed /  11 skipped
```

新增 11 条测试全部需要真实 PostgreSQL，因此无外部服务那一列的 `passed` 不变、
`skipped` 从 260 涨到 271。

### 本轮明确未做

- **saver 本身**：`BaseCheckpointSaver` 的 `aput` / `aput_writes` / `aget_tuple` /
  `alist` 一个都没实现，`LangGraphTaskWorkflow` 仍默认 `InMemorySaver`，所以
  **进程重启不保留执行位置这一条至今没有变化**（这一条已由上面的
  [PR-050（二）](#2026-07-28-pr-050二saver-本身跨进程恢复第一次有了证据)在
  checkpointer 层解决）；
- **thread → graph version 的映射仍在进程内存里**。它没有进这三张表：按实施计划
  §6 它属于 `task_runs.graph_version`，而 ADR-014 写明 checkpointer 不得成为 Task
  产品状态的第二个 writer。它随 WP06-07 的 `task_runs` 落库；
- checkpoint 的保留与清理策略。三张表都有 `created_at`（blob 只能靠它——它只能经由
  checkpoint 不透明载荷里的 `channel_versions` 到达，SQL 无法把它 join 回来），但还
  没有任何东西会删除它们。

## 2026-07-28 PR-049 LangGraph Adapter 与 graph version 注册表

状态：**已合入 `main`（GitHub `#59` / `341cbf5`）**。对应 WP06-05。**本轮第一次引入 `langgraph` 依赖。**

Adapter 拥有编译、checkpoint 和调度；**不拥有任何路由决定**。每条边都来自
`workflows.research_graph`——所以"重放 checkpoint 的那张图"和"控制流测试断言的
那张图"是同一份声明。在这里重述一条路由规则，就是造出第二份定义，而它会静默漂移，
且只在恢复路径上暴露。

### 依赖决策：主依赖，不是 extra

`langgraph` 进 `[project.dependencies]` 而不是可选 extra。`embedding` extra 的存在
理由是**体积**（运行时加权重好几个 GB）；langgraph 是纯 Python 小包，放进 extra
只能换来"CI 跳过 workflow adapter 的测试"，也就是对它什么都证明不了。

许可证门禁需要补两个字符串，都来自传递依赖，且都宽松：

```text
MPL-2.0 AND (Apache-2.0 OR MIT)   ← orjson
Apache-2.0 OR MIT                 ← ormsgpack
```

`MPL-2.0` 本身早已在允许列表内，所以这是列表的**扩充**而不是策略放宽。用 CI 同款
`pip-licenses --allow-only` 命令对 140 个包的实际解析结果逐个验证过，新增 17 个包
（langchain-core、langgraph-checkpoint、langsmith、zstandard 等）全部通过。
`uv lock --check --offline` 通过，uv 版本与 CI 固定的 `0.11.31` 一致。

### 状态即通道，且不能悄悄少一个

`TaskState` 以普通 mapping 进入图，字段就是图的通道。两个引用通道挂
**与控制流同一个排序并集 reducer**，因此 LangGraph 自己的 fan-in 与
`workflows.fan_in` 产生相同的合并结果。

`TaskState` 新增字段而这里没加通道，会在**第一次 checkpoint round-trip 时被丢掉**。
所以有一条测试直接断言两个字段集合相等，而不是指望它们自觉保持同步——破坏验证里
删掉一个通道会让 3 条测试失败。

### 预算耗尽走 `END`，不走 approval

`route_quality_gate` 返回 `None` 时，adapter 映射到 LangGraph 的 `END`。这是一个
**LangGraph 认识而领域不认识**的节点名，正是要点：图必须停下，而 `TaskNodeId`
不该为了描述"停下"而多出一个成员。

`resume` **不传初始状态**：状态已经属于 checkpoint，再传一次正是"崩溃后把原始输入
追加两遍"的来源。测试用计数 handler 证明 `understand` 只跑过一次。

graph version 注册表按版本编译并缓存。未注册的版本**失败关闭**，不回退到最新图——
checkpoint 记录了它由哪个版本写入，猜错的代价恰好在最贵的时候产生。

### 有牙验证

六处等价改动，还原后逐字节一致：

| 破坏 | 失败测试数 |
|---|---|
| 预算耗尽路由到 `approval` | 1 |
| `resume` 重新提交初始状态 | 2 |
| `run` 不拒绝已存在的 thread | 1 |
| 通道 reducer 改成覆盖 | 1 |
| `resume` 忽略 graph version 不匹配 | 1 |
| `GraphState` 少一个通道 | 3 |

本轮全仓门禁：`907 passed / 260 skipped`（新增 12）；Ruff、Pyright 全过。
langgraph 无 type stub，按包内既有的 FlagEmbedding / sentence-transformers 同款做法，
用窄 `pyright: ignore` 加 `cast` 收窄，而不是为整个包放宽类型检查。

### 本轮明确未做

- **PostgreSQL checkpointer（WP06-06）**：当前用 `InMemorySaver`，因此
  **进程重启不保留执行位置**，"从 checkpoint 恢复"尚无证据；
- Task Worker / TaskService / Task 查询接口（WP06-07）、事件时间线（WP06-09）；
- `plan`/`critic` 的结构化输出解码；`approval`/`export` 仍是无副作用占位；
- thread → graph version 的映射目前在**进程内存**里，随 WP06-06 的持久
  checkpointer 一并落库。

## 2026-07-28 PR-048 Agent node：只经 `AgentExecutor`，且失败也要记账

状态：**已合入 `main`（GitHub `#58` / `0830e55`）**。对应 WP06-03。

节点通过 `AgentExecutor` 到达模型，除此之外没有别的路径——不持有 tool registry、
不持有 model port、不持有 Runtime 内部对象。**一个能自己组装循环的节点，就是第二个
Runtime，而且不带第一个的预算、取消和 Policy 保证。** 测试直接断言节点实例只持有
一个 `_executor` 字段。

本轮只做**产物是一个 artifact 的节点**：`understand / research_internal /
research_external / synthesize`。它们的状态贡献就是 `AgentOutcome.output_ref`
已经携带的引用。`plan` 与 `critic` 需要把模型输出**解码成结构化值**（TaskStep 列表、
pass/revise 决定），那需要一份解码契约，所以刻意不在本模块里。
`ARTIFACT_PRODUCING_NODES` 在调用时校验，因此将来给某个节点加上结构化产物时，
它不能继续走 artifact 那条路而把那个产物悄悄丢掉。

### 失败的运行也要记账

`AgentExecutor` 的契约是"预期失败返回终态 outcome，而不是抛异常——调用方是一个
必须记录并路由的 graph node"。所以节点**先把 run id 和 usage 折进状态，再判断成败**：

```text
run → 记录 agent_run_id + usage → 判断是否可用 → 可用则返回，不可用则带着已记账的状态抛出
```

`AgentNodeFailedError` 携带那份**已经扣过费的状态**。只记录成功的节点，会让同一个
Task 在一个"看起来从没动过"的预算里无限重试。对照组就是同一节点、同一次调用，
唯一差别是 outcome 失败。

### 完成但没有产物，是失败而不是空成功

这四个节点的全部工作就是产出那个 artifact。`completed` 但 `output_ref is None`
被判为失败——否则图会继续往下走，把一个背后什么都没有的目标交给下一个节点。
`cancelled` 同理：即使 outcome 上挂着 artifact 引用也不采纳，因为它背后的工作是被
中途停掉的。

### 提示词是投影，不是转录

节点只发送 objective 和 plan，**不发送先前节点累积的输出**。那些东西在 artifact
store 里；把它们抄进 prompt，会让上下文随图增长而不是随问题增长。测试用
evidence/outcome 引用做 canary 断言它们不出现在 prompt 里。

`TaskRunContext`（trace/stream/principal/envelope/budget）**刻意不进 `TaskState`**：
一个携带 principal 和授权信封的 checkpoint，会让 resume 重放 Task 启动时的授权，
而不是重新推导它。由 Registry 在 claim 时提供。

### 有牙验证

六处等价改动，每处都**只**让对应的那一条测试失败，还原后逐字节一致：

| 破坏 | 失败测试数 |
|---|---|
| 只在成功时记账 | 1 |
| 接受"完成但无产物" | 1 |
| 把 cancelled 当作内容 | 1 |
| prompt 回放累积引用 | 1 |
| usage 覆盖而不是累加 | 1 |
| `absorb_draft` 保留陈旧 review | 1 |

本轮全仓门禁：`895 passed / 260 skipped`（新增 13）；Ruff、Pyright 全过，跳过项不变。

### 本轮明确未做

- `plan` 与 `critic` 两个节点（需要结构化输出解码契约）；
- `approval` / `export` 占位节点；
- LangGraph adapter、checkpointer、Task Worker、事件时间线。**仍然没有任何 Task
  可以端到端运行**；本轮只是把"节点如何与模型交互、如何记账"这一层钉死。

顺带把 `research_graph._evolve` 提升为公开的 `evolve`：重新校验这条规则原本要在
两个模块里各写一遍，而重复的规则就是会漂移的规则。

## 2026-07-28 PR-047 固定研究图的确定性控制流与 fan-in reducer

状态：**已合入 `main`（GitHub `#60` / `4ec04d2`）**。
对应 WP06-02（条件路由）与 WP06-04（确定性 fan-out/fan-in reducer）。

新增 `src/agent_workbench/workflows/`——框架无关的核心包，架构守卫已实际覆盖它
（把 `import langgraph` 放进去会让 `test_core_keeps_frameworks_and_concrete_sdks_at_outer_boundaries`
失败，已实测）。**边写成数据、路由写成纯函数**，因为一个嵌在已编译图里的路由决定，
只能靠运行那张图来测试。LangGraph adapter 将来编译这份声明，而不是持有同一决定的
第二份措辞。

两条对恢复起支撑作用的性质放在这一层而不是 adapter 里：

- **fan-in 是排序并集**。合并结果与哪个 research 分支先完成无关，重复合并同一份
  contribution 也不改变结果。于是 fan-in 中途崩溃后的 checkpoint 会**收敛**，而不是
  累积重复引用。测试直接断言交换律、幂等性，以及"重放一个分支等于没重放"；
- **revision 预算耗尽的 quality gate 不返回下一个节点**。它返回 `None`，而不是
  `approval`：把耗尽路由到审批，等于**批准一份 critic 明确否决的草稿**，恰好在质量门
  最该起作用的时候把它变成形式。调用方必须显式处理这个值，不能靠忽略返回值走到审批。

三条 quality gate 条件边各有测试，且**互为对照组**——`pass → approval`、
`revise 且有预算 → synthesize`、`revise 且预算耗尽 → 无后继`，后两条唯一的差别就是预算。

`begin_revision` 在推进计数的同时**丢弃旧 review**：`TaskState` 要求存储的 review 描述
当前 `revision_count`，保留它要么校验失败，要么更糟——让下一次质量门读到一份关于
**已被重写的草稿**的陈旧结论。

### 一个本轮修掉的真实缺陷

reducer 最初用 `model_copy(update=...)` 构造新状态。**Pydantic 的 `model_copy` 跳过
校验器**，而 `DomainModel` 没有开 `revalidate_instances`，所以一个返回乱序或重复引用
的 reducer 会被**原样存进 checkpoint**，直到以后某次读取才失败。改为经
`model_validate` 重新校验的 `_evolve`。

这一条的守卫只有在 reducer 真的回归时才起作用，所以测试**注入**那次回归
（monkeypatch 掉 `merge_refs` 让它返回乱序），证明 `fan_in` 拒绝而不是落盘。
修复前没有任何测试能发现这个问题——sabotage 4 只让这一条失败，正说明它此前是裸的。

### 有牙验证

五处等价改动，每处都让**对应**测试失败，还原后逐字节一致、23 条全过：

| 破坏 | 失败测试数 |
|---|---|
| 预算耗尽改为路由到 `approval` | 2 |
| `merge_refs` 保留插入顺序（不排序） | 4 |
| `begin_revision` 保留陈旧 review | 2 |
| `_evolve` 改回 `model_copy` | 1 |
| `route` 忽略空 plan | 1 |

本轮全仓门禁：`882 passed / 260 skipped`（新增 23 条），Ruff format/check、Pyright
`0 errors / 0 warnings` 全部通过。跳过项与 `main@e93d7a1` 完全相同，本增量不触碰
任何外部依赖。

### 本轮明确未做

- **WP06-03 Agent node**：`understand / plan / research_* / synthesize / critic`
  的处理函数尚不存在，本轮只有它们之间的控制流；
- **WP06-05 LangGraph Adapter**：`langgraph` 仍不是项目依赖，`adapters/langgraph/`
  不存在；
- **WP06-06 PostgreSQL checkpointer**、**WP06-07 Task Worker / TaskService /
  Task 查询接口**、**WP06-09 Task 事件时间线**；
- 因此**没有任何 Task 可以端到端运行**，也没有 checkpoint 恢复证据。本节只证明
  "下一个节点是谁"和"并行结果如何合并"这两件事是确定的、有界的、可测的。

## 2026-07-28 PR-046 Sparse 加载守卫：拒绝没有 lexical head 的编码器

状态：**已合入 `main`（GitHub `#55` / `e93d7a1`）**。

BGE-M3 训练好的 lexical 投影放在 `sparse_linear.pt` 里，和基础权重**并排**而不在其中。
FlagEmbedding **不把这个文件的缺失当作错误**：它当场构造一个新的 `Linear(1024, 1)`
然后继续。于是下游每一道检查都通过、又都毫无意义——模型加载成功；输出宽度确实是
250002，但那个宽度来自 tokenizer 词表，**不来自这个投影**；编码器返回结构完好的
sparse 向量。**只有数字是错的，而且是随机地错**，随机不触发任何告警。

后果是：两个进程加载同一个固定 revision，拿到的是不同的权重。这让 `index_identity`
变成**贴在一个随机变量上的标签**——一个进程写入的 point，被另一个进程的向量空间检索。
本项目此前发布的每一个 hybrid 数字，都只是某个无关分布的一次采样。代价是两个被撤回
的诊断（"短查询编码为空"不是错的，是**随机的**），以及一次结论会反转的消融。

**修复是拒绝，不是下载。** 把文件取到本地只修好一台机器；拒绝构造一个投影不在缓存里
的编码器修好所有机器，而且错误信息里带着取回它的命令。

两条守卫，改动前都失败：

- 同一个固定 revision 的同一查询，在两次加载之间必须编码为**同一个向量**——这正是
  `index_identity` 赖以成立的契约；
- 直接命名成因：一个**新构造**的 `Linear` 不可能两次产生相同的数字。症状测不出这一条。

`evals/rag/reports/ABLATION.md` 与 `SPARSE-DIAGNOSIS.md` 已按加载修复后的重跑结果
订正，`dense.json` / `hybrid.json` 同步更新，`hybrid-run1.json` 保留作对比。选择
FlagEmbedding 的背景决策见 [ADR-013](./adr/0013-bge-m3-sparse-encoder.md)（PR-026
留下）；本轮补的是那个决策**没有覆盖到的加载缺口**。

附带修正：投影守卫的 `huggingface_hub` 导入对类型检查器屏蔽并经 `cast` 收窄。该库
随 `embedding` extra 才安装，本机装了它、CI 没装，所以本地 pyright 通过而 CI 是唯一
能看见这个错误的地方——处理方式与包内既有的 FlagEmbedding / sentence-transformers
一致，不让一个可选依赖变成跑门禁的必需品。

## 2026-07-28 PR-045 Reranker：重排已授权候选，fail-open 且不扩大范围

状态：**已合入 `main`（GitHub `#54` / `3b7829b`）**。对应 WP05-03 / WP05-04。

cross-encoder 同时读 query 和 passage，而检索器做不到：dense 与 sparse 都在**问题存在
之前**就把 passage 编码完了。这值得在几十个候选上多花一个数量级的算力，也正是这一步
需要 timeout 的原因。

**Port 返回每个 passage 一个分数（按位置对应），而不是一个重排好的列表。** 一个返回
passage 的 adapter 可以少还一个、重复一个、或者还回一个从没给过它的东西，而调用方
分不清那是 bug 还是排序结果。分数让契约可以**按长度**校验，并把重排留给
**知道 PostgreSQL 授权了什么**的那一层。于是"reranker 不可能引入一段提问者无权读的
passage"**由构造成立，而不是靠信任**。

位置固定在**授权之后、`top_k` 之前**：

- 更早，cross-encoder 会读到提问者无权读的文本；
- 更晚，它只能在检索器已经切好的名单里做提升，永远推翻不了检索器的选择——而那不是
  消融要比较的东西。

**fail-open 且窄。** timeout、异常、分数条数对不上，全都回退到**已授权的原顺序**：
一个质量步骤不该把一个能用的回答变成一个错误。`CancelledError` **不捕获**——它继承
`BaseException`，一个被取消的请求必须保持取消。回退目标就是输入列表本身，因此
**没有任何一条路径会扩大已授权范围**。

**缺 reranker 不损失 Chat 能力**，这和缺 embedder 不同：一个让回答变差，另一个让人
根本无法构造查询。但由于**在端点上，未重排的进程和重排过的进程无法区分**，
`ApiDependencies` 记录 `reranker_unavailable` 原因；否则一份针对静默未重排部署写出来
的消融报告，会把差异记在模型头上。`RetrievalResult.reranked` 出于同一理由存在：它
区分"真的重排过"与"fail-open 回退"，否则一次超时会被读成"rerank 没有差别"这种被
制造出来的零结果。

**未做**：三臂消融的 `hybrid-rerank` 臂仍未跑。hybrid 在当前 38 题 gold set 上已打满
1.000，**rerank delta 在这里必然为 0**；要测出 rerank，先得有更难的 gold set，而不是
先跑第三臂。详见 `evals/rag/reports/ABLATION.md`。

## 2026-07-28 PR-044 Task 工作流的 checkpoint-safe 状态

状态：**已合入 `main`（GitHub `#53` / `3538e26`）**。这是 WP06 的第一块，**只有领域
状态与 Port**：LangGraph adapter、节点 handler、checkpointer 和 Task Worker 都还不存在。

图的 checkpointer 被允许持久化这份状态，所以这个模块是围绕**什么绝不能进来**写的：

```text
执行位置      → checkpointer
产品状态      → Task Registry
大输入/大输出 → 各自的 store
```

每个字段都小且 JSON 可序列化。这份状态不允许长出 `current_step`、消息记录、文档正文
或任何 provider/框架对象。

**校验器拒绝，而不是规范化。** 未排序的 `depends_on` 是一个**错误**，而不是在这里被
悄悄排好——建立规范顺序的是并行 fan-in 的 reducer，在入口处排序会**掩盖一个已经不再
做这件事的 reducer**。plan 依赖只能指向排在它之前的 step，于是一个给定 DAG 只有一种
拓扑表示，一个 checkpoint 也就只重放到一个状态、而不是若干个。同时禁止自依赖和重复
依赖。

`ports/task_workflow.py` 把 `thread_id` 与 `graph_version` 显式放在**每一次操作**上，
adapter 不能拿一份不同的图定义去静默恢复一个 checkpoint。`resume` **刻意不接受初始
`TaskState`**：状态已经属于 `thread_id` 指向的那个 checkpoint，再接受一次就使"崩溃后
把原始输入追加两遍"重新成为可能。

canonical v1 节点 ID 固定为 `understand / plan / route / research_internal /
research_external / synthesize / critic / quality_gate / approval / export`，与架构
基线、checkpoint metadata、事件和测试一致；重命名任一节点都必须提升 `graph_version`。

golden 载荷集新增 `TaskState`：原有 13 个聚合**逐字节未变**，重新生成的文件只增 52 行、
不删任何行。

## 2026-07-28 PR-043 Chat lease 过期原子终态

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；静态检查与本地可运行门禁通过**。

PR-041 让硬崩溃遗留的 `running` Turn 最终失败，但当时 reaper、claim 和迟到
prepare 都可能写过期终态，Turn failure 与 durable Event 也不在同一个原子边界。
PR-043 把 PostgreSQL 的过期语义收敛到唯一 writer：

```text
ChatExpirationCoordinator.expire_due
  → SELECT oldest due running FOR UPDATE SKIP LOCKED
  → failed(deadline, stale_execution) + clear lease
  → ChatTurnExpired
  → COMMIT one Turn
```

实现不变量：

- PostgreSQL 中只有 `ChatExpirationCoordinator` 可以持久化
  `stale_execution` 过期终态；普通 failure/cancellation writer、prepare 和 cleanup
  都先锁 session/Turn，再用数据库时钟复核 lease；
- claim 只创建或读取 Turn，不机会式回收。协调器提交前，同 key 仍观察原
  `running`，新 key 仍得到 busy，不能先解锁 session、后补 Event；
- 迟到的 `prepare_release`、`finish_failed` 和
  `finish_running_if_current` 不写 candidate、failure、assistant 或 Event；它们在锁内
  发现仍为 `running` 但 lease 已到期时抛 `ChatTurnLeaseExpiredError`，协调器已经提交
  时只观察既有终态；调用方也不能把构造的 `stale_execution` outcome 送进普通
  cleanup 绕过协调器；
- 每个候选使用一个独立 PostgreSQL 事务。更新 Turn 后写 Event 失败，或 Event 写完后
  出错，都会一起回滚；毒化候选保持 `running`，跨轮稳定扫描游标保证
  `batch_size=1` 时后续 due Turn 仍可处理；
- answer release 与 expiry 共用 SHA-256 派生的有界终态键
  `chat-turn:{sha256(turn_id)}:terminal`。同一 Turn 的 answer 与 expiry 不能各占一个
  幂等键；已存在的相同 `ChatTurnExpired` 可收敛，冲突 payload 则使该候选回滚；
- `ChatTurnExpired` 只观察 Chat Turn 的发布生命周期。Runtime 可能已经有
  `RunCompleted`；此时仍可随后出现 `ChatTurnExpired`，但不能伪造第二个
  `RunFailed`，事件也不携带候选答案、引用或 output reference；
- Memory coordinator 不宣称数据库耐久性或分布式原子性，但保持可观察失败回滚：
  Event append 失败时 Turn 仍是 `running`、session 仍 busy、Event 不可见；成功后
  Event 与 Turn 才在同一进程锁边界内一起可见。

确定性测试覆盖两个事务 fail seam、`batch_size=1` 毒化候选隔离与跨轮推进、两个
reaper 竞争、prepare/reaper
竞态、claim 在原子提交前后的可见性、共享终态键以及 Runtime/Chat 两种终态事件的区分。
本地全仓门禁为 `800 passed / 257 skipped / 1 deselected`；唯一 deselect 是当前
受限沙箱禁止 `socket.bind()` 的 loopback 真实性测试。Ruff、Pyright、compileall、
Alembic 唯一 head 与 `git diff --check` 均通过。真实 PostgreSQL 并发用例因未配置
`AGENT_WORKBENCH_TEST_DSN` 而显式跳过，未被描述为已在本机执行。

## 2026-07-28 PR-042 Chat pending 发布无流量恢复

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；无外部服务门禁与静态检查通过。
真实 PostgreSQL 并发用例在该增量当时因未配置测试 DSN 而跳过**。

PR-041 关闭了 `running` 硬崩溃后永久 busy，但 `prepare_release` 已提交、
原子发布尚未开始的窗口仍需要原客户端用同一 key 重试。若客户端消失，
`release_pending` 会一直占用单会话 active Turn。本轮把该恢复改为与请求流量无关：

```text
短查询 list_release_pending
  → 取 session 的 tenant/owner scope
  → 重新进入 ChatReleaseCoordinator
  → 锁 session/Turn/引用文档/event stream
  → 重新校验当前 ACL + source_revision
  → AnswerCommitted | AnswerWithheld + assistant + terminal Turn 原子提交
```

实现要点：

- 新增 `PendingChatRelease` 端口模型和 `list_release_pending(limit)`；Memory/PostgreSQL
  共用同一契约，只返回 `release_pending`，按 `turn_id` 稳定排序并携带 session owner；
- PostgreSQL 列表查询使用短连接普通 MVCC `SELECT + JOIN`，不把扫描事务或行锁带进
  模型/发布阶段；多个恢复器可看到同一候选，最终由 Turn 锁、terminal state 和稳定
  `chat-turn:{sha256(turn_id)}:terminal` 幂等收敛；
- `ChatPendingReleaseRecovery` 隔离单候选失败，后续候选仍会尝试；失败项保留 pending，
  下一轮继续重新授权，绝不把旧候选降级为未经检查的发布；
- API lifespan 同时管理 running reaper 与 pending recovery；两者都只依赖
  PostgreSQL/EventLog，不依赖 embedding、Qdrant 或模型，因此 Chat 降级不可用时仍能
  清理先前遗留状态；
- 确定性测试模拟 `after_prepare_before_release`：不再发送原请求，后台恢复后仍只写
  一个 `AnswerCommitted`，并证明会话可 claim 新 key；另测单候选失败不终止同批后续项。

完整无外部服务门禁为 `771 passed / 242 skipped`；Ruff、Pyright、compileall 和
Alembic head 检查通过。真实 PostgreSQL 契约还覆盖 owner scope、limit/order，以及
普通 pending scan 不等待另一个 Worker 持有的 Turn 行锁。

本节当时保留的 execution expiry 多 writer 和裸 Turn failure 边界已由 PR-043
关闭：claim/late prepare/cleanup 不再写过期事实，独立协调器按 Turn 原子提交
`failed(stale_execution)` 与 `ChatTurnExpired`，且不伪装成 Runtime `RunFailed`。

## 2026-07-28 PR-041 Chat 固定执行 lease 与孤儿回收

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；无外部服务门禁、静态检查和迁移
head 检查通过。真实 PostgreSQL 并发用例在该增量当时因未配置测试 DSN 而跳过**。

同步 Chat 没有 checkpoint、attempt-aware event 或副作用恢复协议，因此硬崩溃后
“把同一个 Turn 交给另一个 Worker 重跑”并不安全。本轮选择更小且可证明的语义：

```text
claim
  → running + 固定 lease_until
  → release_pending | failed | cancelled

lease 到期
  → failed(deadline, stale_execution)
  ↛ running（禁止续租、转移和自动重跑）
```

实现要点：

- 迁移 `0009_chat_turn_lease` 增加 `lease_until`、`running ⇔ lease 非空` 数据库
  约束和过期扫描部分索引；升级时旧 `running` 行立即具备可回收截止时间；
- claim 使用 PostgreSQL `statement_timestamp()` 计算
  `api.request_timeout_seconds + chat.orphan_grace_seconds`，不信任应用机器时钟；
- PR-043 后 claim 不再机会式终态化：协调器提交前，同 key 保持 `running`，新 key
  保持 busy；
- `prepare_release` 在 Turn 行锁内复核数据库时间；过期时只抛
  `ChatTurnLeaseExpiredError`，不写候选、failure、Event 或 assistant；
- `ChatTurnReaper` 委托 `ChatExpirationCoordinator` 按 `(lease_until, turn_id)`
  使用 `FOR UPDATE SKIP LOCKED` 回收；Turn failure 与 `ChatTurnExpired` 在每 Turn
  独立事务中提交，多个 API 实例可以竞争而不重复处理；
- `finish_failed` 与 `finish_running_if_current` 同样在锁内复核 lease，只能清理
  尚未过期的 `running`，不能覆盖 prepared/terminal，也不能成为第二个 expiry writer；
- `asyncio.timeout` 现在真正消费 `api.request_timeout_seconds`；外层
  `CancelledError` 使用 shielded best-effort cleanup 后仍重新抛出；
- Chat 路由用 `CancellationSource` 传递原因，并直接取消实际 Chat task，断开不再等待
  retrieval 自己轮询；API lifespan 先停止 reaper、有界排空 cleanup，再关闭
  HTTP/Qdrant/PostgreSQL 依赖。

PR-041 当时的契约覆盖固定 lease、请求 deadline、外部 task 取消和 terminal-only
reaper；其中“不同 key 机会回收”和“迟到 prepare 直接写失败”的断言已由 PR-043
替换为：原子提交前同 key 保持 running、新 key busy、late prepare 无写入，以及
coordinator 提交后同 key 返回失败并允许新 key。PR-041 当时完整无外部服务门禁为
`763 passed / 237 skipped`，Alembic 唯一 head 为 `0009_chat_turn_lease`；这不是
PR-043 的当前门禁数字。

本节最初保留的 `release_pending` 无人重试边界已由 PR-042 关闭；execution lease
的多 writer 与非原子 Event 边界已由 PR-043 关闭。固定 Chat lease 仍刻意不提供
自动重放或故障转移。

`0009` 当前按停机迁移设计：部署必须先停止旧版本写入，再升级 schema，最后启动新版本。
它不是 expand/dual-write/contract 三阶段 migration，不支持新旧应用副本滚动共存。

## 2026-07-28 PR-040 Chat 原子授权发布栅栏

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；静态门禁与内存测试通过。真实
PostgreSQL/Qdrant 并发用例在该增量当时因未配置服务而跳过**。

PR-039 的稳定 `event_key` 能让重复发布返回同一个事件，但“先查 ACL、后写事件”的
两个事务之间仍存在 TOCTOU：复核成功后可以恰好发生撤权；进程也可能在
`prepare_release` 后退出，重试再把旧候选发布出去。本轮新增
`ChatReleaseCoordinator`，生产适配器把以下步骤放进一个 PostgreSQL 事务：

1. 固定按 `conversation session → chat turn → document_id 排序后的文档行
   → event stream` 加锁；
2. 在文档行锁内复核 tenant、deleted、精确 `source_revision` 与 owner/ACL；
3. 若任何来源变化，构造不含候选正文、ArtifactRef、citation 与 revision 的安全
   withheld 结果；
4. 写唯一 `AnswerCommitted/AnswerWithheld`、追加 assistant message、把 Turn 转为
   `committed/withheld`；
5. 一起提交或一起回滚。

所有合规的 Document/ACL writer 必须先锁同一 document row，再修改内容、revision
或 ACL；这是发布栅栏成立的写入协议，不能用绕过 repository 的 ACL-only SQL 破坏。
`ChatTurnResult` 现在持久化排序且唯一的 `authorized_revisions`，并强制其文档集合与
citations 一致，因此 `release_pending` 重试具备完整的再授权输入。

新增的 PostgreSQL/Qdrant 确定性竞态测试覆盖：

- prepare 后故障、撤权先提交、同 key 重试：只能产生 `AnswerWithheld`，API 对象、
  history、event 与 `chat_turns.result` 均不含候选正文；
- 发布先取得文档锁、撤权并发到达：通过 `pg_blocking_pids()` 证明撤权事务真实等待，
  发布提交后撤权才推进 revision，形成可解释的线性顺序。

稳定 `event_key` 继续作为重复调用的防御，但已不再承担跨两个提交恢复一致性的职责。
内存协调器仅保持同一可观察契约，不宣称提供跨进程授权栅栏。

## 2026-07-28 PR-039 幂等 Chat Turn 与发布恢复

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；本地确定性测试通过。真实
PostgreSQL/Qdrant 用例在该增量当时因未配置服务而跳过**。

本轮把“消息历史”与“一个请求执行到了哪里”分开建模。新增
`ChatTurnStore`、PostgreSQL `chat_turns` 表和迁移 `0008_chat_turns`，生命周期为：

```text
running
  → release_pending
  → committed | withheld

running
  → failed | cancelled
```

核心不变量：

- `POST /v1/chat/sessions/{session_id}/messages` 强制要求 `Idempotency-Key`；
- `(session_id, idempotency_key)` 唯一，同键不同 request hash 返回 409；
- `run_id` 全局唯一，并由 tenant/principal/session/key 稳定派生；
- PostgreSQL 会话行锁和 partial unique index 共同保证每个会话最多一个
  `running/release_pending` Turn；并发请求不会交错消息或重复计费；
- `claim_turn` 在同一事务内完成 ownership 校验、历史快照、user message 和 Turn
  创建；鉴权先于幂等查询，错误主体不能利用 key 探测 Turn；
- `release_pending` 只保存内部候选结果，**不写可见 assistant message**；
- `AnswerCommitted/AnswerWithheld` 使用稳定 `event_key`，重复调用不会重复事件；
- PR-040 已进一步把最终授权复核、答案事件、assistant 与 Turn 终态合并为同一个
  PostgreSQL 提交，取代本节最初的“先事件、后 Turn”两阶段实现；
- withheld Turn 的持久化 `AgentOutcome` 会清空候选正文、ArtifactRef 和 citation，
  被拒绝的模型输出不能藏在 retry ledger 中。

确定性测试覆盖同键重试、不同 hash 冲突、跨会话 key 作用域、全局 run 冲突、
并发 claim、状态转换幂等、所有权先验、发布后故障注入和秘密不落盘。API response
新增 `turn_id`，终态重试返回原 `turn_id/run_id/result`。

本轮仓库级无外部服务门禁为 `747 passed / 223 skipped`；Ruff format/check、
Pyright、compileall 全部通过，Alembic 唯一 head 为 `0008_chat_turns`。跳过项需要
PostgreSQL、Qdrant 或本地 BGE 权重；受沙箱禁止 `socket.bind()` 的单个真实性测试按
既有方式从本轮本地门禁排除。

本节最初记录的 `running` 永久 busy 风险已由 PR-041 的固定 lease、PR-043 的单一
expiry writer 与 terminal-only reaper 关闭。它刻意不提供自动故障转移；若未来允许
重新执行同一 Turn，必须同时加入 owner、epoch、heartbeat、全写入 fencing、
checkpoint 与 attempt-aware event。

## 2026-07-28 PR-038 durable Event 幂等发布

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；内存契约与静态门禁通过。真实
PostgreSQL 契约在该增量当时因未配置测试 DSN 而跳过**。

迁移 `0007_event_idempotency_key` 为 `events` 增加可空 `event_key`，并以
`(stream_id, event_key) WHERE event_key IS NOT NULL` 唯一索引建立流内幂等边界。
内存与 PostgreSQL EventLog 共用同一契约：

- 同一 stream、同一 key、同一 scope/payload/parent 返回原 envelope；
- 同键不同内容 fail closed，且不消耗 sequence；
- 并发八次 append 只产生一个 event，下一事件 sequence 连续；
- 同一 key 可在不同 stream 独立使用；
- transient event 禁止携带 key；
- `ScopedEventSink`、`ObservingEventSink` 和 `AnswerReleaseSink` 全链路透传 key。

EventLog 仍未提供历史 schema upcaster registry 与 poison-row 隔离/跳过策略。

## 2026-07-28 PR-037 EventLog 版本化回放元数据

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；本地静态门禁通过。真实 PostgreSQL
新用例在该增量当时因未配置测试 DSN 而跳过**。

`EventEnvelope` 一直声明 `schema_version` 与 producer timestamp，但 PostgreSQL
`events` 表此前没有保存版本，append 也没有写入 envelope timestamp。结果是：

- 旧 row 回放时会被默认套用当前 schema version，消费者无法知道它由哪个契约产生；
- append 返回的时间与 replay 返回的数据库落库时间不同，同一事件 round-trip 后发生
  变化。

本轮新增迁移 `0006_event_schema_version`，把已有 row 明确回填为 schema v1，随后移除
临时 server default，要求以后每次 append 显式写版本。`PostgresEventLog` 现在同时写入
`envelope.schema_version` 与 `envelope.timestamp`，read 时显式重建两者；未知版本会在
领域模型边界失败关闭。

相关测试覆盖：

- append 返回值、数据库列和 replay envelope 的 schema version 完全一致；
- 固定 producer clock 后，数据库与 replay 保留同一个时间；
- 将持久 row 人为改成未知版本时，replay 拒绝解析；
- 从 `0005` 升级时已有事件回填 v1，最终列为 `NOT NULL` 且没有残留 default。

本地相关门禁为 `30 passed / 22 skipped`；仓库级无外部服务门禁（排除受当前沙箱禁止
`socket.bind()` 的测试文件）为 `702 passed / 189 skipped`。Ruff format/check 与
Pyright 全部通过，Alembic 唯一 head 为 `0006_event_schema_version`。跳过项需要
PostgreSQL、Qdrant 或本地 BGE 权重。本轮尚未实现 schema upcaster registry、
poison-row 隔离/跳过策略；稳定幂等键已由后续 PR-038 补齐。

## 2026-07-28 PR-036 顺序多轮上下文

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；无外部依赖测试通过**。

此前 ConversationStore 会保存消息，但每次模型调用只收到当前问题与当前
`ContextPacket`，所以“有历史记录”并不等于“模型能多轮对话”。本轮把调用语义改为：

```text
读取并验证 session ownership
→ 取得已经提交的 conversation history 快照
→ 为当前问题执行一次新的授权检索
→ 历史原始消息 + 当前 evidence prompt 交给 Runtime
→ 最终授权后提交本轮 assistant 消息
```

只回放 ConversationStore 中已经存在的原始用户问题与最终 assistant 消息。上一轮 RAG
passage 不进入历史，未通过发布门的候选答案也不会进入历史；因此后续轮次不会从内部
prompt 重新带入旧 source revision 的原文。撤权路径只会回放安全拒答。

本轮确定性测试证明：

- 第二轮模型请求按 `user → assistant → user` 顺序收到第一轮已提交消息；
- 当前问题只出现一次，且只有当前问题附带当前检索 evidence；
- 撤权时生成过的候选秘密不会进入下一轮模型请求，只有安全拒答会进入。

本节落地时的边界是**顺序调用下的多轮上下文**；后续 PR-039 已补上
`chat_turns` 事实表、并发非交错和请求/发布幂等恢复。历史 token window/compaction
与模型实际使用 Citation 的结构化校验仍未完成。

## 2026-07-28 PR-035 安全发布基线

状态：**已合入 `main`（GitHub `#51` / `7025425`）；本地确定性测试通过**。

本轮关闭了 2026-07-27 复核报告中的两条直接读取风险：

1. **答案发布门**：固定 2-step Chat 不再把未完成最终 ACL 复核的模型正文写入
   durable `ModelCompleted` 或 live `ModelDelta`。Runtime 事件仍保留调用、用量和终态，
   但答案正文只在复核成功后进入 `AnswerCommitted`；撤权时只写安全的
   `AnswerWithheld`。
2. **source revision 读取栅栏**：Qdrant 候选除当前可读外，还必须满足
   `candidate.source_revision == PostgreSQL 当前 source_revision`。旧 revision 和不可能的
   future revision 都不能进入 `ContextPacket`。

同时修正了三项相邻的 Chat 语义：

- failed/cancelled AgentOutcome 不再保存空 assistant 消息或返回 HTTP 200；API 分别返回
  结构化的 502/504/409 终态；
- Route、`AgentRunRequest`、EventScope、AgentOutcome 和 API response 共用同一个
  `run_id`，Chat stream 固定为 session ID；
- session ownership 在 embedding/Qdrant 检索前验证，猜测 session ID 不能触发昂贵检索。

确定性测试覆盖：

```text
正常路径：ModelCompleted 正文为空
        → ACL/evidence confirm
        → Chat Turn 写入内部 release_pending（History 不可见）
        → AnswerCommitted 含最终答案
        → Turn 转 committed 并追加可见 assistant

撤权路径：模型已生成秘密文本
        → confirm_unchanged 失败
        → HTTP 只返回拒答
        → History / EventLog / SSE 均不含秘密
        → AnswerWithheld 只含安全替代文本

失败路径：RunFailed/RunCancelled
        → History 只保留 user message
        → 无 AnswerCommitted/AnswerWithheld
        → API 非 200
```

2026-07-28 本地质量门：

```text
ruff format --check .    passed（178 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
pytest                   711 passed / 188 skipped / 1 environment failure
```

唯一失败仍为受限沙箱禁止 `socket.bind()` 的 loopback 真实性测试；与本轮代码无关。
外部 PostgreSQL、Qdrant 和真实 BGE 权重未配置的测试按契约跳过。

本轮**没有**宣称完成：

- Qdrant 旧 Point 物理 replace/delete；当前已做到“不可读取”，尚未做到“已清理”；
- Ingestion Worker 的 advisory lock、heartbeat、retry/dead-letter 和常驻进程；
- Agentic `knowledge_search` 的 run-scoped evidence ledger 与最终提交门；
- 历史 token window/compaction、模型实际引用校验；幂等 `chat_turns` 和并发
  Turn 非交错已由后续 PR-039 补齐；
- LangGraph Task、Task Registry、Multi-Agent、CrewAI benchmark。

## 2026-07-25 仓库核验总览

核验基线：`main@f071323`，PR-001～PR-015 与 ADR-012 已合并。当前配置登记
231 个 Settings 叶子字段、47 个组。

已经实现并有测试证据：

- Domain、Ports、Fake Adapter、可复现 CLI；
- 自研 Runtime 的串行 Tool Loop、Policy/Tool Gateway、预算与取消、并行只读、
  exclusive 屏障和 Hook Bus；
- DeepSeek 流式 HTTP Adapter 的离线 contract tests；
- PostgreSQL ConversationStore、Document/Version/ACL、事务 Outbox；
- Local ArtifactStore 与 Upload / Artifact / Health API。

明确尚未实现：

- Chat RAG、LlamaIndex、Qdrant、BGE、RAGAS；
- LangGraph Task、Task Registry、lease/fencing/checkpoint、Multi-Agent；
- 生产身份认证、S3、Worker、SSE、UI、Docker Compose；
- DeepSeek 的进程级装配和真实服务 E2E。

本次门禁共收集 544 项测试。无数据库环境为 499 passed、45 skipped；PostgreSQL
16 集成套件为 160 passed，Alembic `0001 → 0002` 通过。两组测试有重叠，不能
相加。Ruff、Pyright、三 profile、CLI golden、许可证、Gitleaks 与 Actionlint
均通过；GitHub Actions 证据见
[run 30184299195](https://github.com/he-zi-qiang/agent-workbench/actions/runs/30184299195)。

当前阻断项：

1. ~~默认 `api.host = "0.0.0.0"`，开发 Header Identity Resolver 可能被本机
   之外的调用者访问并伪造身份~~（2026-07-26 已修复，见下方 P0-1 一节）；
2. ~~`requires_approval=True` 没有阻止 write tool 执行~~（2026-07-26 已修复，
   见下方 P0-2 一节）；
3. ~~Upload/Document/Artifact 缺少同 tenant 内的 owner/ACL 对象授权~~
   （2026-07-26 全部修复，见下方 P1-1、P1-3、P1-2 三节）；
4. ~~tool/token/cost budget 不是硬上限~~（2026-07-26 已修复，见下方 P1-5 一节）；
5. ~~Policy 改写可绕过参数字节上限，Policy/Hook deadline 不完整~~
   （2026-07-26 已修复，见下方 P1-6 / P1-7 一节）；
6. ~~DeepSeek 对损坏 SSE frame fail open，Artifact 下载不是真正流式~~
   （2026-07-26 均已修复，见下方 P1-9 / P1-4 两节）；
7. ~~Outbox claim 没有 lease/fence，worker 崩溃后不可恢复~~
   （2026-07-26 已修复，见下方 P1-10 一节）。

**七条阻断项已全部关闭**，核验报告的 14 条缺陷同样全部修复并附回归测试。每一条的
触发条件、判断依据、有牙验证结果与**明确留下的缺口**记在下面各自的小节里；报告的
§7 是同一份记录的另一半。

完整触发条件、文件位置和建议修复顺序见
[仓库核验报告](./repository-audit-2026-07-25.md)。核验那一轮只校正文档，没有改
生产逻辑；此后的修复各自一个 PR，逐条记在下面，并同步回核验报告的第 7 节。
纪律是：**没有覆盖触发条件的回归测试，任何一条都不算关闭。**

## PR-001 Bootstrap

状态：**已实现并通过本地测试，已纳入 Git 版本管理**。

已交付：

- Python 3.12 src-layout 包；
- `pyproject.toml` 和 `uv.lock`；
- 已迁移并适配正式目录的配置基线；
- 脱敏的 `agent-config-check` 入口；
- clean-room 与合规文档；
- 使用 `main` 分支的独立 Git 仓库。

2026-07-23 验证证据：

```text
uv 0.11.31
Python 3.12.13
uv lock --check --offline: passed
pytest: 43 passed
agent-config-check test profile: status=ok
non-editable install with packaged default config: passed
```

仍不属于 PR-001 的内容：

- CI 和配置 ownership 架构测试；
- API、进程 Container 和 readiness；
- Runtime、Domain 与 Ports；
- Persistence、RAG、Workflow 与 Multi-Agent；
- Docker Compose 和外部服务连通性。

## PR-002 Config CI

状态：**已实现并通过本地同构检查**。

已交付：

- 覆盖 230 个 Settings 叶子字段的 `config/ownership.yaml`；
- ownership 唯一性、生命周期和 Task snapshot 正向 allowlist 架构测试；
- 核心层框架依赖、反向依赖、原始配置读取和 `os.environ` 边界测试；
- development、test、production 三个离线配置 profile；
- Ruff、严格 Pyright（产品源码）、pytest、许可证与 Git 历史密钥扫描 CI；
- GitHub Actions 与下载型工具都固定到 release SHA 或 SHA-256；
- 删除没有消费者的 `admin_token`、`webhook_token` 配置入口。

2026-07-23 本地验证证据：

```text
uv lock --check --offline: passed
ruff format --check: passed
ruff check: passed
pyright: 0 errors, 0 warnings
pytest: 60 passed
development/test/production config profile contract tests: passed
dependency license allowlist: passed
Gitleaks 8.30.1 working-tree/history scan: passed
```

该 PR 只证明配置和 CI 合同，不代表 API、Worker、模型或外部 Adapter 已经
启动。

## PR-003 Domain

状态：**已实现并通过本地测试**。

已交付 `src/agent_workbench/domain/`，只依赖标准库与 Pydantic：

- `schema.py`：`DomainModel`/`VersionedModel` 基类、schema 版本闭合校验、
  `extra="forbid"`、frozen 与 `hide_input_in_errors`；
- `identifiers.py`：平台自铸 ID 前缀与生成器，同时接受 provider 原样
  ID（`toolu_...`）；
- `errors.py`：13 个稳定 `ErrorCode`、`ErrorInfo` 与领域异常层级；
- `artifacts.py`：`ArtifactRef`（content-addressed，不含 URL/路径）；
- `messages.py`：provider-neutral 消息与 text/tool_use/tool_result 块；
- `tools.py`：`ToolSpec`/`ToolCall`/`ToolResult` 与 `align_results()`；
- `context.py`：`ContextPacket`/`ContextChunk`/`Citation`/`SourceLocator`；
- `policies.py`：`PrincipalContext`、`AuthorizationEnvelope`、
  `ExecutionContext`、`PolicyDecision`；
- `runs.py`：`TraceContext`、`RunBudget`/`BudgetUsage`/`TokenUsage`、
  `AgentRunRequest`、`AgentOutcome`、Runtime 状态机与 `StopReason` 词汇表；
- `events.py`：基线 6.7 的 19 种事件、统一 envelope 与 durability 映射。

已经被编码为构造期校验（而不是注释）的不变量：

- 每个 `ToolCall` 恰有一个 `ToolResult`，提交顺序按模型调用顺序稳定；
- write/external/destructive Tool 必须 exclusive 且声明 permission scope；
- 失败 `ToolResult` 必须携带 `ErrorInfo`，成功的必须不携带；
- `ModelDelta`/`ToolProgress` 恒为 transient 且不得携带 stream sequence，
  durable 事件必须携带；`event_type` 必须与 payload 一致；
- Citation 必须落在同一 `ContextPacket` 的 chunk 与相同 document version 上；
- 预算耗尽的 run 报告为 failed + 结构化 `stop_reason`，不伪装成 completed；
- 第三方异常转 `ErrorInfo` 时只保留异常类型名，丢弃消息正文。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 219 passed（其中 domain 新增 159 项）
development/test/production config profile: status=ok
golden 序列化基线：tests/domain/golden/domain_v1.json（13 个聚合）
```

本 PR 不新增依赖，`pyproject.toml` 与 `uv.lock` 未改动，`uv lock --check`
由 CI 执行。

仍不属于 PR-003 的内容：Ports 与 Fake Adapter（PR-004）、CLI 纵向切片
（PR-005）、Runtime 循环本身（PR-006 起）。领域对象存在且有契约测试，
按项目纪律只能标记为 Implemented/Tested，不能标记为 Demonstrated。

## PR-004 Ports + Fakes

状态：**已实现并通过本地测试**。

`src/agent_workbench/ports/`（框架无关 Protocol）：

- `model.py`：`ModelRequest`、`ModelEvent`（text_delta / tool_call / usage /
  completed）、`ModelPort.stream()`；
- `tools.py`：`ToolHandler`、`ToolInvocation`、`ToolBinding`、`ToolRegistry`；
- `agent_executor.py`：`AgentExecutor.run(request, emit, cancellation)`；
- `policy.py`：`PolicyEngine.decide(call, context)`；
- `event_log.py`：`EventScope`、`EventCursor`、`EventLogPort`、`EventSink`；
- `conversation_store.py`：`ConversationSession`、`StoredMessage`、
  `ConversationStore`；
- `artifact_store.py`：`ArtifactStore`；
- `cancellation.py`：`CancellationToken` 协议、`CancellationSource`、
  `NullCancellationToken`。

`src/agent_workbench/adapters/`（无外部依赖的实现）：

- `models/fake.py`：脚本化 `FakeModel`（可重放最后一轮，供死循环测试）；
- `tools/`：`StaticToolRegistry` 与两个无副作用 Tool（`read_document`、
  `text_statistics`）；
- `memory/`：`InMemoryEventLog`、`InMemoryConversationStore`、
  `InMemoryArtifactStore`；
- `policy/envelope.py`：`EnvelopePolicyEngine`（deny-by-default 的过渡实现）；
- `events.py`：`ScopedEventSink`；
- `testing.py`：`fake_stack()` 组合出完整栈，字段类型全部写成 Port，
  由 Pyright 静态验证结构一致性。

本 PR 固定下来的行为：

- **sequence 由 log 分配**，durable 事件在流内单调无洞，transient 事件返回后
  即丢弃且不占用 sequence——SSE cursor 因此是可信的续传位置；
- **跨租户读与不存在完全同形**（同一 `NotFoundError`、同一文案），
  artifact 与 conversation 都有针对性测试；
- `ModelRequest` 只携带 model profile，不含 model id / temperature / key；
- Tool call id 从 provider 原样穿过 proposal → 执行 → tool 消息 → 下一次
  ModelRequest（`test_fake_stack.py` 端到端断言）；
- `EventCursor.decode()` 对畸形输入统一 fail closed，且不解释拒绝原因。

领域侧的唯一改动：新增 `not_found` 错误码与 `NotFoundError`，用于统一
"不存在 / 无权知道它存在"两种情况。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 290 passed（contracts 新增 71）
development/test config profile: status=ok
```

测试用 `asyncio.run()` 驱动协程，**不引入 pytest-asyncio**，
`pyproject.toml` 与 `uv.lock` 保持不变。

按各自工作包推迟的 Port（不在本 PR 冻结）：`IngestionPort`、
`RetrieverPort`、`EmbeddingPort`、`SparseEncoderPort`、`RerankerPort`、
`DocumentStore`、`OutboxPort`（WP03–WP05）；`TaskRegistry`（WP07）；
`ApprovalStore`（WP10）；`TelemetryPort`（WP12）；`SandboxPort`、
`MemoryPort`（更后）。先冻结一个没有实现来校验的协议，正是契约在第一个
使用者出现前就漂移的方式。

## PR-005 CLI Skeleton

状态：**已实现并通过本地测试**。

第一个跑通的纵向切片：输入 → 脚本化模型 → 统一事件 → 输出。

已交付：

- `src/agent_workbench/apps/cli/`：`main.py`（argparse 与退出码）、
  `demo.py`（确定性场景装配）、`rendering.py`（文本与 JSONL 两种渲染器）；
- `agent-cli` console script，CI 用已安装的 wheel 跑 smoke 并与 golden 比对；
- `src/agent_workbench/adapters/agents/single_turn.py`：
  `SingleTurnAgentExecutor`，满足 `AgentExecutor` 协议的 walking skeleton；
- `adapters/events.py` 增加 `ObservingEventSink`（live tee，让订阅者看到
  transient 事件）；`InMemoryEventLog` 与 `fake_stack()` 增加可注入的
  `event_ids`，与既有 `clock` 注入同一目的；
- 领域侧新增 `canonical_arguments()` / `argument_digest()`：事件记录参数摘要
  而不是参数本身，WP10 的副作用 ledger 复用同一份规范形式。

本 PR 固定下来的行为：

- **CLI 只消费统一事件与返回的 `AgentOutcome`**，不触碰 executor、模型
  adapter 或 store 的内部；
- **两个视图取自不同来源**：流式回答来自 live transient delta，时间线来自
  运行结束后对 durable log 的重放。重放拿不到的东西，重连的客户端也拿不到；
- **skeleton 不拥有 tool loop，并且拒绝得很大声**：模型提出工具调用时，先落
  一条 durable `ToolProposed`，再让整个 run 失败。默默丢弃会让模型永远等一个
  不会到来的 ToolResult，正是不变量 1 要防的情况；
- 预算在开工前检查（过期 deadline 时模型调用次数为 0）；
- adapter 异常变成结构化 outcome，且只保留异常类型名——
  测试用 `sk-ant-` canary 断言 provider 异常正文不进入 outcome 与事件；
- `max_tokens` 截断的回答报告为 failed，不伪装成 completed；
- 事件里没有 prompt 正文，测试用 canary prompt 断言 JSONL 中不出现。

demo 模式冻结时钟、用计数器生成事件 ID，因此**同一条命令的输出逐字节可复现**，
由 4 个 golden 文件（文本/JSONL × 完成/拒绝）守护。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 328 passed（cli 19 + 单轮 executor 19）
development/test config profile: status=ok
agent-cli demo / --propose-tool read_document: 退出码 0 / 1
```

`pyproject.toml` 只增加 `agent-cli` 入口，未新增依赖，`uv.lock` 未改动。

范围说明：demo 不加载 Settings。它不连数据库、向量库和任何 provider，要求注入
DSN 才能看一句脚本化回答属于仪式而非安全；从校验过的配置做真实依赖注入是
`bootstrap/container.py` 的职责，与第一个真实 adapter 一起落地。真正的
model-tool 循环仍属于 WP02（PR-006 起），本 PR 没有、也不允许提前实现第二个
循环。

## PR-006 Runtime Serial Loop

状态：**已实现并通过本地测试**。

自研 `ClaudeLikeAgentRuntime` 落地，替换 PR-005 的 walking-skeleton executor
（`SingleTurnAgentExecutor` 及其测试已删除，不与新 Runtime 并存）。

已交付 `src/agent_workbench/runtime/`，只依赖 domain 与 ports：

- `state.py`：把基线 7.1 的状态机写成可执行转移表 + `RunStateMachine`。
  非法转移抛 `InvalidStateTransition`，因此“跳过授权直接执行”这类实现错误
  在第一次发生时就会失败，而不是产出一条看起来合理的运行记录；
- `tool_executor.py`：单次已授权调用的执行原语，负责超时与异常归一化；
- `agent_runtime.py`：串行 `model → tool → result → model` 循环本体。

本 PR 固定下来的不变量：

- **每个已暴露的 `tool_call_id` 恰有一个 `ToolResult`**——未知工具、被拒、
  handler 抛异常、超时、批次中途取消，全部产出结果而不是留空。少一个结果，
  模型就会永远等一个不会到来的回答；
- **提交顺序永远是模型的调用顺序**：串行执行下两者本就一致，仍然强制过一次
  `align_results()`，这样 PR-009 的并行调度器无法通过“谁先跑完”改变模型看到
  的内容；
- **超时属于 executor 而不是循环**：串行循环没有别的办法从一个不返回的
  handler 里恢复，预算约束的是花费不是单次 await 的时长；
- **Tool 只有经过 PolicyEngine 才会执行**：deny 分支下 handler 调用数为 0；
  `allow_with_modified_input` 目前直接拒绝——重写后的参数必须重新做 schema
  校验与再授权，而做这件事的 Tool Gateway 还没到，所以宁可拒绝也不在未校验
  的输入上执行；
- **循环有上限**：`max_steps` / `max_tool_calls` / deadline 在每轮开工前检查，
  一个永远提工具调用的模型会被 `max_steps` 终止（有测试）；
- handler 越权回答别的 `tool_call_id` 时，只让它自己那次调用失败，不会同时
  破坏两个 id 的配对。

CLI demo 随之升级为完整一轮：`agent-cli demo` 现在演示
模型 → `read_document` → ToolResult → 模型 → 回答；`--deny` 演示 deny 分支
（handler 不被调用，模型仍收到该 id 的答复）；`--tool none` 回到单轮文本。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 360 passed（runtime 新增 49）
development/test config profile: status=ok
agent-cli demo / --deny / --tool none: 退出码 0
```

`pyproject.toml` 与 `uv.lock` 未改动。测试套件里有且只有一处等待真实时间：
tool 超时测试固定耗时 1 秒（`ToolSpec.timeout_seconds` 的域下限就是 1 秒），
是有界且确定的，不是靠 sleep 碰运气的竞态测试。

范围说明：本 PR 不含 **schema 校验**与 **Hook 重校验**（两者一起定义“最终参数”
的含义，属于 PR-007 Tool Gateway）、**并行只读调度**（PR-009）、大结果
Artifact 化（WP12）、真实 Anthropic Adapter（WP02-06）。因此基线不变量 2 目前
只实现了授权那一半；demo 中的两个 Tool 自己校验参数类型，`invalid_tool_input`
路径由它们覆盖。

## PR-007 Policy + Tool Gateway

状态：**已实现并通过本地测试**。

把“这次调用能不能跑”收进唯一的 `ToolGateway`，并补上基线不变量 2 与 3
缺的那一半：schema 校验与参数被改写后的重新校验/再授权。

已交付：

- `runtime/schema_validation.py`：一个**明确划定范围**的 JSON Schema 子集
  （type / properties / required / additionalProperties / items / enum /
  minimum / maximum / minLength / maxLength / pattern / minItems / maxItems）。
  超出子集的 schema 在**装配阶段**被拒绝，而不是在调用时被静默跳过——一个会
  忽略 `oneOf` 的校验器，会把每次调用都报成合法却什么都没校验；
- `runtime/tool_gateway.py`：`propose / prepare / authorize / invoke / refuse`
  五个阶段方法 + `advertise()`。Runtime 的批次循环退化成对它的编排，状态机
  仍留在 Runtime；
- `agent_runtime.py` 相应瘦身：不再直接持有 registry / policy / executor，
  只依赖 `ToolGateway`（构造签名随之简化为 `model` + `gateway`）。

本 PR 固定下来的行为：

- **handler 只在“最终参数”同时通过 schema 校验与授权决定之后才运行**。
  “最终”是关键词：策略返回 `allow_with_modified_input` 时，重写后的参数会被
  重新校验并重新提交决定；能在检查之后改参数，就等于同时绕过两道检查；
- **重写循环有界**（默认 3 轮）。一直改参数的引擎会被拒绝，而不是被无限重试；
- **重写成非法参数时调用被拒**，handler 调用数为 0（有测试）；
- **顺序可观测**：schema 校验先于授权，所以参数非法的调用连 `PermissionResolved`
  都不会产生——`tests/runtime/test_agent_runtime.py` 直接断言这条时间线；
- **参数体积上限**（默认 65536 字节，对应 `policy.max_tool_argument_bytes`）在
  解析之前拦截；
- **校验错误只报路径和期望，不报值**：这些消息会进入事件、运维日志和模型自己的
  上下文，用 canary 测试守住；
- Gateway 同时拥有一次调用的审计轨迹（proposal / permission / start /
  completion / failure），事件因此不可能和它实际做的事不一致。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 398 passed（新增 schema 24 + gateway 14）
development/test config profile: status=ok
CLI golden 文件逐字节未变——这次重构对 demo 行为完全等价
```

`pyproject.toml` 与 `uv.lock` 未改动：JSON Schema 子集是自研的，没有引入
`jsonschema` 依赖。工具 schema 都写在本仓库里，为了 8 个关键字引入一个完整
实现并不划算；代价是子集必须显式，扩展它是这个文件里的一次自觉修改。

范围说明：不含 **Hook Bus**（WP02-05，`ports/hooks.py` 与 `runtime/hook_bus.py`
尚未存在）——目前唯一能改写参数的来源是 PolicyEngine，重新校验/再授权的骨架
已经就位，接 Hook 时复用同一条路径。同样不含并行只读调度（WP02-04 / PR-009）、
大结果 Artifact 化（WP12）与真实 Model Adapter（WP02-06/07）。

## PR-008 Runtime Budgets

状态：**已实现并通过本地测试**。

PR-006 已经做掉了循环上限（`max_steps` / `max_tool_calls` / token / cost）与
轮次边界上的取消。本 PR 补的是**时限**：在实施计划 5.2 之前，一个停住不动的
模型流会让整个 run 无限期挂起——预算约束的是花费，不是单次 await 的时长。

已交付：

- `runtime/budgets.py`：纯粹的 deadline 运算。
  `effective_model_deadline()` 返回各层下界的最小值**以及它来自哪一层**；
  `effective_tool_timeout()` 用 run 剩余时间约束单个工具；
- Runtime：单次模型调用套 `asyncio.timeout(有效 deadline)`；流式过程中在每个
  事件边界检查取消；退出时显式 `aclose()` 生成器；
- ToolExecutor / Gateway：`run_budget_seconds` 一路传到执行处。

本 PR 固定下来的行为：

- **有效 deadline 是一个下界，不是几个互不相关的定时器**：
  `min(运行时 envelope, run 剩余时间)`。模型 profile 自己的超时由 Adapter 在更
  内层施加——只有 Adapter 知道 profile 映射到哪个具体模型——两层嵌套，短的先触发；
- **哪一层到期决定了怎么报**：run deadline 到期是预算结果（`budget_exceeded` +
  `stop_reason="deadline"`，run 结束）；超出 envelope 是 provider 问题
  （`provider_error`，`retryable=True`）。两者相等时按 run deadline 报，因为它的
  到期后果更严重。同一个"模型卡住"的场景有两个测试，只有配置不同、结论不同；
- **工具不能活得比批准它的 run 更久**：`min(工具声明超时, run 剩余时间)`，
  且每次调用重新计算——第一个工具跑得慢，第二个能用的就更少；
- **取消在下一个事件边界生效**，并通过关闭生成器传导到 Adapter（测试用生成器
  `finally` 里的标志位断言 `closed is True`）。模型完全静默时由 deadline 兜底，
  所以"有界时间内停止"两条路径都有覆盖；
- 卡住的模型调用**不会**产生 `ModelCompleted`——它确实没有完成。

取消测试不使用 sleep：cancel 是在事件 sink 观察到第一个 `ModelDelta` 时发出的，
因此交错是确定的。三个超时测试各等 50 毫秒真实时间，有界且必然触发。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 414 passed（新增 budgets 12 + runtime 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变
```

范围说明：三层 retry 计数（model / graph node / task）中只有 model 这一层的
deadline 在本 PR 内；node 与 task 层属于 WP06/WP08。真实 Adapter 的
`max_retries` 要等 WP02-06 的 Anthropic Adapter。Hook Bus（WP02-05）与并行只读
调度（WP02-04）仍未实现。

## PR-009 Parallel Reads

状态：**已实现并通过本地测试**。

补上基线不变量 4 与 5：`parallel` 工具可以并发，`write/external/destructive`
必须过 exclusive 屏障；执行顺序与提交顺序彻底分离。

已交付：

- `runtime/tool_scheduler.py`：`plan_tool_batches()` 是一个**纯函数**——
  接收一批已授权调用，返回执行分组。并发 bug 难观察更难复现，所以"谁能和谁
  一起跑"这个决定放在不需要事件循环就能验证的代码里；
- Runtime 执行阶段按组推进：组内 `asyncio.gather`，组间顺序执行。

分组规则只有两条：

1. **保持模型的顺序**：从左到右扫描，连续的 parallel 调用累积成一组，因此
   排在写操作之前的读，绝不会在它之后执行；
2. **exclusive 调用自成一组**：写/外部/破坏性工具在领域层就被强制为
   exclusive（`ToolSpec` 构造期校验），所以"副作用要过屏障"从文档里的一句话
   变成了内存里的形状——它两侧都不会有东西在飞。

本 PR 固定下来的行为：

- **提交顺序永远是模型的调用顺序**，与完成顺序无关。测试用一对握手 handler
  证明：`waits` 必须等 `opens` 打开闸门才能返回，所以完成顺序是
  `[fast, slow]`，而提交给模型的顺序是 `[slow, fast]`；
- **并发是被证明的，不是被假设的**：上面那对 handler 在串行调度下会互相
  等死（由工具超时兜底），因此这个测试同时证明了"确实并发"和"顺序稳定"；
- **exclusive 工具旁边永远没有别人**：探针记录同时在场的 handler 数，
  写工具那一格恒为 1；
- `max_parallel_read_tools`（对应配置同名字段，默认 4）限制同时在场数量；
- 取消发生在组与组之间：**已在飞的组保留真实结果**，尚未开始的组拿到
  cancelled 结果——取消停止未开始的工作，不改写已经发生的事。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 428 passed（新增 scheduler 9 + runtime 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变
```

范围说明：CLI demo 仍然只调用一个工具。M1 验收里"两个只读 Tool"的固定演示
留到评测/演示工作包——golden 文件是逐字节比对的，把它钉在并发执行的事件交错上，
等于承诺一个调度器并不保证的顺序。并发性由上面的握手测试证明，那是更强的证据。

不含 Hook Bus（WP02-05）与真实 Model Adapter（后来确定为 DeepSeek，
WP02-06/07）。WP02 至此
只剩这两项。

## PR-010 Hook Bus

状态：**已实现并通过本地测试**。

补上 WP02-05：部署方提供的 Hook 可以在工具调用被判定之前检查、改写或拦截它。
WP02 至此只剩真实 Model Adapter。

> 编号说明：原计划 §9 的首批 PR 列表把 Hook Bus 留在 WP02 内部未编号，
> PR-010 是 PostgreSQL/Artifact Base。实施时把 Hook Bus 排进 PR-010，其后
> Provider 占用 PR-011～PR-012，持久化与上传车道落在 PR-013～PR-015，
> 计划文档已同步。

已交付：

- `ports/hooks.py`：`ToolCallHook` 协议与 `HookOutcome`（不变 / 改写参数 /
  拦截）。Hook **返回决定，不修改传入的调用**；能改的只有 arguments——
  工具名和 call id 属于模型的请求和那条必须回答它的结果；
- `runtime/hook_bus.py`：按注册顺序跑一遍，每个 Hook 看到的是上一个产出的结果。

本 PR 固定下来的行为：

- **Hook 改写过的参数重新走 schema 校验，然后才进授权**——能在检查之后改参数，
  就等于绕过检查。这条路径和 PolicyEngine 的 `allow_with_modified_input`
  复用同一套 Gateway 逻辑（PR-007 就是按这个形状搭的）；
- **失败即拦截，不是忽略**：Hook 抛异常或超时，被检查的那次调用直接被拦下。
  反过来——把坏掉的 Hook 当成放行——会让部署方写的每条安全规则在出 bug 的
  瞬间静默消失；
- **Hook 有时限**（默认 5 秒），和它守护的工具同样理由：一个无界 await 就能
  让一条慢规则拖住整个 run；
- **只跑一遍**：后面的 Hook 改写不会重新触发前面的 Hook。多轮需要一条收敛
  规则，而说不清的规则等于没人能 review 的规则；
- **Hook 看不到已经失败的调用**：未知工具、参数非法的调用在 Hook 之前就被拒，
  Hook 负责塑形合法输入，不是给坏输入当第二个解析器；
- Hook 异常正文不外泄，只保留异常类型名（`sk-ant-` canary 测试）；
- Hook 名字必须唯一——审计行必须能说清是谁拦的。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 445 passed（新增 hook_bus 12 + gateway 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变（demo 不装 Hook，空 bus 是 no-op）
```

范围说明：Hook 目前在代码里装配，没有对应的 Settings 字段——`runtime.*` 里
没有 hook 相关配置，因此 `config/ownership.yaml` 无需改动。把 Hook 变成可配置
（以及 `after_tool` / 会话级 Hook）要等 Bootstrap Container 能注入它们时再说。
本 PR 只实现 `before_tool` 一个阶段。

## PR-011 DeepSeek Provider Contract

状态：**已实现并通过本地测试**。

模型 Provider 由 Anthropic 改为 **DeepSeek**（OpenAI 兼容协议）。

这次改动最值得记的一点是：**Runtime、Gateway、Scheduler、Domain 一行都没改。**
换 Provider 只动了配置契约和（尚未实现的）Adapter，这正是 `ModelPort` 存在的
理由。相应地，它也不是改一行能完事的——`model.provider` 是被冻结成 `Literal`
的，`configuration.md` 早就写明"新增 Provider 时升级配置 schema，而不是接受
一个无法启动的 provider 字符串"，所以这是一次 schema 迁移。

已交付：

- `model.provider: Literal["deepseek", "fake"]`（`anthropic` 已移除——没有
  Adapter 的 provider 字符串不该是可配置的）；
- 新增 `model.base_url`，默认 `https://api.deepseek.com`；沿用与 Qdrant/OTel
  同一套 endpoint 校验（禁 userinfo / query / fragment）；
- `secrets.deepseek_api_key` 取代 `anthropic_api_key`，同步更新
  `SECRET_LIKE_ENV_KEYS`、脱敏键表、mounted-secret 文件名与 CI 环境；
- `config_schema_version` `1.1` → `1.2`；
- `config/ownership.yaml`：新增 `model.base_url`（owner
  `adapters.model.deepseek`，lifecycle `startup`）与 `secrets.deepseek_api_key`。

顺带修掉了一个**潜伏的快照违规**：Task snapshot 的正向 allowlist 原本写的是
`model.*`，而 `configuration.md` §7 明确规定 endpoint 不得进入恢复快照。加上
`model.base_url` 会让它直接违规，所以 allowlist 收窄为
`model.provider` / `model.main.*` / `model.compact.*`，`run_semantics_snapshot()`
显式剔除 `base_url`。**迁移端点不改变一个在跑的 Task 的语义，恢复旧 Task 也不会
连回它当初的端点**——两条都有测试。

新增的安全规则：

- **`model.base_url` 必须是 HTTPS，除非指向 loopback**。每一次请求都带着
  provider API key，所以这条比 Qdrant 那条更严（Qdrant 只在 `remote` 时强制
  HTTPS，模型端点则是无条件的，只给本地兼容服务开口子）。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 452 passed（config 新增 7）
development/test/production config profile: status=ok（config_schema_version=1.2）
CLI golden 文件逐字节未变
```

`pyproject.toml` 与 `uv.lock` 未改动——本 PR 不引入任何依赖。

范围说明：**Adapter 本体不在本 PR 内**。DeepSeek 的流式接口需要一个 HTTP
客户端（`httpx`），也就是需要新依赖与重新生成的 `uv.lock`，而当前工作环境里
没有 `uv`。`tests/architecture/test_dependency_boundaries.py` 已经预先把
`httpx`、`openai`、`deepseek` 加进核心层禁止导入清单，Adapter 落地时不会悄悄
渗进 Runtime。

另注：CI 的依赖许可证 allowlist 目前是
`MIT / BSD-2 / BSD-3 / Apache-2.0 OR BSD-2-Clause / PSF-2.0`，**不含纯
Apache-2.0**。选 HTTP 客户端时需要一并确认许可证，或显式扩展 allowlist。

## PR-012 DeepSeek Model Adapter

状态：**已实现并通过本地测试**。

第一个与进程外世界通信的 Adapter，也是第一个引入外部依赖的 PR。

已交付：

- `adapters/models/deepseek.py`：`DeepSeekModel` 实现 `ModelPort`，走 DeepSeek
  的 OpenAI 兼容 chat completions 流式接口；
- `httpx` 依赖（BSD-3）与重新生成的 `uv.lock`；
- CI 许可证 allowlist 扩展（见下）。

本 PR 固定下来的行为：

- **工具调用攒齐才发**：provider 把一次调用的 JSON 参数拆成多个分片流下来，
  Adapter 按 index 缓冲，直到流声明结束才组装成 `ToolCall`。半截 JSON 绝不能
  进 schema 校验和策略——`ports/model.py` 的契约就是这么写的，现在有实现来兑现；
- **每条流都以 `ModelStreamCompleted` 结尾**，包括失败的那些。让调用方去区分
  "provider 停了"和"adapter 抛了"，就是让它在某处一定弄错；
- **线上的东西一个字都不往回引**：HTTP 错误只带状态码，不读也不引用响应体——
  聊天补全的错误体可能把发出去的 prompt 原样回显，而错误文本会流进事件、日志和
  模型自己的上下文（canary 测试守住）；
- 传输层异常只保留异常类型名（URL 及其 query 可能出现在消息里）；
- **不认识的 finish_reason 报错而不是猜**（例如 `content_filter`）；
- 参数无法解析、缺 id 或缺名字 → 报 provider 错误。猜一个参数等于把模型从没要求
  过的东西送到 handler 面前；
- `stream_options.include_usage` 必开：拿不到 token 账目的 run 没法执行 token
  预算。DeepSeek 的 `prompt_cache_hit_tokens` 也一并映射（其他兼容服务上缺省为 0）。

测试用 `httpx.MockTransport` 喂**真实线格式字节**，CI 依然完全离线。其中一条端到端
测试直接断言 **Runtime 分不出它和脚本化模型的区别**：同样的两轮工具循环，durable
时间线与 `tests/runtime` 里 FakeModel 那条逐项相同。这是 `ModelPort` 这层抽象的
验收标准，不是"我检查过了"。

### 许可证 allowlist 扩展

加 `httpx` 时 CI 的许可证门禁拦下了它的传递依赖 `certifi`（MPL-2.0）。核查后发现
这不是 httpx 的问题——**原 allowlist 与项目自己选定的技术栈冲突**：

| 依赖 | 许可证 | 原 allowlist |
|---|---|---|
| certifi | MPL-2.0 | ❌ |
| asyncpg | Apache-2.0 | ❌ |
| qdrant-client | Apache-2.0 | ❌ |
| opentelemetry-sdk | Apache-2.0 | ❌ |

PR-002 那份清单看起来是照着当时已有的 21 个包写的，是描述而非策略。现在把策略写
明确（见 `docs/compliance.md`）：允许宽松许可证与文件级弱 copyleft，拒绝强
copyleft（GPL/AGPL/LGPL）与未声明许可证。allowlist 同时列 SPDX 与 classifier
两种拼写，因为 `pip-licenses` 只照搬包元数据里写的那种。

2026-07-25 验证证据：

```text
uv lock --check --offline: passed
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 473 passed（deepseek 新增 21）
dependency license allowlist: passed
CLI golden 文件逐字节未变
```

`uv lock --check` 与许可证门禁是前十个 PR 里唯一只能靠 CI 验证的两项，本次起可以
在本地跑（`uv` 按 CI 固定的 0.11.31 装在一次性 venv 里，未污染项目 `.venv`）。

范围说明：Adapter 尚未接进 Bootstrap——`base_url`、API key 与 profile 目前由构造
参数注入，从校验过的 Settings 组装是 `bootstrap/container.py`（WP00-03）的职责。
LangChain model/tool 互操作 Adapter（WP02-07）仍未实现。**model ID 与工具调用支持
情况请对照 DeepSeek 当前文档确认后再投产**；本 PR 的 contract test 钉住的是线格式
处理，不是某个具体模型的能力。

## PR-013 PostgreSQL/Artifact Base

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。

第一次有了真正的持久化：WP03-01～03（session factory + Alembic、PostgreSQL
ConversationStore、本地 ArtifactStore）。

已交付：

- `adapters/persistence/`：`models.py`（SQLAlchemy **Core** 表，不是 ORM——
  仓储的职责就是把行显式映射成领域对象，identity map 和惰性加载只会再引入一
  套隐式的"何时读"）、`engine.py`、`conversation_store.py`；
- `migrations/` + `alembic.ini`：`0001_conversations`；
- `adapters/artifacts/local.py`：`LocalArtifactStore`。

本 PR 固定下来的行为：

- **位置在锁住会话行的前提下分配**：同一会话的两次 append 在锁上串行，
  `sequence` 因此是**无洞的**而不只是唯一的。数据库另有
  `UNIQUE(session_id, sequence)`——锁一旦被绕过，写入会失败而不是悄悄复用位置。
  有测试并发发 5 条断言拿到 `[1,2,3,4,5]`；
- **消息按领域对象存取**（JSONB，含 schema 版本），读回时过同一个模型。
  用本进程不认识的契约写下的行会在边界上 fail closed，而不是半懂不懂地进入
  模型上下文；
- **`statement_timeout` 设在连接级**而不是每条查询：逐条设置的超时迟早会有人忘；
- **本地 Artifact 先写 blob 再写元数据**——中途崩溃留下的是一个没人引用的
  blob（垃圾），而不是一个指向不存在字节的引用（谎言）；
- **元数据放在 blob 旁边而不是建表**：目前除了按 id 点查没有任何查询，
  只服务点查的表是"伪装成索引的 schema"。等上传落地、artifact 行必须与
  document version 同事务写入时，那张表才有存在的理由；
- 跨租户读与不存在**完全同形**——沿用 in-memory 既有的常量文案（不含 id）。

契约测试重构成**一套跑两种实现**：`tests/contracts/` 的 conversation 与
artifact 套件现在参数化在 `memory`/`postgres` 与 `memory`/`local` 上。一个 Port
有两个实现和两套测试，就等于有两个契约。

`tests/persistence/test_migrations.py` 断言迁移与模型元数据描述的是同一个
schema（`compare_metadata`），并跑一遍 downgrade → upgrade。没人跑过的
downgrade 会在需要它的那次事故里才被发现。**这个测试验证过是有牙的**：给模型
加一列会让它失败。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14（专用库 agent_workbench_test）
alembic upgrade head: 通过，schema 与模型零漂移
ruff format --check / ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest（有库）: 498 passed
pytest（无库，等同主 CI job）: 484 passed, 14 skipped
```

CI 新增 `postgres` job：`postgres:16`（按 digest 固定）service，先跑迁移再跑
两个数据库相关套件。主 `quality` job 保持完全离线，数据库参数化用例在那里跳过。

两条安全护栏：

- 测试用的 DSN 变量是 `AGENT_WORKBENCH_TEST_DSN`，**故意不在 `AW_` 命名空间内**
  ——settings 会拒绝任何未登记的 `AW_*`，那道守卫比前缀对称更值钱；
- 该 DSN 指向的库名必须以 `_test` 结尾，否则直接拒绝。这些套件会 TRUNCATE，
  导错 DSN 应该得到一个被跳过的套件，而不是一个被清空的数据库。

范围说明：上传数据面（create-upload / 流式或 presigned / complete）、Outbox、
document/version/ACL 仓储与 S3 Adapter 属于 WP03-04～09，不在本 PR。
`artifacts` 表后来随 PR-014 落入实际迁移 `0002_documents_outbox`。三个 DSN
里本 PR 只建了普通查询用的那一个；guard 与 listener 引擎要等协调工作包，
那时它们的连接规则才开始有意义。

## PR-014 Upload/Outbox

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。

WP03-04～06、08、09：上传用例、document/version/ACL 仓储、ingestion outbox
与竞争领取。

已交付：

- `ports/documents.py` / `ports/outbox.py`：`DocumentStore`、`OutboxPort`
  及其 DTO（PR-004 里刻意推迟冻结的两个 Port，现在有实现来校验了）；
- `adapters/persistence/documents.py`、`outbox.py`；
- `application/uploads.py`：`UploadService`（首个应用服务层）；
- 迁移 `0002_documents_outbox`：`artifacts`、`upload_intents`、`documents`、
  `document_versions`、`document_acl`、`outbox_events`。

本 PR 固定下来的行为：

- **version 与它的 outbox 事件同事务提交**。拆成两次提交会同时制造两种排序
  修不好的故障：一个永远不会被索引的文档，和一条指向已回滚内容的索引项。
  测试用 `monkeypatch` 把 outbox 插入换成会触发 CHECK 约束失败的版本——它在
  document 与 version 行已经写入之后才失败——断言两者都没留下；
- **revision 在锁住 document 行的前提下推进**，因此是**单调的**而不只是互不相同；
- **新建文档用条件插入再加锁**：两个上传竞争创建同一个新文档时，都读不到行，
  普通 INSERT 会让后者撞主键。`ON CONFLICT DO NOTHING` 之后重新加锁，拿到的
  要么是自己插的行，要么是抢先者的；
- **完成是双重幂等的**：同一个 upload 再完成一次返回它已经产出的 version；
  内容与当前版本完全相同时不推进 revision——重发的请求不该让索引重做一遍
  产出完全相同行的工作；
- **完成时两边都不信**：读取已存对象自身的 size 与 digest，与传输发生**之前**
  客户端声明的值比对。传错的字节、或声明了自己没有的摘要，都在这里失败，
  而不是变成一个索引会忠实复现的 document version；
- **outbox 没有指向 documents 的外键**：删除事件必须比它描述的那一行活得更久，
  否则索引永远无法被告知遗忘它；
- outbox payload 携带 `authorized_principals`，索引的过滤条件要靠它；
- `SKIP LOCKED` 领取：两个 worker 并发各领 3 条，合计 6 条、零重叠（有测试）。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14
alembic upgrade head（0001 → 0002）: 通过，零漂移
pytest（有库）: 525 passed
pytest（无库，等同主 CI job）: 496 passed, 29 skipped
ruff / pyright (strict, src): 全部通过
uv lock --check --offline / 许可证 allowlist: 通过
```

**并发测试抓到了一个真实缺陷**：最初的实现在两个上传同时创建同一个新文档时会
撞主键失败。这不是测试写错了，是实现漏了竞争窗口——已按上面的条件插入模式修复。

反射式 Port 契约测试（PR-004 建立）也拦了一次：四个新聚合必须补进样例表才能
通过 round-trip / 版本 / golden 三重检查。

范围说明：**没有 HTTP**。FastAPI 上传路由与 2 MiB 控制面 request-limit
中间件（WP03-07）留给独立 PR——它引入的是另一个方向的表面（依赖、应用装配、
中间件、413 语义），和本 PR 的"事实与 outbox 原子性"是两件事。

outbox 的 claim **不是 lease**：worker 死了，它领的事件就一直被领着，这里没有
任何东西回收它。lease 时长、heartbeat、fencing 属于协调工作包；在这里做一半会
得到一个看起来可恢复、实际不可恢复的东西，比一个明显的缺口更糟。S3/presigned
传输与 `document_deleted` / `acl_changed` 两类事件的产生路径同样留待后续。

## PR-015 Upload API

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。WP03 的本地存储与
Upload/Outbox 基线完成；S3/presigned 与完整对象授权仍未完成。

WP03-07：FastAPI 上传路由与控制面 request-limit 中间件。

已交付：

- `apps/api/`：`main.py`（应用工厂 + `agent-api` 入口）、`dependencies.py`、
  `middleware.py`、`identity.py`、`state.py`、`routes/`（uploads / artifacts /
  health）；
- `bootstrap/projections.py`：`ApiRuntimeConfig` 等**窄配置对象**；
- `ArtifactStore.put_stream()`：流式写入，两个实现都补齐。

本 PR 固定下来的行为：

- **两个平面共用一个服务器**。控制请求是描述工作的 JSON，被
  `api.max_control_request_body_bytes` 限制住并在超限时返回 **413**；数据面
  （`PUT /v1/uploads/{id}/content`）豁免——把文档传输限制在控制面大小上，
  等于不支持上传；
- **限制不信任 `Content-Length`**：中间件读到比上限多一个字节为止。缓冲这么多
  正是上限本身允许的量，也是唯一能对"声明了长度却不遵守"的请求做出正确判断的
  办法。有一条测试专门用分块 body 且不声明长度来打这个点；
- **本地存储边读边写**：分块落盘、边写边算哈希与字节数，全程不在内存里持有整个
  对象；在检疫文件名下写完才改名发布，所以失败或超限留下的是一个检疫文件，
  而不是一个别人能读到的半成品；
- **跨租户读与不存在完全同形**：状态码、响应体都一样（有测试直接比对两者）；
- **传输前先读 intent**：未知上传或别的租户的上传在写入第一个字节之前就被拒；
- liveness 不碰数据库（碰了就会在与它无关的故障里报告进程已死，让编排器无故重启
  它）；readiness 碰，但有 2 秒上限。

**配置只有一个入口这条规则被架构守卫强制执行了。** `apps/api` 最初直接依赖
`bootstrap.settings`，`test_raw_configuration_sources_are_confined_to_bootstrap`
立刻拦下，报错信息就是设计说明："inject a narrow configuration object instead"。
于是补上 `bootstrap/projections.py`：API 拿到的是 `ApiRuntimeConfig`，看不到
检索漏斗、协调时序或评测指标，DSN 仍以 `SecretStr` 形式传递、只在构造引擎时
解包——一个配置对象的 repr 因此打不出 DSN。

**身份是接口层的结果，不是请求体字段。** 目前只有一个读 header 的开发用解析器，
`deployment_scope == "remote"` 时会在装配阶段拒绝启动。写这一节时默认 host 还是
`0.0.0.0`，名义上的 local scope 仍可能监听所有网卡，所以这道检查当时不足以阻止
意外暴露；监听地址的强制校验见下方 P0-1 一节（2026-07-26）。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14
pytest（有库）: 541 passed
pytest（无库，等同主 CI job）: 496 passed, 45 skipped
ruff / pyright (strict, src): 全部通过
uv lock --check --offline / 许可证 allowlist: 通过
CLI golden 文件逐字节未变
```

CI 的 `postgres` job 现在同时跑 `tests/api`。

**修掉一个会挂住生产的 bug。** 413 中间件缓冲请求后，用一个"永远返回空 body"的
`receive` 替换了原来的。Starlette 的流式响应会在同一个通道上等
`http.disconnect`——它永远等不到，于是 artifact 下载整个挂死。测试跑不完暴露了
它；现在缓冲消息放完之后会**回落到原始通道**。

另一处也是测试抓的：数据面豁免最初写成了路由前缀 `/v1/uploads`，把**声明端点
一起豁免了**，413 门禁形同虚设。改成一个精确谓词
`is_data_plane_path()`——只有以 `/content` 结尾的那一条路由算数据面。

范围说明：Chat / Task / SSE / Approval 路由属于 WP13；S3 presigned 传输、
`document_deleted` 与 `acl_changed` 事件路径仍未实现。真正的身份提供方要等
D0 决策检查点（WP04 前）。

## ADR-012 身份边界（D0 决策点）

状态：**已决**。见 [ADR-012](./adr/0012-identity-boundary.md)。

实施计划要求在检索工作包之前定下：v1 是单用户本地演示，还是接入一个已认证的
Principal Adapter。之所以必须先定，是因为 WP04 的检索要按 ACL 过滤候选，而
ACL 过滤的意义完全取决于 principal 从哪里来。

**决定**：v1 在领域层是多租户的，在部署层是单机的。

到 WP03 为止的事实是：**隔离规则已经做完了，缺的是认证**。这两件事经常被混为
一谈，而它们的失败模式完全不同——隔离错了会泄漏数据，认证错了会让任何人成为
任何人。租户隔离在仓储、应用服务与 HTTP 三层都有测试，跨租户读与"不存在"在
状态码、响应体和错误文案上完全同形；缺的只是"请求头里写谁就是谁"这一层。

否决了两个选项：把 v1 降为单用户演示（会让 WP04 的 ACL 过滤无法验证，而那正是
这个项目在 RAG 方向最值得展示的部分），以及现在就接 OIDC（JWKS 缓存与轮换、
时钟偏移、令牌撤销各自都是能微妙出错的地方，且现在没有任何消费者压力，还需要
一份基线 13.1 尚未覆盖的令牌层威胁模型）。

原 ADR 依赖“开发身份只能随 loopback 监听运行”这一安全前提。2026-07-25 核验
发现当前实现并未强制它：默认 `api.host = "0.0.0.0"`，local scope 不检查实际
监听地址。因此 remote 拒绝装配只能算部分护栏，不能证明缺口无法被意外暴露。
ADR 已补记这一实现偏差，并在 2026-07-26 修复（见下方 P0-1 一节）。

新增 `tests/architecture/test_identity_boundary.py`，把 ADR 里的规则变成可执行
的：`PrincipalContext` 只能在一份显式清单里的模块中构造（API 的解析器、CLI 的
demo、以及定义它的领域模块）。新增一处就要改清单，也就必须有人解释为什么。
**验证过是有牙的**：在 `runtime/` 下放一个构造点会让它按模块名报错。同一个文件
还断言 remote 拒绝装配的那两行仍然在位。

能力表相应更新：tenant-scoped 数据访问标为 Implemented/Tested，**生产级身份
认证明确保持 Planned**。同 tenant 不同 principal 的对象级授权仍有已知缺口，
不能笼统宣称完整租户隔离已完成。README 与简历同样不得升级这一项；`scopes`
目前由调用方在请求头里自述，因此不是权限来源，只是让真实解析器接入时不必改动
下游的形状占位。

## P0-2 审批 fail closed

状态：**已实现并通过本地测试**。核验报告 §4 的 P0-2、§6 修复顺序第 2 项。

`ToolGateway.authorize()` 现在在两个 allow 分支之前检查
`decision.requires_approval`。为真时发出 `PermissionRequested`，然后以
`approval_required` 拒绝这次调用；handler 不再被触及。

这里的判断是：`effect="allow", requires_approval=True` 不是许可，是**尚未裁决**。
在此之前 `requires_approval` 是一个纯粹的只写字段——`domain/policies.py` 定义它、
`EnvelopePolicyEngine` 按风险等级设置它，而整个 `runtime/` 包没有任何一处读它。
于是「需要审批」这个信息在从 Policy 流向执行的路上被无声丢弃了，而丢弃的方向
恰好是放行。

之所以是拒绝而不是挂起等待：审批设施（`ApprovalStore`、恢复入口）属于 WP10，
现在不存在。「需要人来决定，但这里没有人可以决定」只能落到拒绝上。等 WP10 到位
后这一分支改为挂起并等待裁决，改动仍然限于 Gateway——`PermissionRequested` 事件
与 `approval_required` 错误码从 PR-003 起就在 Domain 里定义好了，此前一直没有
写入方，这次只是终于把它们接上。

模型侧拿到的是 `status="error"` 且正文含 `approval_required` 的 tool result，
因此能区分「不被允许」和「尚未裁决」；run 本身继续正常收尾，不会因为一次待审批
的调用而失败。

回归测试 6 条：Gateway 级 2 条（含 `allow_modified` 改写分支同样被拦），Runtime
级 4 条（完整 run 副作用为零、持久事件序列、回灌给模型的错误正文，以及一条对照
组）。**验证过是有牙的**：临时撤掉那个分支后前 5 条全部失败。第 6 条对照组在
撤掉后仍然通过——正是它保证前 5 条不是靠「write 工具一律拒绝」这种过度实现凑出
来的，拒绝跟的是审批要求本身，不是风险等级。

范围说明：只改这一件事。P0-1（loopback 强制）、P1-6（改写绕过字节上限）
与 P1-7（Policy/Hook deadline）都在同一个文件附近，但各自是独立的行为变化，
留给各自的 PR。

## P0-1 监听地址强制 loopback

状态：**已实现并通过本地测试**。核验报告 §4 的 P0-1、§6 修复顺序第 1 项。

`api.host` 默认从 `0.0.0.0` 改为 `127.0.0.1`；`ApiSettings.host` 只接受 loopback
地址；`build_dependencies()` 在选定 Header Resolver 之前再校验一次。

**规则是无条件的，没有以 `deployment_scope` 为条件。** scope 是部署给自己贴的
标签，而决定谁能触达 Header Resolver 的是绑定地址——把标签当作绑定地址的代理，
正是这条缺陷成立的原因。remote scope 拒绝装配的检查保持不变，两者互不替代。

为什么两层都校验：`ApiRuntimeConfig` 可以不经 Settings 构造（测试就这么做），
而装配层正是选定 Header Resolver 的那一层，拒绝把它和一个可达地址配在一起的
判断，属于那里。

不是 `localhost` 的主机名一律拒绝，而不是去解析它：解析在校验时和 bind 时可以
给出两个答案，DNS 在两者之间还能改变，「不确定」的安全答案是否。

新增 `tests/api/test_bind_address.py`，16 条，覆盖提交默认值、Settings、装配和
真实 socket 四层。**验证过是有牙的**：撤掉 Settings 校验失败 6 条，撤掉装配层
校验失败 1 条，只把默认值改回 `0.0.0.0` 失败 5 条，三者全撤失败 11 条。

其中 socket 那两条刻意绕开 `Settings`，直接读 `config.default.toml` 的原始值来
`bind()`——只见过校验器放行的值的 socket 测试，抓不到校验器本身写错，而这一条
要在校验器都被删掉时仍然成立。三者全撤那次，它是靠一次**成功的跨接口连接**抓到
缺陷的，不是靠读字符串。

配套的对照组同样必需：同一条连接在 `0.0.0.0` 绑定下必须连得上。没有它，
「连接被拒绝」既可能说明护栏有效，也可能说明探针指向了一个本来就没人监听的端口。
这正是这条缺陷能活下来的机制——`test_the_api_refuses_a_remote_deployment_scope`
断言的是 scope 标签，读起来却像在守护整个身份边界。该测试保留（scope 那一半它
确实守住了），docstring 已写明它不覆盖监听地址。

范围说明：这挡住的是**意外暴露**，不是认证。反向代理、SSH 端口转发或容器端口
映射仍可以把 loopback 进程送上网络——那是部署方的选择，代码拦不住，也不该假装
拦得住。生产身份认证仍是 Planned，README 与能力表不因此升级。Settings 叶子字段
数不变（231），没有新增配置项。

## P1-1 上传与文档的对象级授权

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。核验报告 §4 的 P1-1、
§6 修复顺序第 3 项的前半。

`DocumentStore` 的每个方法都显式接收 `principal_id`；`UploadService` 与三条上传
路由把接口层解析出的 principal 一路传下去。此前整条链路只往下传 tenant，于是同
一个 tenant 里的任何人只要知道 upload id，就能替 owner 传输、替 owner 完成，或者
把自己的 upload 指向别人的 document、覆盖内容并替换 ACL。

**读和写是两条不同的规则**，这是这一条最重要的判断：

| 操作 | 谁可以 |
|---|---|
| 观察 / 传输 / 完成一个 upload | 声明它的那个 principal |
| 向已存在的文档提交新版本 | 文档 owner，**仅此** |
| 读文档、版本列表、授权名单 | owner **或** ACL 授权的 principal |

把 ACL 同时当作写授权，会让「授权某人查看」悄悄变成「授权某人覆盖」——那是没有
任何人打算给出的权限。所以 `_is_granted()` 只服务读路径，写路径只比 owner。

**授权检查与写在同一把锁下。** `_locked_document()` 现在返回整行（revision、
owner、knowledge base），检查放在 `FOR UPDATE` 之后：先检查再取锁，判断的是一份
可能已经不是被写对象的行。条件插入那条竞态分支同理——检查放在
`ON CONFLICT DO NOTHING` **之后**，对最终握住的那一行做，否则「输掉创建竞态」
反而成了获得写权限的路径。

KB 不一致用新的 `KnowledgeBaseMismatchError` 拒绝（409）。它不是授权失败——调用方
确实是 owner——而是一个与已提交事实矛盾的断言：接受它会让 document 行停在 KB-A，
而 outbox 事件告诉索引 KB-B。沿用 `UploadVerificationError` 的既有做法复用
`invalid_tool_input` 错误码，**没有新增领域错误词汇**。

拒绝一律是 404，与「不存在」「别的 tenant 的」完全同形。

回归测试 17 条。新增 `tests/api/test_upload_authorization.py`（10 条）**固定
tenant、只换 principal**，并且**故意把 upload id 和 document id 交给攻击者**——
那正是要防的处境，id 会出现在日志、URL 和工单里，「难猜」不是授权规则。另有
7 条落在持久化层，因为读规则目前没有 HTTP 面。

**验证过是有牙的**：撤掉 upload owner 检查失败 3 条，撤掉 document owner 检查
失败 3 条，撤掉 KB 检查失败 2 条，三者全撤失败 8/10（通过的 2 条正是对照组），
撤掉读授权失败 3 条。并发竞态那条单独验证过：把写授权挪到条件插入**之前**，
它连续 6 次全部失败。

写这些测试时改掉了自己两条虚的断言：一条下载 owner 原来的 artifact 来证明「内容
还在」——但接管会写**新的** artifact 并把文档指过去，原 artifact 两种情况下都还在，
所以那条断言恒真；改成直接查库断言版本数、revision 与 digest。另一条只断言 404
正文里没有文件名，而放行时正文本来也没有文件名，同样恒真；改成状态码和正文一起断言。

**已知残留**：`document_id` 由调用方指定，因此邻居仍可通过「用别人的 id 得到
404、用新 id 得到 201」区分出某个 document id 是否存在。消掉它要改成服务端铸造
document id，那是 API 形状变更而不是授权变更，不在本 PR。P1-2（Artifact 对象
授权）与 P1-3（相同内容重传忽略 ACL 撤销）仍然开着，各自一个 PR。

## P1-3 相同内容重传时的 ACL 调和

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。核验报告 §4 的 P1-3。

digest 相同的那条提前返回分支现在先调和 ACL：授权集合有变化时，原子地替换 ACL 行、
推进 revision、写一条 `acl_changed` 事件，然后仍然返回既有 version。

**重传相同内容正是表达「同一份文档、换一批读者」的方式。** 此前这条路径直接返回
旧 version，ACL 行一次都没碰，也不发事件——「内容不变、撤销某人授权」于是完全
不生效，而且是静默的：索引会继续把文档答给一个 owner 已经取消授权的人，直到有人
碰巧上传了不同的字节。`acl_changed` 从 PR-013 起就定义在 `OutboxEventKind` 和
数据库 CHECK 约束里，一直没有写入方——和 P0-2 的 `PermissionRequested` 同一种形状。

**授权变更要占一个 revision。** 消费者按每文档一个单调计数器排序事件；ACL 事件与
内容事件若能共用 revision，乱序到达就无法与重复到达区分。代价是 version 行在
revision 空间里变稀疏（`[1, 3]`，2 是一次授权变更），这是对的：version 记录内容，
那次 revision 没有改变内容。因此顺带订正了两处 docstring——原来把「不推进 revision」
写成了无条件的幂等性质。

比较集合而非序列，所以重排授权列表不算变更；内容与 ACL 都没变时仍然什么都不做。

回归测试 7 条，全部在持久化层。撤销的效果目前**没有 HTTP 面可观测**（没有 GET
document 路由，artifact 下载的对象授权还是 P1-2），所以没有放形式上的 HTTP 测试。

**验证过是有牙的，而且是双向的**：撤掉调和失败 4 条；只撤掉「集合未变则直接返回」
那道早退失败 3 条，其中一条是原有的幂等测试。第二个方向是必需的——没有它，
「每次重传都推进 revision 并发事件」这种过度实现同样能让前 4 条变绿。

## P1-2 Artifact 对象授权

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-2，§6 修复顺序第 3 项的后半。

`ArtifactStore.put`/`put_stream` 记录 `owner_id`，`get`/`head` 接收 `principal_id`。
artifact 归存储它的 principal 所有，其他人一律 not found——与「不存在」「别的
tenant 的」三者同形。此前同 tenant 的任何人只要知道 artifact id 就能下载，而 id
会出现在 tool result、事件 payload 和 URL 里；UUID 难猜不是授权。

**owner 放在存储自己的元数据里，不放进 `ArtifactRef`。** 这是这条唯一需要设计
判断的地方：`ArtifactRef` 随消息和事件流转，而「谁能读这些字节」是被存对象的属性，
不是指针的属性——指针若携带它，等于把答案连同问题一起发出去。附带好处是领域模型
没变，golden 基线和 `DOMAIN_SCHEMA_VERSION` 都不受影响。

**没有按审计原文走 PostgreSQL。** 审计写的是「在 PostgreSQL 持久化 owner/对象
关系」。`artifacts` 表虽在 schema 里却至今没有写入方，`LocalArtifactStore` 一直用
blob 旁的 sidecar；把授权搬进数据库会让文件存储适配器依赖数据库引擎，而那张表按
其模块 docstring 的说法还没「挣得自己的位置」。记进存储自身的元数据，对未来的 S3
适配器是同一形状（对象元数据），也不引入这层依赖。这是有意偏离修复方向，不是遗漏。

sidecar 改为带 `format` 标记的信封，不认识的信封视为不存在。

回归测试 20 条：共享契约 6 条 × 2 个 store（in-memory 与 filesystem 都跑，规则对
每个后端都被钉住）、sidecar 专属 5 条、HTTP 面 3 条。

**验证过是有牙的**：同时撤掉两个 store 的 owner 检查失败 10 条（跨 store、跨层）；
撤掉 `format` 兜底失败 1 条；撤掉 `isinstance` 兜底失败 1 条——每道防线各有一条
测试专属于它。写的过程中又抓到自己一条恒真断言：原本用集合收集三种拒绝的文案并
断言集合只有一个元素，但被放行的那次什么都不加，集合照样只有一个元素；改成按顺序
记录三次结果，放行记 `"allowed"`。

**已知残留，是明确的功能缺口而不是疏漏**：artifact 归存储它的 principal 所有，
**文档 ACL 触及不到它**——没有东西能把 artifact 反查回引用它的 document version。
于是被授权读文档的 principal 看得到文档存在，却下载不了字节。fail-closed 且不完整，
顺序如此。已用 `test_a_read_grant_does_not_yet_reach_the_bytes` 把这个限制钉住，
反查落地时必须**有意地**改掉它；补齐需要 `document_versions.artifact_id` 上的索引
和一个新的 DocumentStore 方法，属于独立变更。

P1-4（下载整体读入内存，不是真正的分块流式）仍然开着，就在同一条路径上。

## P1-5 Runtime 预算硬上限

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-5，§6 修复顺序第 4 项。
**审计的四个阻断面到此全部关闭。**

三处独立的行为变化，共同的形状是：**上限必须在花费之前生效**。事后记账的不是上限，
副作用已经发生了——和 P0-2 是同一句话，只是发生在调度层而不是授权层。

**一、tool-call 配额在 dispatch 之前预留。** 此前一轮提出 N 个调用就全部执行、之后
才记账；余量只剩 1 而提出 3 个时，3 个 handler 全跑，账上还写 3，比上限本身还大。
现在按余量计算准入数，超出的立即以 `budget_exceeded` 拒绝——仍然发 `ToolProposed`
（模型确实提议了），仍然回答它的 id（每个 id 都欠一个结果）。**只对准入的调用记账**：
因上限被拒的调用不该自己消耗上限。

**二、每回合合并 usage 之后复检预算。** 循环顶部那次检查在回合之前，看不到这一回合
花了什么。于是 `max_total_tokens=1`、模型上报 120 tokens 时 run 报 `completed`；若这
一回合还提议了工具，工具照跑。复检放在错误/取消分支之后、`if turn.calls` 之前，两种
后果同时消掉。

顺带的行为变化：**步数用尽的那一回合，其工具不再执行**。最后一步给了模型，工具结果
已经没有读者，执行它们等于用真实副作用换取会被丢弃的输出。已用测试钉住。

**三、无法度量的成本上限被拒绝。** `cost_micro_usd` 没有任何生产者，成本预算永远停在
0、永远不触发——和 P0-2 的 `requires_approval` 同一种形状，一个只写字段，沉默的方向
恰好是放行。这里**不实现计价器**（按 model revision 的价格表是独立变更），而是拒绝
接受一个执行不了的上限：调用方要的是保证，静默地不提供比说出来更糟。引入计价器的那个
PR 负责删掉这个分支。

**明确的残留**：token 上限仍然只能在模型调用**之后**核对——一次调用花多少 token，
调用前不可知。所以单次调用可以冲过上限、随后 run 立刻失败。上限约束的是 run 走多远，
不是单次 provider 调用返回多少。计价器仍未实现。

回归测试 10 条（4 + 3 + 1 + 2，含 3 条对照）。**验证过是有牙的**：撤掉配额预留失败
3 条、撤掉回合后复检失败 3 条、撤掉成本拒绝失败 1 条、把记账改回按提议数失败 1 条。

值得记一笔：改完之后既有 610 条测试**一条都没红**。审计说的「预算测试没有覆盖越界
批次」正是这个意思——现有两条上限测试的预算都恰好卡在整数倍上（`max_tool_calls=4`、
每轮 2 个调用），从没构造出越界的那一批，所以这一整类缺陷全程绿灯。

## P1-6 / P1-7 Gateway 的尺寸上限与时间界

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-6 与 P1-7，§6 修复顺序第 5 项的
一部分。两条各自一个提交。

**P1-6：Policy 改写受参数字节上限约束。** `authorize()` 的改写分支从 `_validate()`
改为 `_check()`——与原始参数、与 Hook 改写同一道检查。此前 Hook 改写走完整检查而
Policy 改写只重跑 schema，于是 `max_argument_bytes` 约束的是模型能发多少、不是
Policy 能替换成多少：上限 64 字节时一次改写把 10,000 字节送进 handler，run 正常
完成。schema 说 `query` 是字符串，没说一万个字符不行。3 条测试，撤掉失败 2 条。

**P1-7：Policy 与 Hook 受 run deadline 约束并 fail closed。** 三处：

1. Policy 引擎此前完全无界——部署方提供的代码坐在每次调用的必经路径上，卡住就把
   run 拖过它自己的 deadline。现在界是 `min(gateway 自带 policy 超时, run 剩余)`。
   gateway 保留自带默认值（5 秒），这样调用方忘记传剩余时间时**仍然有界**；默认成
   None 才是同一类 fail-open。
2. Policy 抛出的异常归一化为拒绝。**复现时发现比审计写的更严重**：异常直接逃出
   `authorize()`，调用方拿到 traceback 而不是终态结果，违反 `AgentExecutor` 的协议
   约定；而且正文原样带出——复现消息里就有 `dsn=postgres://u:sk-ant-canary@h/db`。
   现在只有异常**类型名**过界，与 Hook Bus 早已遵守的规则一致。
3. Hook 取 `min(自身超时, run 剩余)`，逐个重算。此前每个 hook 都持有完整超时，只剩
   2 秒的 run 仍可能在一个 hook 里花 5 秒——本该结束 run 的 deadline，恰恰是 run
   唯一管不住的东西。剩余为 0 时不启动任何 hook 或 policy 调用，并拒绝该次调用。

拒绝理由里带上**是哪一个界耗尽的**，因为两种后果相同但指向不同的排查方向——和
`ModelCallDeadline.source` 同一个判断。

9 条测试（gateway 6 + hook bus 3，含 2 条对照）。**有牙验证有一点特别**：撤掉
policy 归一化后，能终止的 3 条失败，另外 2 条**永远挂住**（后台跑 8 秒未结束才强
杀）——无界等待正是缺陷本身，它表现为挂起而不是断言失败。hook 那侧同理：撤掉后
1 条失败、1 条挂 30 秒。

## P1-8 重复 tool_call_id 在派发前被拒绝

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-8。

两处行为变化：

1. **模型 turn 完成后、准备/授权/执行之前检查 ID 唯一性。** `tool_call_id` 就是
   「结果回答的是哪一次调用」本身；两次调用共用一个，就无法说哪个结果属于哪一次。
   此前只有 `align_results()` 会发现，而它跑在 handler **之后**——模型重复一个 ID，
   工具就按重复次数各执行一次，run 再死在记账上。复现确认**两次 handler 调用**。
   现在整轮失败、handler 零调用。唯一性是**每轮**的：ID 只需在同侪之间可区分，
   跨轮复用照常执行（有测试钉住）。
2. **`align_results()` 的失败归一化为终态 outcome。** 复现确认了审计的补充：
   `ToolPairingError` **逃出了 `run()`**，调用方拿到 traceback 而不是终态
   `AgentOutcome`——违反 `AgentExecutor` 协议约定，Graph node 因此拿不到可记录、
   可路由的结果。与 P1-7 里 policy 异常逃逸是同一种形状。

5 条测试，含 2 条对照。**关于那条 backstop 需要说明**：预检让重复 ID 永远到不了
`align_results()`，所以第二处修改在正常路径上**不可达**。不可达而又没人跑过的分支
是主张不是保证，所以测试里放了一个故意坏掉的 gateway（返回带错误 `tool_call_id`
的结果）实际走一遍——生产代码里没有任何东西会这么做。

**验证过是有牙的**：撤掉预检失败 2 条，撤掉 backstop 失败 1 条，两处各有专属测试。

## P1-9 DeepSeek SSE frame fail closed

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-9。

三处行为变化：

1. **无法解码的 `data:` frame 结束整条流**（此前静默跳过）。跳过不是中性的：工具
   参数按片段拼接，从中间丢掉一片，剩下的仍可能拼成一份**完全合法的 JSON**——
   一个模型从未发出的调用，带着没人选过的参数。已复现：插入一个损坏 frame 后，
   handler 拿到 `{"document_id": "doc_SAFE"}`，而模型本意更长。注释行、空行、
   `[DONE]` 仍然跳过，它们本来就不携带数据。
2. **领域校验错误归一化为终态 error。** `BoundedText` 限制单个 delta 为 4096 字符，
   超过时构造 `ModelTextDelta` 抛的 `ValidationError` **直接逃出了 `ModelPort`**。
   provider 自己的限制不是本进程的契约，Port 的调用方不该拿到 Pydantic traceback。
3. **累积工具参数有上限**（默认 256 KiB 字符，构造参数可调）。那是这里唯一随
   provider 发送量增长的东西。不新增配置项，与 gateway 的 `policy_timeout_seconds`
   同一种做法。

**有一条测试把缺陷本身写成了预期行为**：
`test_unreadable_frames_are_skipped_rather_than_fatal`，断言的正是要修掉的东西，
一直是绿的。已替换，旧名字与这段历史记在新测试的 docstring 里。这比 P0-1 那种
「守错对象」更直接——不是守护了错误的对象，而是守护了错误本身。

8 条测试（含 2 条对照：注释与空行仍然跳过、上限之内的参数照常拼装）。**验证过是
有牙的**：撤掉 frame fail-closed 失败 3 条，撤掉 `ValidationError` 归一化失败
1 条，撤掉参数上限失败 1 条。

## P2-1 DeepSeek 可靠性配置接入 Adapter

状态：**已实现并通过本地测试**。核验报告 §4 的 P2-1。

`DeepSeekProfile` 增加 `timeout_seconds`、`max_retries`、`tool_calling_required`，
adapter 实际消费它们。此前三者在 Settings 里有定义、有校验，却没有任何消费者——
部署方可以配一个超时或重试次数，两样都不会发生。

**重试语义是唯一需要判断的地方。** 流式调用一旦吐出过事件就不能重试：调用方已经看到
的字节收不回来。所以只有**第一个事件之前**的失败可以重试——传输故障与可重试状态
（5xx、429）。`_attempt()` 用 `_RetryableFailure` 表达，`stream()` 是外面的重试循环。
400 之类不重试：请求本来就错，再发还是错。退避按次翻倍，立刻重试 429 等于要求被更狠
地限流；`sleep` 可注入，测试不必真等。

`tool_calling_required` 只在有 tools 时才发 `tool_choice: "required"`——没有工具却
要求必须选一个，是没人能满足的请求。

Settings → `DeepSeekProfile` 的投影仍不存在，因为 DeepSeek 还没装配进进程
（WP02-06/07）。本条修的是 adapter 侧的配置语义对齐。

9 条测试（含 2 条对照）。**验证过是有牙的**：撤掉 timeout / tool_choice / 可重试状态
信号 / 「已发事件则不重试」守卫，分别失败 1 / 1 / 2 / 1 条。

最后那条一开始**没咬住**：我写的测试用了一个格式损坏的响应体，走的是 P1-9 的坏 frame
路径，到不了那个守卫。换成真正在中途抛 `ReadError` 的响应流之后才成立。
## P2-2 关闭任意可关闭的流

状态：**已实现并通过本地测试**。核验报告 §4 的 P2-2。

`_stream_model` 的 `finally` 从 `isinstance(stream, AsyncGenerator)` 改为一个
`_Closable` Protocol（只要求有 `aclose`）。

`ModelPort` 承诺的是 `AsyncIterator` 而不是 `AsyncGenerator`，所以一个返回其它
可关闭迭代器的 adapter——比如包着一个必须释放的连接的那种——**从来没有被关过**。
这类泄漏表现为压力下连接耗尽，离真正出问题的那行很远。`aclose` 才是协议，
`AsyncGenerator` 只是最常见的满足者。

2 条测试（含 1 条对照：没有 `aclose` 的流不能让 run 出错——关闭是「能则关」，
不是「必须有」）。**验证过是有牙的**：改回只关 `AsyncGenerator` 失败 1 条。
## P1-4 Artifact 分块流式下载

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-4。

`ArtifactStore` 增加 `iter_chunks()`，两个 store 各自实现，下载路由改用它。此前
`Path.read_bytes()` 把最多 100 MiB 读完再包进 `StreamingResponse`——名义流式，
峰值内存是每个并发下载一整个对象。

`iter_chunks()` **不是 `async def`**：授权必须在调用时发生，而不是拉第一块时。
只在首次迭代才拒绝的协程会让路由先承诺 200、再发现无可发送，那对客户端与网络中断
无法区分。

7 条测试（共享契约 5 条跑两个 store，HTTP 面 2 条）。

**HTTP 那条写了三遍才对**，前两遍都是恒真断言，都在有牙验证时被自己抓出来：
内容小于默认分块（一片本就正确）→ 换成大于两个分块；httpx `ASGITransport` 把响应
缓冲成一片（两种实现都报 1）→ 改成直接驱动 ASGI 数 `http.response.body`；数所有
body 消息（Starlette 总补一条**空**终止消息，单次整体 yield 也是 2 片）→ 只数非空片。

**验证过是有牙的**：路由改回「整体读入 + 单片 yield」失败 1 条，撤掉 `iter_chunks()`
授权检查失败 1 条。
## P1-10 Outbox lease 与 fencing

状态：**已实现并通过本地测试（含真实 PostgreSQL 与 Alembic 往返）**。核验报告 §4 的
P1-10，也是审计要求「在真正启用 ingestion worker 之前」完成的一条。

claim 成为**租约**并带 fencing token。Alembic `0003` 给 `outbox_events` 增加
`lease_until` 与 `claim_token` 及按到期时间的部分索引。

**为什么单有到期不够。** 一个只是卡住的 worker（长 GC、网络分区）在租约到期时仍然
活着。它回来后会 ack 一个**另一个 worker 此刻正在处理**的单元，把别人的在途工作标记
为完成，而别人真正做完的结果反而像重复劳动。所以每次 claim 铸一个 token，ack 必须带
当前的那个。

`StaleExecutionError` 从 PR-003 起定义、docstring 明写用途，一直**没有生产者**——
第三个这种形状（前两个是 P0-2 的 `PermissionRequested` 与 P1-3 的 `acl_changed`）。

到期一律读**数据库时钟**：两个 worker 对时间的分歧正是同一租约被握两次的方式。
ack 靠 **rowcount** 判断，因为匹配不到任何行的 UPDATE 是成功的。

迁移会释放迁移前已 claim 未 ack 的行——它们没有租约也没有 token，既不可回收也不可
ack，正卡在这次修复要消除的状态里。释放是安全的：未 ack 的事件本就是欠着的工作，
最坏是被重复应用一次，而 ingestion 侧无论如何必须幂等。

**仍缺 heartbeat**：诚实做慢活的 worker 无法延长租约、会丢掉它。那属于 ingestion
worker 本身；在它存在前，租约应设得比最慢的工作单元更长。已写进模块 docstring。

6 条测试（含 2 条对照）。**验证过是有牙的**：撤掉过期回收失败 3 条，撤掉 fence
失败 2 条。

## PR-016 Dense Retrieval Kernel（WP04 起步）

状态：**已实现并通过本地测试（含真实 Qdrant）**。WP04-04 与 WP04-05 的索引侧；
只开内部 Port，**不注册任何外部 Chat/RAG 路由**——按计划 §9，检索接口要等
PR-017 的 ACL 二次重验落地后才对外可见。

新增 `ports/vector_index.py` 与 `adapters/vector/qdrant.py`：collection schema、
幂等 upsert、带 tenant / knowledge-base / ACL 过滤的 dense 搜索、按文档删除。

三个判断写在代码里：

1. **point id 是 chunk id 的 UUIDv5。** Qdrant 只收无符号整数和 UUID，而
   at-least-once 投递配上生成式 id，等于每次重试都堆一份近似重复，检索会把它们
   一起返回。稳定 id 才让重投是幂等的。
2. **过滤在查询语句里，不在返回之后。** 先取 limit 再在 Python 里筛，会返回比
   请求更少甚至为空的结果——取决于邻域碰巧怎么排——而且把租户边界挪进了调用方。
   边界和计数属于同一条语句。有一条测试专门钉这个：两个不可见 chunk 比可见的更
   靠近查询向量，limit=1 时必须返回那个可见的。
3. **payload 过滤是缩小候选，不是授权。** 模块 docstring 明写：真正的授权在
   PostgreSQL，Qdrant 返回后还要按 document/version 重验（PR-017）。把派生副本
   当权限权威，正是陈旧索引变成数据泄漏的方式。

**没有承诺 revision 顺序保证。** Port 里我一开始写了「旧 revision 不覆盖新的」，
随即改掉了：Qdrant 没有条件写，任何检查都是 read-then-write，会输掉它声称能赢的
竞态。顺序属于 ingestion worker 的单写者锁（WP05-07），这里只记录 revision 供那
套协议比较。

**测试基础设施**：CI 的 postgres job 改名为 `stateful`，加了 Qdrant 服务容器
（按 digest 固定，与其它镜像一致）。Qdrant 镜像不带 shell，容器级 health check
用不了，改在 runner 上轮询 `/readyz`。无服务时跳过 93 项并显式报出，不是静默跳过。

14 条测试，全部对真实 Qdrant 跑。**验证过是有牙的**：撤掉 tenant 过滤失败 1 条，
撤掉 knowledge-base 过滤失败 1 条，撤掉 ACL 过滤失败 2 条，撤掉维度校验失败 1 条。

依赖新增 `qdrant-client`；许可证 allowlist 相应扩了三个既有宽松条款的不同拼写
（numpy 的复合串、protobuf 的 `3-Clause BSD License`），做法与 PR-012 引入 httpx
时一致。

## PR-016b 摄取管线：解析、切块、嵌入

状态：**已实现并通过本地测试（含真实 Qdrant）**。WP04-01/02 与 WP04-03 的确定性
部分；真 BGE-M3 与 PDF 各自独立 PR。

一个文档版本进去，一批可检索的 chunk 出来：解析 → 切块 → 嵌入 → 写入。

**先全部嵌入，再写任何东西。** 模型调用中途失败时，索引里不能留下半个版本——
半索引比未索引更糟：检索会找到存在的那一半，然后当作全部来回答。

**chunk id 是推导的，不是生成的。** 同一版本、同样切法，每次必须落在同样的 id 上，
否则重建索引会在旧 chunk 旁边再写一份。推导里包含决定「一个 chunk 是什么」的全部
东西：版本、序号，以及索引身份（嵌入器 + 切块器）。换了嵌入器，id 就变——因为向量
不再可比，旧的点也不再是答案。

**token 计数器的名字参与索引身份。** 这是这个 PR 里唯一真正的取舍。窗口按 token
度量，而由谁来数决定每一条边界落在哪。同一个名字下存在两种切法，就意味着重建索引
会悄悄移动所有边界，而按旧偏移建立的引用会指向已经不在那里的文本。所以
`ApproximateTokenCounter` 的名字明确写着 approx，将来换成模型自己的 tokenizer 是
一次**可见的重建**而不是缓慢漂移。

**偏移是记录的，不是回头搜索的。** chunk 的字符区间在切的时候就记下来；靠搜索
chunk 文本来定位，遇到重复段落会把每一份都定位到第一处。有测试用重复六遍的句子
钉住这一点。

**解析器拒绝它读不了的东西，而不是凑合读。** 非 UTF-8 用替换字符解码出来的文档，
一样能切块、嵌入、检索得有模有样——那会得到一个建立在没人写过的文本上的回答，而
每一层都报告成功。PDF 没有放进来：它是一个依赖，也是一整类自己的失败模式
（加密、只有扫描图层、生产者对页边界的分歧），塞进 `bytes.decode` 旁边等于把这些
全藏起来。

**架构守卫抓到一次真实的分层违规。** 最初 `application/ingestion.py` 直接 import
了 `adapters/ingestion`，守卫报错。它是对的：解析迟早要带库，所以应该是 Port；
切块是纯逻辑，属于 application；token 计数器是 Port + adapter。重构后
`application` 只依赖 `ports`。

`qdrant-client` 从 `>=1.12,<2` 收窄到 `>=1.12,<1.13`：CI 里按 digest 钉死的服务端
镜像是 1.12.4，而锁文件解析到了 1.18.0，客户端每次连接都会警告主次版本不匹配。
服务端的 digest 钉死是有意的供应链决定，所以对齐客户端而不是反过来——今天只是警告
的不匹配，明天可以是真的协议不兼容。

测试 41 条：切块器 11、解析器 6、确定性嵌入器 9、端到端接真实 Qdrant 9（另有既有
Qdrant 索引测试）。无 Qdrant/无库时 619 passed、102 skipped，跳过数显式报出。

## PR-016c BGE-M3 dense EmbeddingPort

状态：**已实现；adapter 逻辑在所有环境下有测试，真模型契约需要本地权重**。
WP04-03。

真 BGE-M3 dense adapter，放在**可选依赖组** `embedding` 里。

**为什么是可选的。** 运行时加权重合计几个 GB，而除了嵌入契约本身，其余每一道门禁
都不需要它们。所以 CI 不装：跑得快、离线，且计划 §12「离线测试不调用真实 BGE 依赖」
那条门禁继续成立。代价写在下面。

**导入是惰性的。** 模块顶层 `import torch` 会让没装 extra 的人整个包都不能用，所以
导入发生在加载函数内部。缺依赖时报的是一句能照做的话（`uv sync --extra embedding`），
而不是 `ModuleNotFoundError`。

**只做 dense。** BGE-M3 也能出 sparse 和多向量，但暴露那些的库（FlagEmbedding）
接口更宽也更不稳定。sparse 是 WP05-01；为一个还没有消费者的能力现在就引入更重的
依赖，正是一个项目积累起自己解释不了的依赖的方式。

**类型不依赖那个可选库。** pyright 在 CI 里看不到 `sentence_transformers`，所以
adapter 针对一个本地 `SentenceEncoder` Protocol 编写——只声明实际用到的两个方法。
顺带把接口写清楚了：那两个方法一旦变化，就是这个 adapter 必须察觉的变化。

**维度不匹配在加载时就拒绝。** 模型输出宽度与 `rag.embedding.vector_size` 不一致
时，它写不进按那个宽度建出来的 collection。Qdrant 最终也会拒绝 upsert，但在启动时
失败才能指出原因，而不是等到第一个文档。

**`encode()` 放在工作线程里。** 它是同步且计算密集的；直接 await 会让整批嵌入期间
事件循环停摆，在共享进程里意味着其他所有请求排在一个文档后面。真正该用的有界执行器
属于协调工作包，`asyncio.to_thread` 是诚实的过渡，且严格优于阻塞循环。

**测试分两档：**

- **12 条在任何环境都跑**，用一个满足 Protocol 的 stand-in encoder：批大小是否传到
  模型、是否要求归一化、顺序是否保持、空批不调用模型、身份是否含 revision、维度
  不匹配/不报告维度/批大小非正各自被拒（含 1 条对照：宽度正确时正常加载）、缺依赖
  时的报错文案。
- **4 条需要真权重**，由 `AGENT_WORKBENCH_TEST_EMBEDDING_MODEL` 控制：实际输出维度
  等于配置（计划明确列出的门禁）、单位向量、确定性、以及**相关文本比无关文本更近**
  ——最后这条是 stand-in 永远不可能有的性质，也是真跑一次的理由。

跳过时**显式报出原因和条数**，不是静默跳过。

写这些测试时又抓到自己一个问题：最初的 `_load_with` 辅助函数**复制**了 `load()` 的
检查逻辑，等于在测我自己的副本而不是真代码——正是这份文档里反复出现的「守错对象」。
改成给 `load()` 加一个可注入的 `loader` 参数，测试驱动的是真正的检查。stand-in 也
有过一个缺陷：它用 `None` 当「沿用 dimension」的哨兵，导致「模型真的返回 None」这
一路径无法表达，被一条测试的失败暴露出来。

**真模型运行证据（2026-07-26，本地）**

```text
BAAI/bge-m3 @ main, sentence-transformers 5.6.1 / torch 2.13.0, CPU
首次运行（含权重下载）：16 passed in 981.62s
修复后重跑（权重已缓存）：19 passed in 81.61s，无告警
```

四条真模型断言全部通过：输出维度 = 配置的 1024、单位向量、确定性、相关文本比无关
文本更接近查询。

**真跑抓到一个 stand-in 结构上不可能发现的问题。** 首次运行报
`FutureWarning: get_sentence_embedding_dimension 已改名为 get_embedding_dimension`。
依赖范围 `>=3.3,<6` 跨越了两种命名：旧版本只有旧名，新版本两个都有但旧名告警。
假 encoder 只会响应写它时用的那个名字，所以对它测多少次都不会暴露真实库改过名——
这类问题只有真加载一次模型才会出现。已修：新增 `reported_dimension()` 按新名→旧名
询问；`SentenceEncoder` Protocol 里**故意不声明维度访问器**，因为在 Protocol 里点名
其中一个会让另一个版本无法满足它。补了 3 条测试覆盖两种命名与都没有的情况。

**能力表怎么标。** dense embedding 标为 Implemented + Tested，但**证据来自本地而不是
CI**：CI 不装可选依赖，所以那 4 条在 CI 里始终跳过。README 与简历写「BGE-M3 dense
retrieval 已验证」时必须同时说明证据是本地运行的，不能指向一条 CI 链接。

## PR-017 Authorized RAG Slice

状态：**已实现并通过本地测试（真实 Qdrant + 真实 PostgreSQL）**。WP04-06、WP04-10。

**索引负责缩小候选，PostgreSQL 负责授权。** 这是两件不同的事，这个 PR 就是把这个
区别落成代码。点的 payload 记录的是「上次索引这个文档时的 ACL」，而「上次索引」
不等于「现在」——一秒钟前撤销的授权还没到达索引，索引会照样把 chunk 返回给那个
刚被拿走权限的人。

**每个候选在成为 context 之前都要对 PostgreSQL 重验。** 不是排序之后，不是组装
citation 的时候，而是之前——因为下游每一步都是文本外泄的途径：进入 rerank 意味着
被模型读过，进入 citation 意味着被用户看到。

**授权检查两次，第二次不是冗余。** 构造 context 和提交答案之间隔着一次模型调用，
授权可以在这中间被收回。每个文档被授权时的 revision 一路带下去，来源变动的答案
被拒绝而不是交付。

**比较 revision 而不是重问「我还能读吗」**，是因为前者能抓到**被替换过的 ACL**：
撤销再重新授予，重问会说「能读」，而 revision 说 ACL 变过——这正是答案所依据的
东西。P1-3 让授权变更和内容变更一样推进 revision，这条才成立。

### barrier 测试

WP04 的退出条件明确要求：在 Qdrant query 完成后、context 构造前提交 ACL revoke，
被撤权的 chunk 不得进入 rerank、模型上下文、回答或 Citation。

**实现方式：不在生产代码里开测试专用钩子。** 测试包装 `VectorIndexPort`，在
`search()` 里调用真实实现后、返回前执行撤权。这精确等于要防的那个时间窗，确定性
（不依赖 sleep，符合 `deterministic_concurrency_required`），且生产路径完全不知道
自己正在被交错。

9 条测试，含 3 条对照（owner 不受影响、未变更时确认通过、陌生人本来就拿不到）。

**验证过是有牙的**：
- 把重验换成「信任索引的 ACL 过滤」——这是真实会犯的错误写法——失败 2 条，且是
  **断言失败**（被撤权的 chunk 真的进了 context），不是崩溃；
- 撤掉答案提交前的第二次检查——失败 2 条。

第一次尝试有牙验证时我用的破坏方式会让代码 `KeyError` 崩溃，那不是忠实的模拟：
真实的错误是「有人删掉 PostgreSQL 重验、改为信任 payload 过滤」，而那种写法不会
崩。改成忠实的版本后，测试是靠断言抓到泄漏的。

### 尚未包含

Chat 切片（WP04-07/08/09：ChatService、`knowledge_search` Tool、REST/CLI）不在本
PR。按计划纪律，**外部 Chat/RAG 路由要等授权链路完整后才注册**——本 PR 补齐的正是
这个前提，但路由本身属于下一个 PR。

Citation 目前用 chunk ordinal 作为 paragraph 定位。字符级 offset 在切块时已经算出
并保存在 `TextChunk.locator` 里，但还没有穿过索引 payload 传到检索侧；页码需要 PDF
解析（同样未实现）。这是**已知的定位精度缺口**，不是遗漏。

## PR-018 固定 2-step ChatService

状态：**服务层已实现并通过本地测试（真实 Qdrant + 真实 PostgreSQL）；HTTP 路由
未注册**。WP04-07 的服务部分。

一轮：检索一次 → 把 `ContextPacket` 作为**引用材料**交给模型 → 交付前再次核验来源。

**「模型不决定是否检索」写成了权限，不是意图。** 工具清单为空，envelope 是
deny 形状的默认值（空 allowlist 什么都不允许）。这样固定 2-step 和 agentic 两条
路径可以分开评测——同一个问题每次以同样方式检索，答案的变化就只能来自模型或语料。

**检索到的段落不是指令。** context 以引用材料的形式进 prompt，system prompt 明说
这一点：一份写着「忽略你的指令」的文档，是一份**写着**那句话的文档，不是一份**执行**
那句话的文档。

**答案被扣下时，进历史的是拒绝说明而不是答案。** 存下答案会把被扣的文本留在下一轮
读得到的地方。

8 条测试，含 withheld 路径（撤权发生在模型已写完答案之后）与「邻居不能借 session id
向别人的会话提问」。

### 两个前置缺陷，都是做这件事时撞出来的

**会话 IDOR。** `ConversationStore.append`/`history` 只按 tenant 限定，同 tenant 里
任何知道 session id 的人都能读走整段对话、还能注入消息——owner 下一轮会把注入内容
当成自己的历史读回来。这是**会话版的 P1-1**。先修它再谈路由，是 PR-017 同一条排序
纪律：授权链不完整时不发布接口。6 条回归测试跨两个 store。

**我自己在 #22 引入的预算缺陷。** 我把 `stop_reason_for`（「我还能再开一步吗」，
docstring 明写「在 turn 之前评估，绝不在之后」）复用成了 turn 之后的超限检查，导致
`max_steps=N` 在完成路径上表现得像 `N-1`：模型在允许的一步内答完，答案被丢弃、run
报失败。**是写 chat 时撞上的**，因为 chat 的一轮恰好就是一步。

现在两个问题分开：`overrun_reason_for`（严格大于）无条件在 turn 后跑——审计的 token
场景需要它，因为一次调用要花多少 token 在调用前不可知；`stop_reason_for` 保持原义，
只在模型**提了工具**时再问，那才是「即将开始更多工作」。所以答完的 run 完成，想继续
但配额用尽的 run 仍在派发前停（#22 加的行为保留）。各有一条专属回归测试。

### 未包含：HTTP 路由与 CLI（WP04-08/09）

**没有注册路由，也没有假装注册。** 接上路由需要在进程里装配一个模型，而 DeepSeek
Adapter 至今没有进程级装配：`ApiRuntimeConfig` 里没有 model/rag/qdrant 投影，也没有
从 Settings 构造 adapter 的代码。那是 WP02-07，独立的一块——要扩投影、装配 Qdrant
客户端与 embedder、处理 API key，并且会让 API 进程从此需要密钥才能启动。

在低余量下草率塞进这样一块装配变更，正是这份文档里反复记录的那类错误的来源。
`knowledge_search` Tool（WP04-08）同理，它属于 agentic 路径，需要同一套装配。

## PR-019 模型装配与配置投影

状态：**已实现并通过本地测试**。WP02-07 的前半：把 DeepSeek Adapter 变成进程能真正
构造出来的东西。

`ApiRuntimeConfig` 新增 `model` 投影（provider、base_url、api_key、两个 profile 及
其可靠性字段），并新增 `bootstrap/model_factory.py` 从投影构造 `DeepSeekModel`。

**这是配置从「承诺」变成「能力」的地方。** 在此之前每一层校验的都是形状——model_id
是非空字符串、base_url 是个 URL——而这些都不说明进程真能触达 provider。

**一个装配不出可用模型的进程，比一个拒绝启动的进程更糟。** 它能通过健康检查、能接
请求、然后每一个请求都在 provider 那边失败——配置错误于是变成一次事故，而症状离
成因隔了好几层。所以缺 key、空白 key、占位 model_id、没有 adapter 的 provider，
四种情况都在启动时拒绝。

**占位 model_id 在所有环境都拒绝，不只 production。** Settings 层的
`_looks_like_placeholder` 只在 production 生效，而committed defaults 里就是
`not-configured-deepseek-main`——一个开发进程「什么都答不上来」，仍然是什么都答不
上来。这是有意与 Settings 层不同的严格度，不是重复校验。

拒绝信息里点名**是哪几个 profile 没有 pin**：一句「有地方配错了」要靠二分才能行动。
API key 不出现在任何拒绝信息里——启动错误会进日志和工单。

7 条测试：四种拒绝各一条、profile 命名一条、key 不泄漏一条，外加一条对照（配置正确
时确实构造出 `DeepSeekModel` 且满足 `ModelPort`）。

### 未包含：路由注册

装配 Chat 路由还差两块：Qdrant 客户端与 embedder。embedder 是个真问题——唯一的真实
实现在**可选依赖**后面，所以给 API 进程装 chat 意味着要么让 API 依赖那个 extra，要么
在 extra 缺席时拒绝装配 chat。后者与本文件里其他每一处的做法一致（拒绝而不是假装），
但那是下一个变更，不是这一个。

## PR-020 Embedder 装配：缺依赖时缺功能，而不是撒谎

状态：**投影与工厂已实现并通过本地测试；HTTP 路由仍未注册**。

`ApiRuntimeConfig` 补上 `qdrant`、`embedding`、`retrieval` 三个投影；新增
`bootstrap/embedding_factory.py`。

**核心决定：缺可选依赖时返回「没有」，而不是替一个假 embedder。**

真实 embedder 需要几个 GB 的运行时与权重。要求整个 API 都装它，会让上传、artifact、
健康检查依赖一套它们从不触碰的机器学习栈；所以没装的进程照常启动，只是不提供 chat。

而另一条路——塞一个 stand-in embedder 好让路由存在——会造出一个用**毫无意义的向量**
回答问题的 chat 接口：检索返回哈希碰巧放在附近的东西，语气笃定，还附引用。
**缺失的功能是可读的，撒谎的功能不是。**

所以工厂返回 `EmbeddingUnavailable` 而不是抛异常：缺席是预期状态，调用方据此不注册
chat 路由，原因在启动时报一次，而不是每个请求各发现一遍。

**宽度不匹配是例外，它抛异常。** 那不是缺席，是配置自相矛盾——照常启动会建出一个
谁都写不进去的 collection。这不是任何人选择的状态。

6 条测试（含 2 条对照）。loader 可注入，与 `BgeM3Embedder.load` 同一个模式：测这几个
分支不该先下载两个 GB——加上之后这组测试从 48 秒降到 0.36 秒。

### 顺带修掉我自己提交的两个游离文件

`git add -A` 在 #35 里扫进了 `bge 2.py` 和 `test_bge_embedder 2.py`——编辑器在我做
`reported_dimension` 修复**之前**留下的副本。

**重复的测试文件才是问题所在**：pytest 收集了它，16 条全部通过——它们测的是旧的访问器
命名，而现在的代码对旧名做了回退。于是没有任何东西报错，测试总数被报成 781，而实际
只有 772 条不同的测试。**一个会跑、还会同意的副本，比一个会坏的副本更糟**，因为它掩盖
了树里存在同一文件的两个版本。

新增守卫拒绝 `thing 2.py` / `thing copy.md` / `thing (1).py` 这类名字，并配一条测试
确认该模式匹配这些形状、且不误伤 `test_2fa.py` 这样的正常命名，再配一条确认被扫描的
清单非空。放回一个副本会让它失败。

## PR-021 Chat 装配进 API 进程（路由仍未注册）

状态：**装配已实现并通过本地测试；HTTP 路由未注册**。

`build_dependencies` 现在组装完整的 chat 栈——embedder、DeepSeek 模型、Qdrant 索引、
会话存储、`ChatService`——并把结果放在 `ApiDependencies.chat` 上；装不出来时放
`chat_unavailable`，写明原因。

**急切加载是个显式参数，不是惊喜。** 装配 chat 会加载嵌入模型。对服务器来说急切
加载是对的——第一个提问不该付掉后面每个提问都不必付的四十秒。但对只跑上传或健康检查
的东西就是错的，所以 `build_dependencies(config, with_chat=...)`。

这一点是**被自己的测试套件抓出来的**。诊断跑给出了确切数字：修复前 `tests/api`
单目录耗时 **900 秒**，修复后 1.9 秒。
因为我本地装了那个可选 extra，于是每一个构造依赖的 API 测试都在真加载 BGE-M3。
CI 没装那个 extra，所以 CI 不会暴露这个问题——只有装了 extra 的开发者会踩到。

### 又一次停在路由前，这次的原因不同

写到路由时发现：`ChatService.ask` 需要一个 `EventSink`，而**只有内存事件日志存在**，
没有 PostgreSQL 的 EventLog 适配器。接上路由等于让 run 的**持久事件写进一个随进程
消失的内存日志**——而基线明确承诺 durable 事件是持久的。

这和 embedder 那个决定同级：要么先做持久事件日志（属于 WP06/WP12 的事件流与 SSE），
要么明确记下「Chat 的审计线索目前不持久」并接受它。我不在余量不足时替这种决定拍板，
所以停在这里，把它记下来。

3 条装配测试：没有嵌入运行时时进程照常装配（上传、artifact 仍在）、chat 缺席且原因
可读、**不替换任何 stand-in**。

## PR-022 持久事件日志（WP06 的存储层）

状态：**已实现并通过本地测试（真实 PostgreSQL）**。路由的最后一块前置。

新增 `events` 与 `event_streams` 两张表（Alembic `0004`）与 `PostgresEventLog`。

**序列在流行锁下分配，不用 identity 列。** 差别是**空洞**：identity 值被一个回滚的
事务消耗掉就永远不会被写入，于是流是唯一的但满是洞——而**订阅者从 cursor 恢复时，
分不清一个洞和一个还没到达的事件**。持有行让同一个流的追加串行化，这才让
`(stream_id, sequence)` 真的意味着「到此为止的全部」。

**流行在首次追加时创建**，不需要单独声明。一个要求生产者先声明流的日志，其失败模式
只在竞态下出现，而修法反正就是这个条件插入。两个追加同时创建同一个流时，双方都条件
插入、再各自锁住最终那一行——和 document store 同一个形状，理由也相同。

**transient 事件返回但不存储**。给它一个位置，就会让 cursor 跳号；存下来又不给位置，
就无法按序重放——两种都是让 cursor 的含义少于它的字面承诺。

重放时**通过写入它的同一个模型校验回来**，所以一行来自本进程不认识的契约时在边界
上 fail closed，而不是半懂不懂地进入某人的重放。`read` 的 limit 自带上限：重放是
客户端发起的请求，不设限就等于允许别人随时让服务器把整条流读进内存。

时钟可注入，与内存实现一致——和墙钟赛跑的测试会在慢机器上失败。

9 条契约测试**同时跑内存与 PostgreSQL 两个实现**（共 18 条）。**验证过是有牙的**：
去掉 `FOR UPDATE` 后，并发无洞那条连续三次全部失败，且只失败那一条。

**明确没做的**：`event_streams` 没有 tenant 列。流的租户不在 `EventScope` 上，而用
`stream_id` 之类推导出来的值去填，是把占位包装成决定——**存一个错的值比不存更糟**。
等 scope 携带租户时，在需要它的那个变更里加。SSE 端点与 `Last-Event-ID` 恢复属于
WP06 的传输层，不在这里。

## PR-023 Chat HTTP 路由（WP04-09）

状态：**已实现并通过本地测试（真实 Qdrant + 真实 PostgreSQL）**。

三个端点：开会话、提问、读历史。**这是这个项目第一次注册外部 RAG 路由**——按计划
的排序纪律，它等到授权链完整（PR-017）、模型可装配（#35）、embedder 决定做完
（#36/#37）、持久事件日志到位（#38）之后才发布。

**路由只在进程能回答时挂载。** 存在但答不了的路由比缺席更糟：缺席是客户端检测一次
的 404，另一种是每个请求一个 500，外加一串「助手为什么坏了」的工单。

**会话显式创建，不由第一个问题隐式打开。** 隐式创建会让「这是哪段对话」取决于顺序，
而一个重试超时请求的客户端会发现自己在第二段对话里、里面装着一半历史。

**一条 stream 对应一个 session，一个 run 对应一轮**：订阅者跟着对话走、从断点恢复，
而每一轮在其中仍可识别。持久事件因此写进这条 stream——这正是路由等待事件日志的理由。

`withheld` 如实上报，不转成 403。问题是被允许的；变的是某个来源在模型写答案期间不再
可读。能区分这两者的客户端可以提议重新提问，而不是为一个被允许的问题显示访问错误。

12 条 API 测试。**未挂载与已挂载两侧都覆盖**——只测「缺 embedder 时是 404」会是守错
对象：一个从来就不工作的 router 也能让那些断言全绿。已挂载那侧用确定性 embedder 与
脚本模型装配出真的 `ChatService`，不下载任何权重、不联网。

**验证过是有牙的**：把条件挂载改成无条件，「缺 embedder 时路由不存在」失败；撤掉会话
owner 检查，「邻居不能提问」与「邻居不能读历史」两条失败。

**仍未做**：SSE 订阅端点与 `Last-Event-ID` 恢复（WP06 传输层）、CLI chat 切片、
`knowledge_search` Tool（WP04-08，属 agentic 路径）。

## PR-024 SSE 订阅与断线恢复（WP06 传输层）

状态：**已实现并通过本地测试（真实 PostgreSQL）**。

`GET /v1/chat/sessions/{id}/events`：按 SSE 推送该会话的持久事件，客户端用
`Last-Event-ID` 从断点续上。

**订阅就是一次还没结束的重放。** 客户端发回它看到的最后一个 id，服务端发送其后的
全部，然后继续发。所以没有单独的「追赶」路径——重连只是一次从更靠后位置开始的订阅。

**cursor 就是 SSE 的 event id**，浏览器因此不需要客户端自己写任何恢复逻辑。它之所以
成立，完全依赖序列无洞：订阅者分不清一个洞和一个还没到达的事件，有洞的日志会让
「n 之后的全部」这个问题无法回答。这正是 #38 那样实现的理由。

**畸形 cursor 从头开始，而不是拒绝连接。** 它来自可能跨过一次部署的浏览器；拒绝会让
那个客户端完全无法重连，而且无从得知清掉它就好了。**指向别的 stream 的 cursor 被忽略**
——沿用它的数字会静默跳过本流的事件。

延迟由轮询间隔决定。配置里的 `wakeup_backend = "postgres_listen_notify"` **仍然没有
消费者**——这是本仓库第五次出现「定义了却没接上」的形状，所以我在这里明说而不是让
行为去暗示它。`catchup_poll_seconds` 与 `replay_page_size` 都真的被用上了。

11 条测试。**验证过是有牙的**：忽略 `Last-Event-ID` 后，恢复那条失败。

**过程中又抓到自己一条守错对象的测试**：`test_transient_events_are_never_sent` 名字
指向流，实际验证的是日志——`read()` 按契约只返回 durable 事件，所以流里那个过滤是
**不可达的**，删掉它一条测试都不会失败。已改名为
`test_a_transient_event_never_reaches_a_subscriber` 并在 docstring 里写明这是由日志
而非流保证的；生产代码里那个 guard 保留（类型上 sequence 可为 None），但注释直说它
不可达、也没有测试覆盖它。

**仍未做**：LISTEN/NOTIFY 唤醒（延迟仍受轮询间隔约束）、CLI chat 切片、
`knowledge_search` Tool。

## PR-025 检索评测：固定语料、gold set、metric 注册表

状态：**已实现并通过本地测试；真实数字已产出（本地）**。WP05-05 的检索部分，
补齐 WP04 退出条件里的语料与 gold questions。

`evals/rag/` 下是 6 篇固定语料与 **23 条 gold questions**（退出条件要求 ≥20），
每条指名回答它的文档。`src/agent_workbench/evaluation/` 下是 metric 注册表与运行器。

**运行器不知道自己在测哪个检索器。** 这正是消融的意义：dense、hybrid、hybrid+rerank
由同一段代码在同一批问题上打分，两份报告的差异才是检索的差异，而不是计数方式的差异。

**报告记录是什么产生了它**——index identity、gold set 摘要、题目数。没有这些的百分比
与任何东西都不可比；两次跑出不同数字只有在测的是同一件事时才有意义，而这恰恰是一个
裸百分比说不出来的。gold set 的摘要尤其重要：**两次运行之间改 gold set，是最容易凭空
造出「改进」的办法**。

**这里不评判答案。** faithfulness 和引用正确率需要模型在环、属于另一个运行器和另一套
证据；混在一起会让检索回归和生成回归看起来一样。

### 真实数字（本地，2026-07-27）

```text
BAAI/bge-m3@main + approx-word-v1-512-64，23 条 gold questions
recall_at_1  0.957     recall_at_3  1.000
mrr          0.971     中位检索延迟  49.9 ms
```

`scripts/run_rag_eval.py` 产出，报告存于 `evals/rag/reports/dense.json`。**CI 不跑它**：
CI 没有嵌入运行时，而用确定性 embedder 跑出来的报告是在测一个哈希函数。

### 差点交出去一组恒真的指标

第一版用 `top_k=10`，报出 `recall_at_5 = 1.0`、`recall_at_10 = 1.0`。查下来发现：语料
只有 6 篇、每篇 1 个 chunk，**top_k=10 每次都把全部 6 篇取回来**——那两个数字报的是
「10 ≥ 6」这个算术事实，不是检索质量。

这与本文件里反复出现的恒真断言是同一类错误，只是这次出现在**指标**里，而指标恰恰是
用来判断其他一切好不好的东西。已改为 `top_k=3`，并把注册表的 recall 上限停在 3，注释
写明理由：**k 不小于语料规模的 recall 是算术不是测量，一个不可能失败的指标也不可能
报告回归**。

10 条 metric 测试全部对着手算得出的数字，21 条运行器测试含两条对照（完美检索器得 1、
盲检索器得 0）。

## PR-026 BGE-M3 sparse encoder（WP05-01）

状态：**已实现；结构测试在所有环境下跑，真 lexical weights 已本地验证**。

`SparseEncoderPort` 与 `BgeM3SparseEncoder`，走 **FlagEmbedding**（BGE 官方库），
加入既有的 `embedding` 可选依赖组。

### 差点走错的捷径

`sentence-transformers` 5.x 提供了 `SparseEncoder`，项目已经依赖它，看起来不必再引库。
动手前实测：

```text
BGE-M3 词表大小:        250002
SparseEncoder 输出维度:   4096
模块链:  Transformer → Pooling → Normalize → SparseAutoEncoder
```

模型卡没声明稀疏头时，它接了个 `SparseAutoEncoder`——把 1024 维 dense 向量压成 4096 维
稀疏码。**那是对 dense 向量的有损重编码，不是词汇匹配。**

危险在于它不报错：Qdrant 存得下、RRF 融得了、评测出数。那条 sparse 支路根本不做 term
matching，现象只是「hybrid 好像没什么用」——一个会被归因为「这语料上 dense 已经够好」
的结论，而不是「sparse 是假的」。架构基线 8.1 点名警告过这件事，我在 #32 里也写过
「sparse 是 WP05-01，届时引 FlagEmbedding」——**我差点用一个看起来现成的 API 绕过自己
写下的判断**。记为 [ADR-013](./adr/0013-bge-m3-sparse-encoder.md)。

### 唯一能区分真假的断言

加载时强制校验**稀疏维度等于分词器词表大小**。别的宽度都会产生 Qdrant 收得下、融合
用得了、却一个词都不匹配的向量，所以这是唯一能判断这条支路是不是真的的检查。

真模型验证（本地，2026-07-27）：12 passed in 53s，其中三条只有真权重在场才跑——维度
实测 250002；真实词项有正权重且索引都在词表内；两段无关文本（「reciprocal rank
fusion」与「preheat the oven」）加权的词项集合不同，**这是 term matching 才有的性质，
4096 维那个假货不可能满足**。

### 许可证门禁抓到一个真问题

`FlagEmbedding` 的包元数据**没有声明许可证**，pip-licenses 报 `UNKNOWN`——而这道门禁
存在的理由正是拦住未声明许可证。

查证：它的 wheel 里附了 MIT LICENSE，只是缺 License 分类器。**没有把 `UNKNOWN` 加进
allowlist**——那会让未来每一个未声明许可证的包自动通过，门禁就废了。改成用
`--ignore-packages` 按包点名豁免，并把证据写进 CI 注释里：一个包、一行、一个必须有人
写下来的理由。其余新增的都是宽松许可证的不同拼写，照常加进 allowlist。

### 已知重复

dense 走 `sentence-transformers`，sparse 走 `FlagEmbedding`——**这是已知重复，不是设计**。
基线要求的是一次前向同时产出两种表示。若 FlagEmbedding 的 dense 输出被验证与现有结果
一致，dense 应当合并过去；在验证之前不动，因为换 embedder 会改 index identity，那是一次
全量重建索引。

**未做**：Qdrant 单次 dense+sparse RRF 融合（WP05-02）、reranker（WP05-03/04）、
对照 dense 基线（`recall@1 0.957 / mrr 0.971`）的消融报告。

## PR-027 Qdrant 单次 dense+sparse RRF 融合（WP05-02）

状态：**已实现并通过本地测试（真实 Qdrant）**。

collection 现在同时声明 dense 与 sparse 具名向量，`search_hybrid` 用一次
`query_points` 发两条 prefetch 加一个 `FusionQuery(RRF)`。

**融合发生在 Qdrant 内部，这个进程从不同时持有两份排序表。** 基线把融合所有者锁定为
Qdrant Query API，并禁止适配器再做一次 relative-score fusion——而「不做第二次」最可靠
的保证方式，就是让这一侧根本拿不到可以再融的两份列表。

点没有稀疏权重时**不写空稀疏向量**：写了会让它「匹配所有稀疏查询、权重为零」，而不是
「不匹配」。collection 因此可以在 sparse 铺开期间同时容纳两种点。

`_narrowing` 抽成共享定义：dense 与 hybrid 两条路必须用同一份过滤器，两份拷贝就是两次
让某一条路授权得不一样的机会。

### 同一个错误，连续两个 PR

第一版 8 条测试**全绿地通过了「删掉整条 sparse prefetch」**。原因与上一个 PR 那个恒真
指标一字不差：collection 里只有 2 个点，而 `dense_limit=10`——**dense 自己就把两个点都
取回来了**，`doc_lexical` 出现在结果里跟 sparse 毫无关系。

改成每条支路 `limit=1`（小到只够够到自己那个点）后，撤掉 sparse prefetch 失败 2 条。

**教训是同一条**：一个不可能排除任何东西的 limit，也不可能证明任何东西。上个 PR 出现在
评测指标里，这个 PR 出现在测试夹具里。

### 一处写下但未验证的断言，已订正

代码注释里我写过「过滤器必须放在 prefetch 上，否则未授权点会占掉名额」。实测：移到融合
查询上，8 条测试**全部照过**——Qdrant 会把外层过滤器下推。

那句话是推测不是事实。已改为如实陈述：两种写法在这里等价；仍按 per-arm 写，是**不想让
授权保证依赖查询优化器的行为**，但明说了测试区分不了这两种写法。

顺带订正一条旧断言：`test_an_unauthorized_principal_gets_nothing_from_either_arm` 写错了
对象——一个对谁都不返回结果的索引也能满足它。已改成同时断言该 principal **能**看到什么。

8 条测试。**未做**：把 sparse encoder 接进摄取与检索链路，以及对照 dense 基线
（`recall@1 0.957 / mrr 0.971`）的消融报告。

## PR-028 把 sparse 接进摄取与检索链路

状态：**已实现并通过本地测试（真实 Qdrant + 真实 PostgreSQL）**。

`IngestionService` 与 `RetrievalService` 各自新增可选的 `sparse_encoder`。

**没有 sparse encoder 时走 dense，而且不假装自己是 hybrid。** `RetrievalService.mode`
如实返回 `"dense"` 或 `"hybrid"`——评测报告因此不可能把一次 dense 运行标成 hybrid，
消融也不可能把一个检索器和它自己比。这是「缺席要可读」在这条链路上的形态。

**sparse encoder 进入 index identity。** sparse 改变的是**一个点是什么**，不只是它额外
携带了什么：一个半稀疏的 collection 会用一条支路给一部分点排序、用两条给另一部分排序。
identity 不同则 chunk id 不同，两者永不共享同一个点，重建索引因此是看得见的，而不是一次
静默覆盖。

**没有权重时不写空稀疏向量**——空向量会「匹配所有稀疏查询、权重为零」，而不是「不匹配」。

**sparse 与 dense 同样是全或无**：先整批编码再写入。失败若留下「前几个 chunk 匹配词项、
其余不匹配」的版本，那个版本会自己和自己竞争排序。

**两条支路各自拿完整候选数**，不各减半：RRF 的职责就是把两份完整候选收敛成一份，
让它在两份已被截断的列表之间选，是另一个检索器，不是被评测的那个。

8 条新测试。**验证过是有牙的**：摄取侧丢掉权重失败 1 条；sparse 不进 identity 失败 2 条；
`mode` 恒返回 hybrid 失败 1 条。

**未做**：消融报告本身。`scripts/run_rag_eval.py` 还需要接上 sparse encoder 并按 `mode`
命名报告，才能对照 dense 基线（`recall@1 0.957 / mrr 0.971`）回答「hybrid 有没有用」。

## PR-029 dense vs hybrid 消融（WP05-08）

状态：**已跑出真实数字（本地）**。完整报告见
[`evals/rag/reports/ABLATION.md`](../evals/rag/reports/ABLATION.md)。

| 指标 | dense | hybrid |
|---|---|---|
| recall@1 | 0.957 | 0.957 |
| recall@3 | 1.000 | 1.000 |
| MRR | 0.971 | 0.971 |
| 中位延迟 | 52.7 ms | 38136.2 ms |

**结论是「测不出差别」，不是「hybrid 没用」。** 三个质量指标逐位相同。但这份语料只有
6 篇文档、每篇 1 个 chunk，问题措辞与原文高度重合，dense 已经 23 题错 1——**留给 sparse
的空间只有那一题**。持平既可能说明 sparse 无增益，也可能说明语料测不出差别，**用现有
数据区分不了**。把它写成「hybrid 不值得做」，是拿一个测不出差别的实验去支撑关于方案的
结论。

**那个 38 秒衡量的是当前实现，不是方案。** 每次查询重跑一遍 BGE-M3 稀疏编码，CPU 上
无预热、无批处理、无常驻——基线 8.1 要求的这些我都没做。可以用它说「当前实现不能用于
交互式查询」，不能用它说「hybrid 太慢」。

报告里写清了下次消融需要语料补什么：罕见标识符、精确短语、错别字、缩写与全称并存——
**现在这份语料里一个都没有**，所以在补进之前，对比只会继续持平且继续什么都不说明。

脚本改为一次跑两种模式、各自独立 collection（sparse 改变 index identity，共用会让 dense
读到为 hybrid 建的点），报告按模式命名。

## PR-030 扩充语料，并订正上一次消融的归因

状态：**已跑出真实数字（本地）**。报告见
[`evals/rag/reports/ABLATION.md`](../evals/rag/reports/ABLATION.md)。

语料 6 → 10 篇，gold set 23 → 38 题。新增 4 篇各带一类 dense 会漏、sparse 该命中的
把手：罕见标识符（`AWB-4471`）、逐字短语、真实工单里的错拼（`Qrdant`、
`rag.embeding.model_id`）、缩写歧义（RRF / ACL / MRR）。问题按**用把手提问**写，
不用改述——改述提问 dense 照样能对齐，测的就还是原来那件事。

**结果是负的：hybrid 劣于 dense**（recall@1 0.947 → 0.868，MRR 0.961 → 0.917）。

在具备 sparse 该擅长条件的语料上仍然更差，说明问题在 sparse 一侧而非语料。**但这不
等于 sparse 无用**——可能是编码、写入或融合权重，目前没有证据区分，所以报告里不写
诊断。下一步是逐题查 sparse 单独检索的命中情况。

### 我自己写下警告、又亲手违反了它

上一次消融（6 篇）的持平，我归因为"语料太小"。**那个归因不完整**：评测脚本当时给两条
支路各只有 `TOP_K = 3` 个候选，让 RRF 在两份已截断的列表间融合——而
`RetrievalService` 里我写过注释明令禁止这种做法。

6 篇语料下 `limit=3` 几乎是全库，截断的伤害看不出来；扩到 10 篇才显现成负增益。修复后
延迟从 30.3 秒降到 184 毫秒（快 165 倍）——**每条支路只取 3 条却要跑 30 秒，那个数字
当时就该让我起疑**，我却把它归到了「hybrid 的实现代价」上。

两个教训是同一个：一个跨文件重复的策略，注释拦不住，只有让它无法被分别设定才拦得住。

## PR-031 sparse 诊断：查到一半，并撤回一个错误结论

状态：**未定论**，记录见
[`evals/rag/reports/SPARSE-DIAGNOSIS.md`](../evals/rag/reports/SPARSE-DIAGNOSIS.md)。

追查 hybrid 为何劣于 dense。绕开融合、只用 sparse 检索，发现：

1. **sparse 编码跨进程不可复现**。同一查询三次独立运行编出不同词项数
   （`RRF`: 0 / 2 / 0；`Qrdant`: 0 / 2）。**但每次运行内部完全自洽**——同进程连编
   三次相同，批量与单条一致。所以不是随机性，是跨进程状态差异。
2. **即使编出词项，sparse 排序也不对**：`AWB-4471` 编出 4 个词项，却把唯一含该错误码
   的文档排在第 3。

已排除：短查询固有编不出词项、批量路径 bug、随机性、融合权重（sparse 单独检索本身
就排不对）。最可能的方向是 **float16 与 MPS 后端的舍入差异**让阈值边缘的词项在不同
进程落到两侧——**未验证**，下一步是固定 `use_fp16` 与 device 后跨进程重测。

### 撤回了一个我在同一轮里给出的结论

我先报告过「短查询固有地编出零词项，这是 BGE-M3 的已知行为」。那是**从单次运行读出
的性质**——我在把它写成测试时，下一次运行立刻推翻了它（`Qrdant` 编出 2 个词项）。
两条测试已撤回，未提交。

**这一轮没有把任何单次观察写成测试。** 把不可复现的观察钉成契约，是把噪声固化——
比不写更糟。

### 一条已合并的测试是不稳定的

`test_the_real_model_weights_real_terms` 断言 `reciprocal rank fusion` 词项数 `> 0`，
而该查询实测在不同运行得到 0 和 1。**它一直通过是运气。** 没有改动它：在编码稳定
之前，改成任何具体期望值都是固化噪声。报告里记了这一条。

## PR-032 排除 float16/MPS 假设，指向权重加载

状态：**假设被推翻，诊断仍未定论**。见
[`SPARSE-DIAGNOSIS.md`](../evals/rag/reports/SPARSE-DIAGNOSIS.md)。

上一轮猜测 sparse 编码的跨进程差异来自 float16 与 MPS 舍入。**固定 CPU + float32 后
两个独立进程仍然不同**，且差异更大（`recieved chunk batch`: 7 项 vs 1 项，且那 1 项
的索引不在 7 项之内）。**精度与设备都不是变量，假设不成立。**

新线索指向**权重未完整加载**：进程 2 里几乎每条查询都塌成 1 个词项、且换成了全新的
词——这不是舍入抖动的形态。BGE-M3 的稀疏头（`sparse_linear`）是独立于主干的一组权重，
**主干加载成功不代表它也加载了**；若它回退到随机初始化，此前所有 sparse 数字（含 #46
的负结果）全部作废。

下一步三项写在报告里：比对两次加载的 `sparse_linear` 权重、确认 `sparse_linear.pt`
是否真的被读取、必要时作废既有 sparse 结论。

**没有给出第三个诊断。** 这一轮已经撤回过一个从单次运行读出的结论；在余量不足时再猜
一个，只会再撤一次。否定结果本身是有价值的产出：它把搜索空间从「精度/设备」缩到了
「加载」。

## PR-033 摄取 worker：把上传接到索引上（WP05-07）

状态：**已实现并通过本地测试（真实 PostgreSQL + Qdrant）**。

**这补的是主体基线上的一处真实断裂**：`workers/` 此前只有 `__init__.py`，没有任何
东西消费 outbox；`IngestionService` 在 `src/` 里除自身模块外无人调用。也就是说
**上传一个文档会写出 outbox 事件，然后没有任何东西索引它**——整条 Chat/RAG 之所以
跑得通，是因为测试和评测脚本**直接调用**了摄取服务。生产路径上传的文档永远不会被检索到。

**事件是唤醒信号，不是数据来源。** worker 锁住 document 行、重读 PostgreSQL **当前**
快照、索引那个快照。稳定 point id 只解决重复投递；乱序投递靠的是这条——按事件自身
payload 应用，晚到的旧事件就会盖掉新版本。

`last_applied_revision`（迁移 `0005`）是比较对象。旧于它的事件标记 superseded 而不
应用，**但仍然 ack**：一个描述过去状态的事件下次仍然描述过去状态，留在队列里只会让
worker 永远重新发现它。

**先索引、后记 revision。** 反过来会让一次崩溃把没做的事标记成已做，而且没有任何东西
会察觉——文档只是永远不可搜索。

取字节与跑模型放在事务之外：把数据库事务横跨这两件慢事，等于让别人排在一把锁后面。

6 条测试，含 1 条对照（worker 跑之前索引里什么都没有——没有它，"上传变成可搜索"
这条断言在一个急切写入的实现下也会通过）。**验证过是有牙的**：撤掉 revision 比较，
「旧事件被 superseded」失败。

### 一处未被测试覆盖，如实记下

`UPDATE ... WHERE last_applied_revision < revision` 里的那个条件是防两个 worker
竞态的。撤掉它**全部测试照过**——我的测试都是单 worker 顺序执行，覆盖不到它。
保留是因为它是正确的，但**没有测试守着**，多 worker 竞态测试属于 WP09 的故障支架。

## PR-034 `knowledge_search` Tool（WP04-08）

状态：**已实现并通过本地测试**。检索作为工具暴露给 agentic 路径。

**包装的是同一个 `RetrievalService`**，所以两条路径产出同一个 `ContextPacket`。两个
检索器意味着两套授权检查、两种引用形状、两个要评测的东西——而受关注较少的那个，
就是会漏的那个。

**principal 来自执行上下文，绝不来自参数。** 这是这个工具存在的关键安全性质：
参数是一次工具调用里**唯一能被不可信文本触达**的部分——一段检索回来的文字写着
「以 user_admin 身份搜索」，就是它在给自己授权。schema 里**没有** principal 字段
（`additionalProperties: false`），handler 也只从 run 读。

knowledge_base 是参数，因为「去哪找」是模型的工作；而找一个它无权读的知识库，会被
和别处同一道 PostgreSQL 检查拒绝——**收窄到某个知识库不是授权，也不会因为它来自参数
就变成授权**。

5 条测试。**验证过是有牙的**：把 handler 改成从参数取 principal，「模型不能自选身份」
那条失败。另有一条断言直接钉在 schema 上（字段集合恰为 query/knowledge_base_id/top_k，
且 `additionalProperties: false`），这样将来悄悄加一个字段会被测试拦下，而不是等到
运行时。

**尚未装配进 ChatService**：默认 Chat 仍然是固定 2-step、工具清单为空。把它接上属于
深度研究模式的开关，是另一个行为变化。
