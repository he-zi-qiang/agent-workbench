# WP15 让 Agent 真正能干活：工作区、沙箱、只读取用、够用的预算、第二张图

五份决策，一条实施顺序。**顺序是承重的**，不是偏好：

| 阶段 | 内容 | ADR | 为什么在这个位置 |
|---|---|---|---|
| **1** | 任务工作区 | [ADR-028](../adr/0028-task-workspace.md) | 后面每一阶段的输入输出都落在它上面 |
| **2** | 一次性沙箱 | [ADR-029](../adr/0029-ephemeral-sandbox.md) | 输入输出需要工作区 |
| **3** | 只读取用网页与下载 | [ADR-027](../adr/0027-read-outward-write-inward.md) | 下载的文件需要地方待 |
| **4** | 预算、`edit`、`grep` | [ADR-030](../adr/0030-working-nodes-are-governed-by-cost.md) | 要先有工具，才知道它们不够用在哪 |
| **5** | 第二张图 `v2_general` | [ADR-031](../adr/0031-a-second-graph.md) | 它的 `work` 节点要上面四样都在才有意义 |

ADR 编号按决定的时间排，不按实施顺序。ADR-027 先写完，但它排在第三。

**第 4 阶段不是可选的**。工具装齐而预算不变的话，一个真在迭代的节点会在 12 步上撞墙，
而 `max_steps` 的域上限 `le=100` 让你连配都配不上去——那时的症状是"功能都在，就是干不完
活"，很容易被误读成模型不行。

沿用既有规矩：**一个 PR 只做一件事**；测试先证明是**红**的再变绿；**没有对照组的测试
不算数**——只断言"这个被拒绝"的测试分不出一个正常工作的校验器和一个把什么都拒绝的
校验器。

## 0. 三条硬约束（整个 WP 都适用）

1. `SUPPORTED_KEYWORDS`（`runtime/schema_validation.py`）**一个字不加**；
2. 新配置字段必须登记进 `config/ownership.yaml`，它会**递归展开**嵌套模型；
3. 抬 `config_schema_version` 会让 `test_the_configuration_schema_version_is_pinned`
   失败——那是机制不是障碍，更新断言并在 docstring 版本串里补一行理由。

## 阶段 1：任务工作区

### PR-1.1：领域模型与清单

- `ArtifactKind` 增加 `"workspace"`；
- 新增 `WorkspaceManifest`：`{名字: ArtifactRef}` 的有界映射，本身序列化后存成一个
  artifact，它的 id 就是"工作区版本"；
- 名字类型：`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$`，**没有斜杠**（ADR-028 §3.3）；
- 上限：单文件字节、文件数、总字节，全部是常量不是配置。

**测试的牙**：带斜杠、带 `..`、空名字、超长名字各自被拒；**对照组**是形如
`draft-v2.md`、`data_1.csv` 的名字被接受。超界返回错误而不是截断——断言返回的是错误
类型，并**对照**限内的同一操作正常返回。

### PR-1.2：`TaskState.workspace_version` 与节点入口语义

- `TaskState` 增加 `workspace_version: Identifier | None`；
- 节点开始时解析出它入口那一版；节点成功结束时把新版本写进 checkpoint。

**测试的牙**（这个 PR 的核心）：
- 节点写了文件后崩溃并重放 → 重放看到的是**入口版本**，看不到上次未完成的写入；
- **对照组**：节点正常结束后的下一个节点**看得到**那些写入。
  少了对照组，一个"永远返回空工作区"的实现也全绿；
- 同名连写两次 → 得到两个版本，旧版本仍可解析。

### PR-1.3：三个工具与 profile 接线

`workspace_list` / `workspace_read` / `workspace_write`，原生工具**不走 MCP**
（ADR-028 §3.5：它要访问 artifact store 与 TaskState，那是 Worker 的内部权限）。

风险声明照 ADR-028 §3.5 填死；`workspace_write` **不带 `operation_key`**。
先只给 `writer/synthesize`。

**测试的牙**：
- writer 可见三个工具；framer / planner / 两个 researcher / critic **都不可见**；
- **对照组（防回归主力）**：不启用时六个 profile 的可见工具集合与今天逐字节相同；
- 跨 Task 读不到别的 Task 的名字（同 tenant 也不行）。

## 阶段 2：一次性沙箱

### PR-2.1：沙箱 MCP server（纯函数那一半）

新建 `src/agent_workbench/apps/sandbox_mcp/`，形状照抄 `apps/word_mcp/`。

`run_python(script, inputs) -> {stdout, stderr, exit_code, outputs}`，**不认识工作区、
租户与所有者**（ADR-029 §3.1）。输入 schema 只用那 17 个关键字。

隔离全部写死不做配置项：无网络、只读根、tmpfs 可写层、非 root、丢弃 capability、
禁止提权、无主机挂载、内存/CPU/墙钟/进程数上限。

**测试的牙 —— 这里必须是真验证，不是断言我们设了 flag**：
- 脚本尝试联网 → **失败**（对照组：纯计算脚本成功）；
- 脚本尝试写只读根 → 失败（对照组：写 tmpfs 成功）；
- 死循环 → 被墙钟杀掉，并返回结构化超时（对照组：快脚本正常返回）；
- 超大输出 → 结构化错误而**不是**截断（对照组：限内输出完整返回）；
- 两次调用之间无状态残留：第一次写一个文件，第二次断言它不在。

### PR-2.2：Task 侧工具与运行时探测

Task 侧工具负责：从工作区读输入 → 调沙箱 → 把输出写回工作区。存储访问留在 Worker。

Worker 启动时探测容器运行时；不可用则记结构化日志、**不注册这个工具、进程照常启动**
（对齐 ADR-025 对连不上的 server 的处理）。

**测试的牙**：运行时不可用 → 工具不在注册表且**进程启动成功**（对照组：可用时工具在）。

## 阶段 3：只读取用网页与下载

详见 [read-outward-plan.md](./read-outward-plan.md)。顺序不变，其中
**PR-1（SSRF 解析后校验）仍然必须最先做**——那一条与本文件的阶段划分无关，它是让模型
能自己命名 URL 的前提。

到这个阶段时工作区已经存在，所以 `download_document` 的产物直接写进工作区，而不是
悬在一个只有 artifact id 的地方。

## 阶段 4：够用的预算与够用的工具

### PR-4.1：预算的主约束换成成本与时限

- `RunBudget.max_steps` 的域上限放宽，角色改为兜底（ADR-030 §2.1）；
- 工作节点必须设 `max_cost_micro_usd` 与 `deadline`，两者今天默认是 `None`；
- 额度用尽的行为不变（ADR-022）：工具用尽收工具，其余用尽结束 run。

**测试的牙**：
- 一个便宜但步数多的 run 跑得完（对照组：同一个 run 在旧的 12 步预算下撞墙）；
- 一个贵的 run 被成本上限停住，`stop_reason` 是成本而不是步数；
- 一个慢的 run 被 deadline 停住。
  三条各配一条不触顶的对照，否则"永远停"的实现也全绿。

### PR-4.2：`workspace_edit`

```
workspace_edit(name, old_text, new_text) -> 新版本
```

**匹配数不是 1 就失败**，返回结构化错误（ADR-030 §2.3）。

**测试的牙**：
- 零处匹配 → 错误，且**文件未变**；
- 多处匹配 → 错误，且**文件未变**（这条最重要：静默改错地方不留痕迹）；
- **对照组**：恰好一处 → 改成功，产生新版本，旧版本仍可解析。

### PR-4.3：`workspace_grep`

结果条数、每行长度、扫描字节数都有界；正则带超时——pattern 来自模型，是不受信任的输入，
一个灾难性回溯的正则不该挂住 Worker。

**测试的牙**：命中/未命中成对；超界截断为"有界结果 + 明确的截断标记"而不是静默少给；
一个已知会灾难性回溯的 pattern 被超时中断而 Worker 存活。

## 阶段 5：第二张图

### PR-5.1：`v2_general` 的节点与边

`understand → work → review → export`。`research_graph.py` 与 `CANONICAL_V1_NODE_IDS`
**一个字不改**；v2 有自己的节点常量。`review → work` 的回边共用 `MAX_REVISIONS` 那道闸。

### PR-5.2：提交时选图并冻结

`POST /v1/tasks` 增加可选字段，默认 v1。图形态**提交时冻结并存进 Task**，与授权信封同理：
一个已经在跑的 Task 不因部署改了默认值而换图。**不做自动路由**（ADR-031 §2.3）。

**测试的牙**：
- 不传字段 → 与今天逐字节相同地走 v1（**防回归主力**）；
- 传 v2 → 走四节点图；
- 提交后改部署默认值 → 已存在的 Task 仍走它提交时那张图；
- v2 的 `work` 节点**拿不到**任何写外部世界的工具（ADR-027 那条线对两张图一样成立）。

### PR-5.3：横切的东西在两张图上都成立

恢复、审批、事件、预算是横切的，一张图漏掉很容易发生。`declared_nodes()` 那类结构测试
要对两张图都跑；导出审批、取消、租约恢复各补一条 v2 的用例。

## 4. 验收命令（每个 PR 都要过）

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/e2e
```

```bash
.venv/bin/python -m ruff check src/ tests/ scripts/ && .venv/bin/python -m ruff format --check src/ tests/ scripts/
```

```bash
.venv/bin/python -m pyright
```

带真实服务的全量（容器起在 5433 / 6333，跳过项应从 597 降到 11）：

```bash
AGENT_WORKBENCH_TEST_DSN="postgresql+asyncpg://agent:ci-only@127.0.0.1:5433/agent_workbench_test" AGENT_WORKBENCH_TEST_QDRANT_URL="http://127.0.0.1:6333" .venv/bin/python -m pytest tests/ -q --ignore=tests/e2e
```

两条环境事实，撞上了别当成 bug：

- 跑真实 Worker 需要 `embedding` extra。`uv sync` **不带** `--extra embedding` 会把它
  剪掉，症状是 Worker 拒绝启动并要求安装该 extra；
- `tests/vector/test_tied_score_order.py` 的 tie-break 用例在全量跑里偶发失败，单独跑
  稳定通过，与本 WP 无关，不要顺手改它。

## 5. 整个 WP 明确不做

- 填表、点击、下单、任何会改变对面状态的操作；
- 驱动桌面软件的界面；
- JS 渲染页面与截图（需要浏览器内核，ADR-027 §3.5）；
- 沙箱内联网（ADR-029 §3.2 —— 那是它保持纯函数的原因）；
- artifact GC。中止的节点留下的无引用字节是**已知遗留**，与 ADR-025 §2.7 记录的是同
  一类问题，等一个单独的工作包；
- 放宽 `retryable_effects`。
