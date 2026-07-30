# 待办清单（2026-07-29 起）

分支 `pr-050-postgres-checkpointer`，基线提交 `180785d`。

本清单的**每一条**都注明了它的依据等级：

- **实测**：本次会话直接跑命令或读源码确认；
- **文档**：来自 [2026-07-29 核验报告](./repository-audit-2026-07-29.md)，**未**逐条复核。

动手前先重跑一遍门禁，确认基线没变（见 [status.md](./status.md) 顶部快照）：

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
- [ ] **1.2 WP07-08：终态释放 reservation + Task-aware safe GC**（依赖 1.1）
      **完成条件**：非终态 Task 引用的 generation 不可物理删除；终态后需同时满足
      WP05 的 retention/outbox 校验才可删；有恢复测试。
- [ ] **1.3 Checkpoint retention / `adelete_thread`**（实测：`adelete_thread`
      在整个代码库里**不存在**，只有同步 `delete_thread` 的显式拒绝）
      **完成条件**：三张 checkpoint 表在一个事务里按 thread 删除；保留策略；
      孤儿 write 行（无对应 checkpoint，正常存在，见 `0010` 迁移注释）的清理规则。

---

## 2. Task Mode 其余（文档）

- [ ] **2.1 HITL Approval**：`approval` 仍是无副作用占位节点。
      **完成条件**：ApprovalStore/API、graph interrupt、
      `running → waiting_approval` 与 `waiting_approval → queued` 的原子转换、
      按 `approval_id + decision_version` 幂等、cancel 与 approve 并发只有一个合法转换（barrier 测试）。
      注意：`application/task_recovery.py` 的第 5/6 分支**已经写好在等这个**
      （当时明确记录为"M3a 产生不出来的图"），落地后要把它们从"决策已覆盖、无真实图"
      变成有真实图的测试。
- [ ] **2.2 外部副作用 ledger（`tool_executions`）**：稳定 operation key、
      intent/result 两段提交、人工核对状态。
- [ ] **2.3 真实外部搜索 Provider**：当前 Adapter 在 Provider 缺失时失败关闭（文档），
      行为正确但能力缺失。

---

## 3. Chat / RAG 欠项（文档，按价值排序）

- [ ] **3.1 Agentic Retrieval 真正接通**：`knowledge_search` 存在，但 API 的
      Tool Registry 为空、Chat 的 `tool_names=()`。
      **完成条件**：注册并授权 Tool、放宽步骤预算、最终 evidence gate。
- [ ] **3.2 Hybrid Chat 装配 sparse encoder**：组件齐全，API 只装了 dense。
- [ ] **3.3 可验证 Citation**：现在返回检索包的 citations，未验证模型是否真的用了。
- [ ] **3.4 历史 token window / compaction**：只有状态和 compact profile，无 ContextEngine。
- [ ] **3.5 SSE 换成 LISTEN/NOTIFY 唤醒**：事实仍从表按 cursor 读，只把轮询换成唤醒。
- [ ] **3.6 EventLog upcaster / 毒行隔离**：现在能拒绝未知版本，不能升级或跳过。
- [ ] **3.7 旧 Qdrant Point 物理清理**：revision 栅栏挡住读取，Point 仍留着。

---

## 4. 作品集与运维（文档，能力缺口而非缺陷）

- [ ] 4.1 OpenTelemetry（只有配置字段，无 SDK/exporter/埋点）
- [ ] 4.2 Langfuse Adapter（只有 optional profile 配置）
- [ ] 4.3 CrewAI 对照实验 + 报告（只有配置约束）
- [ ] 4.4 LlamaIndex ingestion/query Adapter（无依赖、无 Adapter、无测试）
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

第 0 组先做的理由：它**不改行为**，只把"测试通过"变成"测试有牙"，而且它可能
直接改写第 1、2 组的优先级——如果 fencing 有洞，那比补 reservation 紧急得多。
