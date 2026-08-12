# 待办清单（2026-07-29 起）

分支 `pr-050-postgres-checkpointer`，基线提交 `180785d`。

本清单的**每一条**都注明了它的依据等级：

- **实测**：本次会话直接跑命令或读源码确认；
- **文档**：来自 [2026-07-29 核验报告](./repository-audit-2026-07-29.md)，**未**逐条复核。

动手前先重跑一遍门禁，确认基线没变（见 [status.md](../status.md) 顶部快照）：

```bash
export AGENT_WORKBENCH_TEST_DSN="postgresql+asyncpg://agent:ci-only@127.0.0.1:5433/agent_workbench_test"
export AGENT_WORKBENCH_TEST_QDRANT_URL="http://localhost:6333"
ruff format --check . && ruff check . && pyright && alembic heads && pytest -q
```

2026-07-29 实测基线：`1452 passed / 11 skipped`（真实服务）、
`1054 passed / 409 skipped`（无外部服务）、alembic 唯一 head `0016_task_principal_scopes`、
ruff/pyright 全过。

---

## 0. 先做：验证欠账（成本最低、价值最高）

新落地的协调路径**有通过的测试，但没有对照组**。本仓此前每个增量都做等价破坏验证，
正是它抓出了 `start_next` 重复派发、够不着的守卫、没被覆盖的 `pending_writes`、
和一条假的上限断言——协调路径恰恰是"写一条通过的测试最便宜、意义最小"的地方。

- [x] **0.1 对 lease/epoch fencing 做破坏验证**（2026-07-29 完成）
      **结果：9 处破坏，第一轮只抓住 4 处。** 补了 5 条测试后 8 处被抓住；
      第 9 处（`status = 'running'`）经查是**不可证伪**的——lease-lifecycle CHECK
      规定非 running 行的 lease 列必须为 NULL，而 fence 已经要求 owner 匹配且 deadline
      未过期，所以只有 running 行能满足。已写进测试文档，不当作漏洞。
- [x] **0.2 对 stale-lease reclaim / retry / dead-letter 做破坏验证**（2026-07-29 完成）
      **结果：11 处破坏，第一轮只抓住 4 处。** 补了 7 条测试后 6 处被抓住；
      余下 5 处是**成对冗余**——select 与 update 各带一份同样的条件，单独删一份不可观测；
      删掉**两份**（expiry）确实会红，那才是真正要守的性质。update 的 epoch/expiry
      条件因为 select 已持 `FOR UPDATE`、行在事务内不可能变，同样不可证伪。
      全部结论已写进 `reclaim_expired` 的注释。
- [x] **0.3 对 `SKIP LOCKED` claim 做并发对照**（2026-07-29 完成）
      **结果：7 处破坏，抓住 4 处。** 补了 2 条测试（可领取者中最老者优先、
      backoff 未到不可领取）。余下：update 的重复条件同 0.2 不可证伪；
      `skip_locked` 是**活性**而非正确性属性（去掉只是让第二个领取者阻塞而不是跳过），
      任何对结果的断言都看不见它——已如实记在 `claim_next` 注释里，不为它编造测试。
      claim 的排序 lead-in（`available_at` 在前）与基线写的 `priority DESC, created_at, id`
      不一致，两者都没有规定哪个是对的，因此只钉住二者一致的部分。
- [x] **0.4 订正"单 Worker / 多 Worker"的自相矛盾**（2026-07-29 完成）
      复核结论：`docs/status.md:462` 那句在**历史 PR-054 章节**里，按本文件自己的约定
      historical section 保留当时状态，**不是**活的矛盾。真正过期的是
      `domain/task_registry.py` 里"registry 的方法仅限单 Worker 所需"——
      `claim_next`/`heartbeat`/`reclaim_expired` 都已存在，已订正为逐条说明哪条边有 caller。

---

## 1. WP07 收尾

- [x] **1.1 WP07-04：Qdrant generation reservation**（2026-07-29 完成，迁移 `0017`）
      新建 `qdrant_index_generations`（reservation 需要有东西可 reserve；它的 backfill /
      readiness / retention 属 WP04-05，故意没造）。三列进 `task_runs`，**外键就是
      reservation**——有 Task 引用时该 generation 删不掉。submit 事务内先
      `SELECT ... FOR UPDATE` 锁住 generation 行、要求 `active`，否则整笔回滚由调用方
      重解析（reserve-or-retry）。
      **9 处破坏，8 处第一轮即被抓住**；第 9 处（"半个 reservation 可存"）是我把破坏写坏了
      ——只改了 metadata 里的约束名，数据库里的约束仍在。直接在库里 DROP 该 CHECK 复验，
      新增的 4 条参数化测试确实变红。
      **仍未做**：alias→collection 的**每次提交解析**仍依赖启动时
      `qdrant_startup` 校验的 alias↔collection 一致性，没有独立的 resolver 端口；
      "快照与三列不一致时 fail closed" 只覆盖了 (collection, version, generation) 三元组
      互相不匹配的情形，没覆盖快照 JSON 内部与三列不一致。
- [x] **1.2 WP07-08：终态释放 reservation + Task-aware safe GC**（2026-07-29 完成）
      新增 `IndexGenerationStore` 端口 + `PostgresIndexGenerationStore`：
      `retire` / `release` / `collect`。
      **顺序本身就是不变量**：只有 retired 且无引用才能删，只有**终态** Task 才能释放，
      所以任何调用顺序都不可能把索引从还要读它的 Task 脚下抽走。
      release 放弃的只是 **reservation**，"跑在哪个具体索引上"仍在 Task 自己的语义快照里，
      审计不依赖 generation 行存活。
      **8 处破坏全部被抓住**（第一轮 7 处；补的第 8 条是：`collect` 不先加锁时，
      同时在飞的提交会让它把 0 个持有者当成"可删"，然后被外键挡住——安全但报成约束违反
      而不是"还有 1 个 Task 持有"）。
      **仍未做**：WP05 的 outbox/reconciliation 与 retention 到期校验没有接进 `collect`
      （它们依赖尚不存在的 ingestion state），所以 `collect` 目前只保证
      "retired + 无 Task 引用"，不保证"retention 到期且无未完成 outbox"。
      **完成条件**：非终态 Task 引用的 generation 不可物理删除；终态后需同时满足
      WP05 的 retention/outbox 校验才可删；有恢复测试。
- [x] **1.3 Checkpoint retention / `adelete_thread`**（2026-07-29 完成）
      `adelete_thread` 在**一个事务**里删三张表，并且**owning Task 非终态时拒绝**——
      checkpoint 就是执行位置，为一个还能跑的 Task 删掉它不是保留策略，是把唯一可恢复的
      东西销毁，而且下一个 Worker 看到的会是"这个 Task 从来没开始过"，损失是隐形的。
      **孤儿 write 不需要单独规则**：它带着自己的 thread，随 thread 一起删，没有别的东西
      能让它落单（这一条有测试）。
      **7 处破坏，6 处被抓住。** 第 7 处（读 owning Task 时不加锁）不可达：终态在转换表里
      没有出边，读到终态就不可能再变活；已按前几轮同样的方式写进注释。
      **仍未做**：没有"按时间/容量触发"的保留策略与清理入口——只有这个可被调用的原语，
      谁在什么时候调用它属于运维面（与 1.2 的 `collect` 同样的边界）。

---

## 2. Task Mode 其余（文档）

- [x] **2.1 HITL Approval：两个原子边界与 graph interrupt 全部完成**
      （账本与决定事务 2026-07-29；interrupt 节点、Approval API、`NOTIFY task_ready`
      2026-07-30，四个提交 `7014046`/`33ebbbb`/`a257e45`/`25895ca`）

  **2026-07-30 补完的那一半**（下面第一段是 07-29 已有的记录，保留原文）：

  - **真正的 interrupt 节点**：`approval` 节点调用 `interrupt()` 停住，**从不信任
    resume payload**——LangGraph 恢复时整个 handler 重跑，节点用**自己开出的**
    `approval_id` 回查账本。伪造 approval_id 只能唤醒节点，读到的还是它自己那条；
    没有决定就失败关闭。`ApprovalResume` 只带 id 与 version、**不带裁决**。
  - **拒绝是图里的一条路径，但不是 export 那条**：`approval` 变成条件节点
    （与 `quality_gate` 并列），approved → export，rejected → 无后继 + 自己的终态
    失败原因。`TaskState` 因此新增 `approval_decision`（路由必须是 state 的纯函数），
    且与 `approval_id` **成对存在**——只有 id 是"走过闸门却没拿到答案"。
  - **Worker 打通第 5/6 分支**：inspect 报告 interrupt 上的 approval_id，Worker
    问账本要决定，再用 `Command(resume=...)` 续跑同一 thread。没有账本的 Worker
    **park 而不是猜**（并打 warning）。
  - **Approval API**：`GET /v1/approvals/{id}`、`POST /v1/approvals/{id}/decisions`。
    授权与 Task 读同一套：不存在与不属于你**同状态同正文**；`decide` **先鉴权再写**
    （账本虽然也会拒第二次决定，但第一次已经把别人的 Task 重排队了）。
    decided_by 取自认证身份，body 里写 `decided_by` 直接 422。
    发现路径是 Task timeline 上新增的 `TaskApprovalRequested` 事件——**没有**列举
    审批的端点，也不需要有。
  - **`NOTIFY task_ready`**：submit / 决定 / release_for_retry / reclaim 四处，
    都在各自事务内；payload 只有 `{"task_id": ...}`。dead_letter 与 claim **不**通知。

  **破坏验证**：四轮共 34 处，第一轮抓住 31 处。三处漏网及其处理：
  resume 的 `decision_version` 写错不影响任何结果（节点按设计不读 payload）——
  改为断言它**落进了 checkpoint 的 `__resume__` 写**，那是它唯一的可观测面；
  composition 掉了 interrupt 节点、Worker 掉了账本，都是**活性**而非安全性回归
  （router 已经失败关闭、Worker 会 park），补了 composition 断言；
  channel 改名没被抓住，因为测试用的是**和发送方同一个常量**——已改成订阅字面量
  `task_ready`，另有一条测试把常量钉死。补测后 34/34。

  **仍未做**（与 2.1 相邻但不属于它）：`task_ready` 的**监听端**没有写
  （Worker 仍靠轮询；属 3.5 的同一批工作），`tool_executions` 副作用 ledger 见 2.2。

  <details>
  <summary>2026-07-29 原始记录（账本与决定事务）</summary>

- [~] **2.1 HITL Approval：审批账本与决定事务已完成（2026-07-29，迁移 `0018`）**
      本轮做的是**第二个原子边界**（`approvals` 账本 + 决定→重排队），也就是有 barrier 要求
      的那一半。第一个边界（interrupt → `waiting_approval` + 清 lease）此前已由
      `registry.await_approval(lease)` 落地。
      新增：`approvals` 表（`UNIQUE(task_id, graph_node_operation_id)`、
      pending/decided 双向 CHECK）、`task_runs.resume_kind` / `resume_approval_id`、
      `TaskApprovalDecided` 事件、`ApprovalStore` 端口 + `PostgresApprovalStore`。
      决定是**一个事务**：记录决定 + `waiting_approval → queued` + 写 resume 引用 + 写 durable
      事件，四件事同生共死；`decision_version` 让同一决定重放只留一行、只重排队一次；
      approved 与 rejected **都**重排队（拒绝是图里的一条路径，不是没有路径）。
      **11 处破坏，4 处第一轮被抓住**，补了 1 条约束测试后 5 处；余下按性质分类并写进 adapter
      文档：`waiting_approval` 是**成对冗余**（删两处会红，删一处不会）；给 Task 行加锁**不改变
      任何结果**（requeue 的条件会重新求值），它存在只为固定跨表加锁顺序、避免死锁，而这一点
      任何对结果的断言都看不见；approval 更新上的 version fence 只在两个决定真并发时可达，
      需要方法内部的接缝才能确定性触发，因此**保留但未测**。
      **当时仍未做**：graph 里真正的 interrupt 节点与 `Command(resume=...)`、Approval HTTP API 与
      owner/tenant IDOR 测试、`NOTIFY task_ready`。`task_recovery.py` 的第 5/6 分支仍然是
      "决策已覆盖、无真实图"。**以上四项已于 2026-07-30 全部完成，见上。**

  </details>
- [x] **2.2 外部副作用 ledger（`tool_executions`）**（2026-07-31 完成，迁移 `0019`，
      两个提交 `3512f1d`/`08c76e9`）
      三件事各自落地并有测试：
      - **稳定 operation key**：业务 key 而非 `tool_call_id`（重试的模型轮次会铸新 call id，
        按它做键等于每次重试都是新操作）。`UNIQUE(task_id, operation_key)`；同 key 不同
        canonical 参数**冲突拒绝**，不覆盖第一条记录。
      - **intent/result 两段提交**：先记 intent 再 dispatch，中间**重算一次授权**
        （收紧要在下一个授权边界生效，对不可逆动作那个边界就是动作前一刻）。
        全部写入按 Task 活跃 lease 做栅栏。
      - **人工核对状态**：`needs_reconciliation`。判据是**有没有拿到答案**——handler 返回
        错误算知识，超时/取消/预算耗尽算无答案（外部写的"没答案"不等于"没发生"）。
      **破坏验证**：ledger 10 处（9 抓住，1 处 `status='running'` 因 lease-lifecycle
      CHECK 不可证伪，已写进注释）；gateway 10 处全抓住。其中发现**我自己一条测试守错了
      对象**：直接在库里 DROP 状态词表 CHECK 测试全绿，因为那行是被 settlement 约束拒的；
      已改为每个 case 点名它针对的约束。
      **不在本条范围**：`export_artifact`（唯一真实写节点，属 WP10-07），所以当前
      build 里还没有任何工具带 operation key——协议就位，路上还没有车。
- [ ] **2.3 真实外部搜索 Provider**：当前 Adapter 在 Provider 缺失时失败关闭（文档），
      行为正确但能力缺失。

---

## 3. Chat / RAG 欠项（文档，按价值排序）

- [x] **3.1 Agentic Retrieval 真正接通**（2026-07-31，提交 `a4a9afa`）
      三个完成条件都落地了，但**不是**靠把 `tool_names=()` 填上——那样等于把固定两步
      存在的理由花掉（模块注释原话：advertising a retrieval tool here would quietly
      turn this into the agentic path）。做法是**并存的第二条路径**：
      - **seam**：`TurnExecution`。turn 的生命周期（幂等 claim、lease、deadline、断连
        兜底、release fence）两条路径完全共用，只有"交给模型的请求 / 授权过的证据 /
        引用"三样不同。部署在配置里选：`chat.retrieval_shape = "fixed" | "agentic"`，
        **默认仍是 fixed**。
      - **注册并授权 Tool**：agentic envelope 点名 `knowledge_search`，风险上限保持
        deny-shaped 默认（检索是 read，本来就够）。**用权限写而不是用提示词写**，所以
        被检索到的段落说服模型想要别的工具也够不着。
      - **放宽步骤预算**：`max_agentic_steps` / `max_agentic_searches`，且在 settings
        里做跨字段校验（`RunBudget` 要求 tool_calls ≥ steps，不校验的话进程能起来、
        只在有人切换 shape 时才炸）。
      - **最终 evidence gate**：难点是"答案基于什么"。固定路径知道（一次检索一组
        revision）；agentic 的检索发生在模型循环里，而**引用是模型的说法不是记录**。
        所以工具把每次授权到的东西记进 `RetrievalJournal`（按 run 分键），执行结束
        取回。**按模型"看到的"而不是"引用的"设栅栏**——没点名的转述也是用过。
        journal 在 `finally` 里取，失败的 run 也不留残留。
      **破坏验证 10 处，第一轮只抓住 6 处**——漏的四处正是这条提交比看起来大的原因：
      我的测试替工具做了 journal，所以工具**完全不记**或**记错 run** 都能全绿；
      装配漏掉 journal 也没人发现；settings 那对预算没测。四条都补了，装配那条直接
      伸进 binding 查对象同一性而不是信 shape 的名字。补后 10/10。
      **仍未做**：两条路径的对照评测（capability vs determinism 的实测数字）。

- [x] **3.2 Hybrid Chat 装配 sparse encoder**（2026-07-31 复核：**条目本身已过期**）
      重读代码发现 API **早已**装配 sparse：`_assemble_chat` 调 `build_sparse_encoder`
      并传给 `RetrievalService`，后者在有 sparse 时走 `search_hybrid`（Qdrant Query API
      的一次 RRF）。应该是 `180785d` 那批整合带进来的，清单写于其前。
      **但有一个真实的洞**：所有装配测试都把 sparse 打桩成**不可用**，正例从没断言过——
      "有词法运行时却只接了 dense 臂"能通过全部现有测试，然后被当成 hybrid 去评测。
      补了两条正例（固定路径与 agentic 路径各一，后者顺带钉住**两条路径共用同一个
      retriever**——两个 retriever 就是两套授权检查，被忽视的那套就是会漏的那套）。
      破坏验证 2 处全抓住。
- [x] **3.3 可验证 Citation**（2026-07-31 完成）
      以前返回的是**检索包**的 citations，等于"找到了这些段落"被当成"答案用了这些段落"：
      用了一段的答案下面挂七个来源，一段没用的答案下面照样挂满。现在只在模型**点名了**
      且**被展示过**时才给出。
      两半都重要，第二半是**边界**而不是讲究：run 从没见过的 chunk id 是模型产出的字符串，
      原样回显等于让一个**猜出来的**标识（可能撞上这个提问者读不到的真实 chunk）带上本系统
      的权威。验证不通过的一律**丢弃**而非降级。
      连带**订正了一条不变量**：`ChatTurnResult` 原本要求 citations 的文档集合与
      authorized_revisions **相等**——那正是这条要拆掉的混同。改成**包含**：栅栏可以更宽
      （读了没引用的段落其权限仍须成立），但引用不能落在栅栏外（那是没人复核过的来源）。
      正则**从铸造 chunk id 的那一处取形状**而不是自己抄一份，否则 `[redacted]` 这种普通
      行文会被报成伪造来源、把真正的信号淹掉。
      **7 处破坏，6 处第一轮抓住**；漏的第 7 处恰是我刚放宽的那条不变量剩下的那一半
      （引用落在栅栏外可存），已补两条 contract 测试（含反向的"栅栏更宽可以"）。补后 7/7。
      **副作用**：不按 `[chunk_id]` 约定作答的模型会得到"零引用"。这是**如实**而非虚报，
      且在评测里可见——比原来那种稳定虚报好。
- [ ] **3.4 历史 token window / compaction**：只有状态和 compact profile，无 ContextEngine。
- [ ] **3.5 SSE 换成 LISTEN/NOTIFY 唤醒**：事实仍从表按 cursor 读，只把轮询换成唤醒。
- [ ] **3.6 EventLog upcaster / 毒行隔离**：现在能拒绝未知版本，不能升级或跳过。
- [ ] **3.7 旧 Qdrant Point 物理清理**：revision 栅栏挡住读取，Point 仍留着。

---

## 4. 作品集与运维（文档，能力缺口而非缺陷）

- [ ] 4.1 OpenTelemetry（只有配置字段，无 SDK/exporter/埋点）
- [ ] 4.2 Langfuse Adapter（只有 optional profile 配置）
- [ ] 4.3 CrewAI 对照实验 + 报告（只有配置约束）
- [ ] 4.4 LlamaIndex ingestion/query Adapter（**检索侧 2026-08-03 完成**，ingestion 未做）
      依赖、`adapters/llama_index/` 四个模块、按 `CandidateRetrieverPort` 参数化的契约
      测试、同索引同 gold set 的等价评测均已落地，`rag.llama_index.enabled` 第一次有
      消费者。**ingestion 仍未迁移**：没有 `IngestionPipeline`，VectorStore 适配器的
      `add`/`delete` 明确拒绝——一条没有对照基准的第二写入路径正是 ADR-017 第 3 条要防
      的。这一条要打勾，得先有 ingestion 迁移和它自己的对照证据。
- [ ] 4.5 RAGAS judge pipeline（现在只有确定性 IR 评测）
- [ ] 4.6 Task/Multi-Agent benchmark：配置指向的 `evals/tasks/cases.yaml` 不存在
- [ ] 4.7 Claude/Anthropic Provider Adapter（Port 已 provider-neutral）
- [ ] 4.8 Web UI / 最小控制台
- [ ] 4.9 生产身份认证（现在只有开发请求头 Identity + 强制 loopback）
- [ ] 4.10 自研 supervisor + workers + mailbox（现在是固定图的并行研究节点，
      不是动态 supervisor/worker；语义已锁定，实现属 Optional Lab）

---

## 5. 过程性（非代码）

- [ ] **5.1 决定 commit 叙事怎么办**（实测：`180785d` 是 120 文件 / +12039 行、
      标题一行、无正文；其余提交都是"一提交一变化 + 长正文说明为什么"）
      这个仓库对外的价值是**把推理过程留在可读的地方**，`git log` 是作品的一部分。
      要么后续恢复原节奏，要么明确改约定——但不要靠默认漂移。
      注意不要为此重写已推送的历史。
- [ ] **5.2 本清单落地后删除或归档**，避免它变成第二份和 status.md 打架的事实源。

---

## 动手顺序建议

`0.1–0.4` → `1.1` → `1.2`／`1.3` → `2.1` → `3.1`。

**2026-07-29 进度：第 0、1 组全部完成；2.1 完成了审批账本与决定事务这一半。**

**2026-07-30 进度：2.1 全部完成**（interrupt 节点、Worker 第 5/6 分支、Approval API
与 IDOR 矩阵、`NOTIFY task_ready`）。门禁：ruff / pyright 全过、alembic 唯一 head
`0018_approvals`、`1569 passed / 11 skipped`（真实 PostgreSQL + Qdrant）。
下一条是 `2.2`（`tool_executions` 副作用 ledger）或 `3.1`（Agentic Retrieval 接通）。

第 0 组先做的理由：它**不改行为**，只把"测试通过"变成"测试有牙"，而且它可能
直接改写第 1、2 组的优先级——如果 fencing 有洞，那比补 reservation 紧急得多。
