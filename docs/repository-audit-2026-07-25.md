# Agent Workbench 仓库核验报告

- 核验日期：2026-07-25
- 核验提交：`main@f071323`
- 覆盖范围：PR-001～PR-015、ADR-012、源码、迁移、测试、配置、CI 与文档
- 核验方式：静态审查、确定性缺陷复现、本地门禁、PostgreSQL 16 集成测试、
  GitHub Actions 结果复核
- 本轮变更边界：只校正文档与状态，不修改生产代码

## 0. 复核状态

本报告由 Codex 产出后经过一次独立复核（2026-07-25，同一提交 `f071323`）。

**结论：14 条缺陷断言全部成立，统计数字逐项吻合。** 复核方式是对每条行为类断言
重新编写复现脚本、对每条结构性断言重新对照源码，不采信报告原文的措辞。

| 类别 | 复核方式 | 结果 |
|---|---|---|
| P0-1、P1-2、P1-4、P1-7、P1-9、P1-10、P2-1、P2-2 | 对照源码 | 成立 |
| P0-2、P1-5、P1-6、P1-8 | 独立复现脚本 | 成立 |
| P1-1、P1-3 | 真实 PostgreSQL 复现 | 成立 |
| 统计数字（231/47/119/544/499/45/160/17） | 重新测量 | 全部一致 |

复核环境与 CI 的差异：本次复现使用本机 PostgreSQL 15.14，CI 使用
`postgres:16`（镜像按 digest 固定）。P1-1、P1-3 涉及的行锁、`ON CONFLICT`
与事务语义在两个版本上一致，结论不受影响。

第 4 节中有两处描述已按复核结果订正，见 P1-5 与 P1-8 的“复现”条目。

## 1. 结论

仓库已经从“架构与骨架”推进到一个有实质代码的 Runtime + Persistence + Upload
API 基线，但还不能称为完整的 Chat/Task Agent 产品，也暂不适合继续堆 RAG 或
LangGraph 功能。

建议先冻结新功能，依次解决以下四个阻断面：

1. 开发身份解析器可能随默认 `0.0.0.0` 监听暴露到局域网；
2. `requires_approval=True` 被 Runtime 忽略，写工具可直接执行；
3. Upload/Document/Artifact 只有 tenant 隔离，缺同 tenant 内的 owner/ACL 授权；
4. Runtime 的 tool/token/cost budget 不是硬上限。

质量门禁整体健康，但通过的测试不能抵消这些缺失场景：现有 IDOR 测试只换 tenant，
API 测试不真实绑定 socket，预算测试没有覆盖越界批次，审批测试只验证 Policy DTO。

> 本节记录的是 2026-07-25 核验当时的状态，不随后续修复改写。截至 2026-07-26，
> 上列第 1、2 项已修复并有回归测试，逐条记录见 §7；第 3 项修掉了 Upload/Document
> 一半（P1-1、P1-3），Artifact 一半（P1-2）与第 4 项仍然开着。

## 2. 已实现能力

| 能力 | 实现 | 测试 | 说明 |
|---|:---:|:---:|---|
| 配置加载、ownership 与 CI 合同 | ✓ | ✓ | 当前 231 个 Settings 叶子字段、47 个组 |
| Domain、Ports、Fake Adapter | ✓ | ✓ | 框架无关契约与 golden tests |
| 自研串行 Tool Loop | ✓ | ✓ | CLI 有固定演示 |
| Policy / Tool Gateway / Hook Bus | ✓ | ✓ | 存在下述审批、改写与 deadline 缺陷 |
| 预算、取消、并行只读与 exclusive 屏障 | ✓ | ✓ | 调度顺序正确；预算硬上限不成立 |
| DeepSeek 流式 HTTP Adapter | ✓ | ✓ | 仅 MockTransport contract，未装配到进程 |
| PostgreSQL ConversationStore | ✓ | ✓ | Alembic `0001` |
| Local ArtifactStore | ✓ | ✓ | 下载路径并非真正流式 |
| Document / Version / ACL / Outbox | ✓ | ✓ | Alembic `0002`；同 tenant 对象授权不完整 |
| Upload / Artifact / Health API | ✓ | ✓ | 本地开发 API，不是 Chat/Task API |

明确尚未实现：

- LlamaIndex、Qdrant、BGE、RAGAS 与 Chat RAG；
- LangChain 互操作 Adapter、LangGraph Task 与 Multi-Agent；
- Task Registry、lease、heartbeat、fencing、checkpoint、LISTEN/NOTIFY、SSE；
- Ingestion/Task Worker、S3、React UI、Docker Compose、生产身份认证；
- DeepSeek 的进程级装配、真实服务 smoke/E2E；
- OpenTelemetry/Langfuse 的产品链路。

## 3. 质量门禁结果

| 门禁 | 结果 |
|---|---|
| `uv sync --frozen --group dev --no-editable` | 通过 |
| Ruff format / lint | 119 个文件通过 |
| Pyright | 0 errors、0 warnings |
| pytest 收集 | 544 项 |
| 无数据库环境 | 499 passed、45 skipped |
| PostgreSQL 16 集成套件 | 160 passed |
| Alembic | `0001 → 0002` 通过 |
| development/test/production profile | 全部通过 |
| CLI golden | 逐字节一致 |
| 许可证 allowlist | 通过 |
| Gitleaks | 17 个提交，无泄漏 |
| Actionlint | 通过 |

PostgreSQL 套件与无数据库套件有重叠，不能把 499 和 160 相加当作总测试数。最新
GitHub Actions 的 quality、postgres、secret scan 均成功：
[run 30184299195](https://github.com/he-zi-qiang/agent-workbench/actions/runs/30184299195)。

现有门禁缺口：

- 没有 coverage/branch coverage 阈值；
- Pyright 不检查 `tests/` 和 `migrations/`；
- 没有依赖 CVE 或 SBOM 扫描；
- CI 只覆盖 Ubuntu、Python 3.12、PostgreSQL 16；
- DeepSeek 没有可选真实服务 smoke test；
- API 只有进程内 ASGI 测试，没有 Uvicorn、真实 socket 或反向代理测试；
- PostgreSQL 竞态测试未覆盖多进程崩溃、连接中断、PgBouncer 与 fault injection；
- 本地缺 DSN 时 45 项数据库测试静默跳过；
- Actionlint 尚未进入 CI。

## 4. 缺陷清单

### P0-1 默认本地配置可监听所有网卡，身份可由调用者伪造 —— 已修复（2026-07-26）

修复见 §7。以下是缺陷成立时的原始记录，保留以便对照。

涉及：

- `config/config.default.toml:8-20`
- `src/agent_workbench/bootstrap/settings.py:141-153`
- `src/agent_workbench/apps/api/dependencies.py:60-70`
- `src/agent_workbench/apps/api/identity.py:40-56`
- `src/agent_workbench/apps/api/main.py:94-100`

默认 `deployment_scope="local"` 与 `api.host="0.0.0.0"` 同时成立。装配只拒绝
`remote` scope，不限制 local 的监听地址；局域网调用者可以自行填写
`x-tenant-id` 与 `x-principal-id`。

修复方向：默认改为 loopback；Header Identity Resolver 启用时在 Settings 和
装配层双重强制 loopback/Unix socket；增加真实 socket 失败测试。

### P0-2 审批要求被 Gateway 忽略 —— 已修复（2026-07-26）

修复见 §7。以下是缺陷成立时的原始记录，保留以便对照。

涉及：

- `src/agent_workbench/domain/policies.py:64-80`
- `src/agent_workbench/adapters/policy/envelope.py:47-50`
- `src/agent_workbench/runtime/tool_gateway.py:216-237`

Policy 可返回 `effect="allow", requires_approval=True`，但 Gateway 只判断 effect，
随后直接执行 handler。已确定性复现：需要审批的 write handler 执行一次，run
最终状态是 `completed`。

复核补充：`requires_approval` 在 `domain/policies.py` 定义、由
`EnvelopePolicyEngine` 按风险等级设置，但整个 `runtime/` 包**没有任何一处读取
它**——它目前是一个只写字段。这使得缺陷不依赖具体 Policy 实现：任何返回
`allow + requires_approval=True` 的引擎都会被直接放行。

修复方向：审批设施完成前 fail closed；完成后发出权限请求并暂停 run，审批前绝不
进入 `invoke()`。

### P1-1 同 tenant 的非 owner 可接管上传并覆盖文档 —— 已修复（2026-07-26）

修复见 §7。以下是缺陷成立时的原始记录，保留以便对照。

涉及：

- `src/agent_workbench/apps/api/routes/uploads.py:100-145`
- `src/agent_workbench/application/uploads.py:70-105`
- `src/agent_workbench/adapters/persistence/documents.py:105-189`
- `src/agent_workbench/adapters/persistence/documents.py:311-338`

transfer/complete 往下只传 tenant，不传当前 principal。同 tenant 用户知道 upload
ID 后可替 owner 传输或完成，也可把自己的 upload 指向他人的 document 并改写内容
和 ACL。

此外，既有文档属于 KB-A 时可提交 KB-B：document 行仍是原事实，outbox payload
却携带 KB-B，数据库与索引事件发生矛盾。

修复方向：Port/Application Service 显式接收 principal；锁行后校验 upload owner、
document owner/ACL 与 knowledge-base 权限；补同 tenant/不同 principal 的 IDOR
矩阵。

### P1-2 Artifact 缺对象级授权

涉及：

- `src/agent_workbench/domain/artifacts.py:43-52`
- `src/agent_workbench/ports/artifact_store.py:61-70`
- `src/agent_workbench/apps/api/routes/artifacts.py:21-32`

ArtifactRef/Port 只有 tenant，没有 owner 或授权关系。同 tenant 的任意用户只要知道
artifact ID 就能下载。UUID 难猜不能替代授权。

修复方向：在 PostgreSQL 持久化 owner/对象关系，下载前按 principal 授权，再打开
blob。

### P1-3 相同内容重传会静默忽略 ACL 变更 —— 已修复（2026-07-26）

修复见 §7。以下是缺陷成立时的原始记录，保留以便对照。

涉及：

- `src/agent_workbench/adapters/persistence/documents.py:145-150`
- `src/agent_workbench/adapters/persistence/documents.py:175-192`

digest 相同会提前返回旧 version，既不替换 ACL，也不产生 `acl_changed` 事件。
因此“相同内容但撤销授权”不会生效。

修复方向：分别比较内容与 ACL；内容相同、ACL 不同时原子更新 ACL、推进授权
revision 并写 outbox。

### P1-4 Artifact 下载会整体读入内存

涉及：

- `src/agent_workbench/adapters/artifacts/local.py:142-146`
- `src/agent_workbench/apps/api/routes/artifacts.py:25-35`

`Path.read_bytes()` 先把最多 100 MiB 的对象读完，再把单个 bytes 包成
`StreamingResponse`。它不是实际的分块下载。

修复方向：为 ArtifactStore 增加 `open_stream()`/`iter_chunks()`，路由按块发送。

### P1-5 tool/token/cost budget 不是硬上限

涉及：

- `src/agent_workbench/runtime/agent_runtime.py:202-234`
- `src/agent_workbench/runtime/agent_runtime.py:393-545`

确定性复现结果：

- 一轮提出两个调用时，两个 handler 都会执行，随后 run 才因预算终止；
- `max_total_tokens=1` 时模型上报 120 tokens，run 仍 `completed`，
  `usage.tokens.total` 记为 120；
- `cost_micro_usd` 没有生产者，成本预算永远不触发。

**复现（复核订正）**：初稿写作“`max_tool_calls=1`”，但该预算无法构造——
`RunBudget` 的域校验要求 `max_tool_calls >= max_steps`，
`max_steps=4, max_tool_calls=1` 会直接抛 `ValidationError`。可复现的最小配置是
`max_steps=1, max_tool_calls=1`：一轮提出两个只读调用，两个 handler 均执行，
run 随后以 `stop_reason="max_steps"` 失败——**不是** `max_tool_calls`，因为步数
先触顶。副作用已经发生这一点不变，缺陷成立；变的只是触发路径的描述。

修复方向：dispatch 前预留 tool-call 配额；每轮合并 usage 后、完成 run 前再次检查；
按固定 model revision 接入计价器。

### P1-6 Policy 改写绕过参数大小限制

涉及：

- `src/agent_workbench/runtime/tool_gateway.py:192-204`
- `src/agent_workbench/runtime/tool_gateway.py:239-248`

原始输入走大小与 schema 检查，Policy 的 `modified_input` 只重跑 schema。
已复现：`max_argument_bytes=64` 时，handler 仍收到 10,000 字节参数。

修复方向：Policy 与 Hook 的每轮改写统一调用完整 `_check()`。

### P1-7 Policy/Hook 不受完整 run deadline 约束

涉及：

- `src/agent_workbench/runtime/tool_gateway.py:206-248`
- `src/agent_workbench/runtime/hook_bus.py:87-106`
- `src/agent_workbench/runtime/agent_runtime.py:488-569`

Policy 卡住或抛异常时可能绕过 run 的正常终态；多个 Hook 各自使用固定 timeout，
即使 run 只剩极短时间也不会取剩余 deadline 的最小值。

修复方向：把剩余 deadline/cancellation 贯穿 prepare/authorize；Policy 超时与
异常一律归一化并 fail closed；Hook 使用 `min(hook_timeout, remaining_run)`。

### P1-8 重复 tool_call_id 在副作用之后才被拒绝

涉及：

- `src/agent_workbench/runtime/agent_runtime.py:363-390`
- `src/agent_workbench/runtime/agent_runtime.py:488-542`
- `src/agent_workbench/domain/tools.py:219-244`

同一模型轮次出现重复 ID 时，Runtime 会先执行两个 handler，到结果配对才抛错。

**复现（复核补充）**：两个 handler 各执行一次后，`align_results()` 抛出
`ToolPairingError`，而该异常**逃出了 `run()`**——调用方拿到的是异常，不是终态
`AgentOutcome`。这同时违反 `AgentExecutor` 的协议约定（“实现必须返回终态
outcome，而不是对可预期的失败抛异常”），因此本项不只是“顺序不对”，还会让
Graph node 拿不到可记录、可路由的结果。

修复方向：模型 turn 完成后、任何授权或执行前检查 call ID 唯一性；违规整轮失败、
零 handler 调用，并归一化为终态 outcome 而非异常。

### P1-9 DeepSeek 对损坏 SSE frame fail open

涉及：

- `src/agent_workbench/adapters/models/deepseek.py:154-183`
- `src/agent_workbench/adapters/models/deepseek.py:314-349`

非法 JSON frame 被静默丢弃；若它处于工具参数片段中，剩余片段仍可能组成另一份
合法 JSON。超长 delta 等 Pydantic 校验错误也会直接逃出 ModelPort。

修复方向：非注释、非 `[DONE]` 的非法 frame fail closed；限制累计文本与 partial
arguments；把 provider 解码/领域校验异常归一化为终态 error。

### P1-10 Outbox claim 在 worker 崩溃后不可恢复

涉及：

- `src/agent_workbench/adapters/persistence/outbox.py:45-97`
- `src/agent_workbench/adapters/persistence/models.py:225-227`

claim 提交后、ack 前崩溃，事件会永久保持 `claimed_at IS NOT NULL`；ack 也不验证
claim owner 或 fence。当前文档已承认这只是竞争领取，不是 lease。

修复方向：启用 ingestion worker 前加入 `lease_until`、claim token/epoch、过期
reclaim；ack 比较 worker/token/epoch 与更新 rowcount。

### P2-1 DeepSeek 可靠性配置尚未进入 Adapter

`timeout_seconds`、`max_retries`、`tool_calling_required` 已存在于 Settings，但
DeepSeekProfile 与 HTTP 调用没有消费它们。进程装配前必须对齐配置语义。

### P2-2 Runtime 只关闭具体 AsyncGenerator

ModelPort 允许一般 `AsyncIterator`，但 timeout/cancel 只对具体
`AsyncGenerator` 调用 `aclose()`。自定义可关闭 iterator 可能泄漏连接。

## 5. 已确认正确的关键实现

- `plan_tool_batches()` 的连续 parallel 分组、exclusive 屏障与稳定提交顺序正确；
- PostgreSQL document/version/outbox 同事务提交正确；
- document row lock 下的 revision 分配，以及首次并发创建的条件插入方案合理；
- 上传边读边写、检疫文件完成后发布、服务端路径 containment 正确；
- DeepSeek 正常 tool fragment 按 index 拼装与 HTTP 错误正文脱敏正确；
- 跨 tenant 的统一 not-found 语义正确；授权缺口集中在同 tenant 的 principal 层。

## 6. 建议修复顺序

1. **Security boundary PR**：loopback 强制、真实 socket 测试、ADR 一致性。
2. **Approval fail-closed PR**：审批前零副作用，并补 Runtime 级测试。
3. **Object authorization PR**：Upload/Document/Artifact 全链路 principal + IDOR
   矩阵，同时修 KB 一致性与相同内容 ACL 变更。
4. **Runtime hard limits PR**：tool/token/cost、rewrite size、deadline、重复 ID。
5. **Streaming/provider hardening PR**：Artifact 分块下载、DeepSeek fail closed、
   iterator 关闭。
6. **Reliable Outbox PR**：在真正启用 ingestion worker 前实现 lease/fencing。
7. 完成上述阻断项后，再进入 WP04 Dense Retrieval。

每个修复应保持“一项主要行为变化一个 PR”，并为审计中描述的触发条件加入回归
测试。没有回归测试的修复不能把本报告中的对应项标记为关闭。

## 7. 修复记录

按 §6 的顺序逐条修复。一项主要行为变化一个 PR，且必须先有覆盖触发条件的回归
测试，才能把对应缺陷标记为关闭。

### P0-2 审批 fail closed（2026-07-26）

行为变化：`ToolGateway.authorize()` 在两个 allow 分支**之前**检查
`decision.requires_approval`。为真时发出 `PermissionRequested`，随后以
`approval_required` 拒绝该次调用，handler 不再被触及。

之所以是拒绝而不是暂停 run：审批设施（`ApprovalStore`、恢复入口）属于 WP10，
尚未实现。在它到位之前，唯一诚实的语义是「这次调用需要人来决定，而这里没有人
可以决定」。等 WP10 完成后，这一分支改为挂起并等待裁决，Gateway 之外无需改动
——`PermissionRequested` 事件与 `approval_required` 错误码从 PR-003 起就已经在
Domain 里定义好，此前一直没有写入方。

模型侧看到的是一条 `status="error"` 的 tool result，正文含 `approval_required`，
因此模型能区分「不被允许」和「尚未裁决」，run 本身继续正常收尾。

回归测试（6 条，撤掉修复后前 5 条全部失败）：

| 测试 | 断言 |
|---|---|
| `test_a_decision_requiring_approval_does_not_reach_the_handler` | handler 零调用；错误码 `approval_required`；事件序列 |
| `test_a_rewrite_cannot_smuggle_a_call_past_its_approval_requirement` | allow_modified 分支同样被拦 |
| `test_a_tool_needing_approval_never_reaches_its_handler` | Runtime 级完整 run，副作用为零 |
| `test_the_audit_trail_says_a_human_was_needed_not_that_it_was_denied` | 持久事件序列含 `PermissionRequested` |
| `test_the_model_is_told_the_call_awaits_approval` | 回灌给模型的 tool result 可区分两种拒绝 |
| `test_an_envelope_without_an_approval_requirement_still_runs` | 对照组：拒绝跟的是审批要求，不是风险等级 |

最后一条是对照组，因此在撤掉修复后仍然通过——正是它保证前五条不是由「write
工具一律拒绝」这种过度实现凑出来的。

### P0-1 监听地址强制 loopback（2026-07-26）

行为变化：`api.host` 默认从 `0.0.0.0` 改为 `127.0.0.1`；`ApiSettings.host` 只
接受 loopback 地址；`build_dependencies()` 在选定 Header Resolver 之前再校验一次。

规则写成**无条件**的，没有以 `deployment_scope` 为条件。scope 是部署给自己贴的
标签，而决定谁能触达 Header Resolver 的是绑定地址；把标签当作绑定地址的代理，
正是本条缺陷成立的原因。remote scope 拒绝装配的那道检查保持不变，两者互不替代。

不是 `localhost` 的主机名一律拒绝而不解析：解析在校验时和 bind 时可以给出两个
答案，且 DNS 在两者之间可以改变，「不确定」的安全答案是否。

回归测试 16 条，新增 `tests/api/test_bind_address.py`：

| 层 | 断言 |
|---|---|
| 提交默认值 | 直接读 `config.default.toml`，其 `api.host` 是 loopback |
| Settings | `0.0.0.0`、`::`、`10.0.0.4`、`192.168.1.20`、`0.0.0.0:8000` 与非 `localhost` 名字均被拒；四种 loopback 形式被接受 |
| 装配 | 绕过 Settings 直接构造的 `ApiRuntimeConfig` 同样被拒；默认配置照常装配 |
| **真实 socket** | 按**原始 TOML 值**（不经 Settings）`bind()`，断言内核分配的地址是 loopback，且从本机自己的可路由地址连不上 |
| **对照** | 同一条连接在 `0.0.0.0` 绑定下必须连得上 |

socket 那两条刻意绕开 `Settings`。只见过校验器放行的值的 socket 测试，抓不到
校验器本身写错——它要在校验器都被删掉时仍然成立。已逐层验证：撤掉 Settings
校验失败 6 条，撤掉装配层校验失败 1 条，只把默认值改回 `0.0.0.0` 失败 5 条，
三者全撤失败 11 条，其中真实 socket 那条是靠一次成功的跨接口连接抓到的。

对照组是必需的：没有它，「连接被拒绝」既可能说明护栏有效，也可能说明探针指向了
一个本来就没人监听的端口。这正是本条缺陷能存活的原因——
`test_the_api_refuses_a_remote_deployment_scope` 断言的是 scope 标签，看起来像在
守护整个边界。该测试保留（它确实守住了 scope 那一半），docstring 已写明它不覆盖
监听地址。

范围说明：这挡住的是**意外暴露**，不是认证。反向代理、SSH 端口转发或容器端口
映射仍可以把 loopback 进程送上网络——那是部署方的选择，代码拦不住，也不该假装
拦得住。生产身份认证仍是 Planned，能力表不因此升级。

### P1-1 上传与文档的对象级授权（2026-07-26）

行为变化：`DocumentStore` 的每个方法都显式接收 `principal_id`，不再只接收
tenant。`UploadService` 与三条上传路由把接口层解析出的 principal 一路传下去。

**读和写是两条不同的规则**，这是本条的核心判断：

| 操作 | 谁可以 |
|---|---|
| 观察 / 传输 / 完成一个 upload | 声明它的那个 principal |
| 向已存在的文档提交新版本 | 文档 owner，**仅此** |
| 读文档、版本列表、授权名单 | owner **或** ACL 授权的 principal |

把 ACL 同时当作写授权，会让「授权某人查看」悄悄变成「授权某人覆盖」——那是没有
任何人打算给出的权限。所以 `_is_granted()` 只服务读路径，写路径只比 owner。

**授权检查与写在同一把锁下。** `_locked_document()` 现在返回整行（revision、
owner、knowledge base），检查放在 `FOR UPDATE` 之后：先检查再取锁的写法，会对着
一份可能已经不是被写对象的行做判断。条件插入那条竞态分支同样如此——检查放在
`ON CONFLICT DO NOTHING` **之后**，对最终握住的那一行做，否则输掉竞态反而成了
获得写权限的路径。

KB 不一致（既有文档属 KB-A 却提交 KB-B）用新的 `KnowledgeBaseMismatchError`
拒绝，映射为 409。它不是授权失败——调用方确实是 owner——而是一个与已提交事实
矛盾的断言；接受它会让 document 行停在 KB-A 而 outbox 事件告诉索引 KB-B。
沿用 `UploadVerificationError` 的既有做法复用 `invalid_tool_input` 错误码，
不新增领域词汇。

拒绝一律是 `NotFoundError`（404），与「不存在」和「别的 tenant 的」完全同形。

回归测试 17 条：

- `tests/api/test_upload_authorization.py`（10 条，HTTP 层）——**固定 tenant、
  只换 principal**，并且**故意把 upload id 和 document id 交给攻击者**，因为那
  正是要防的处境；id 会出现在日志、URL 和工单里，「难猜」不是授权规则。含 3 条
  对照（owner 仍可提交第二版、邻居可以拥有自己的文档、拒绝时不泄漏声明的文件名
  且必须同时断言状态码）。
- `tests/persistence/test_uploads_and_outbox.py`（7 条）——读规则没有 HTTP 面，
  只能在这一层测；外加一条并发创建竞态测试。

**验证过是有牙的**：撤掉 upload owner 检查失败 3 条，撤掉 document owner 检查
失败 3 条，撤掉 KB 检查失败 2 条，三者全撤失败 8/10（通过的 2 条正是对照组），
撤掉读授权失败 3 条。竞态那条单独验证：把写授权挪到条件插入**之前**，它连续
6 次全部失败。

原有的 IDOR 测试全部只换 tenant，因此这一整类缺陷从头到尾都是绿灯。审计原文
「现有 IDOR 测试只换 tenant」说的就是这件事。

**已知残留（不在本条范围）**：`document_id` 由调用方指定，因此邻居仍可通过
「用别人的 id 提交得到 404、用新 id 得到 201」区分出某个 document id 是否存在。
消除它需要改成服务端铸造 document id，属于 API 形状变更，不是授权变更。
P1-2（Artifact 对象授权）与 P1-3（相同内容重传忽略 ACL 撤销）仍然开着。

### P1-3 相同内容重传时的 ACL 调和（2026-07-26）

行为变化：digest 相同的那条提前返回分支现在先调和 ACL。授权集合有变化时，
原子地替换 ACL 行、推进 document revision、写一条 `acl_changed` outbox 事件，
然后仍然返回既有 version。

判断有两点：

**一、重传相同内容正是表达「同一份文档、换一批读者」的方式。** 此前这条路径直接
返回旧 version，ACL 行一次都没碰过，也不产生事件——于是「内容不变、撤销某人授权」
完全不生效，而且是静默的：索引会继续把文档答给一个 owner 已经取消授权的人，直到
有人碰巧上传了不同的字节为止。`acl_changed` 事件类型从 PR-013 起就定义在
`OutboxEventKind` 和数据库 CHECK 约束里，一直没有写入方——和 P0-2 的
`PermissionRequested` 是同一种形状。

**二、授权变更要占一个 revision。** 消费者按每文档一个单调计数器给事件排序；
如果 ACL 事件和内容事件可以共用同一个 revision，乱序到达就无法与重复到达区分。
代价是 version 行在 revision 空间里变稀疏（例如 `[1, 3]`，2 是一次授权变更）——
这是正确的：version 记录的是内容，而那次 revision 没有改变内容。

不写新 version 行：内容没变。比较的是集合而不是序列，所以重排授权列表不算变更。
内容与 ACL 都没变时仍然什么都不做，既有幂等性不受影响。

回归测试 7 条，全部在持久化层——撤销的效果目前没有 HTTP 面可观测（没有 GET
document 路由，artifact 下载的对象授权是 P1-2），所以这里不放形式上的 HTTP 测试。

**验证过是有牙的，而且是双向的**：撤掉调和失败 4 条（缺陷本身）；只撤掉「集合
未变则直接返回」那道早退失败 3 条——其中一条是原有的幂等测试。第二个方向是必需
的：没有它，「每次重传都推进 revision 并发事件」这种过度实现也能让前 4 条变绿。

新增一条不变量测试：ACL 变更占掉 revision 2 之后，下一次内容变更必须落在 3，
事件序列为 `document_upserted(1) → acl_changed(2) → document_upserted(3)`。
