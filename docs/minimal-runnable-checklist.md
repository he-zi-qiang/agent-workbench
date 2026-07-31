# 最小可运行清单（2026-07-31 起）

目标是**能演示的完整一圈**，不是补全所有能力。每条都写了完成条件与依赖关系；
标注 **实测** 的是本轮直接跑命令验证过的，其余是读代码得出的判断。

跑起来的方法见 [running-locally.md](./running-locally.md)。

---

## 0. 已经跑通（实测）

- [x] **上传 → 文档版本 → outbox → 真实 BGE-M3 → Qdrant**
      实测：`points 2 → 3`；alias `knowledge_active → knowledge_bge_m3_v1` 由摄取
      worker 启动时创建。
- [x] **Task 提交 → claim → LangGraph → 结算**
      实测：`succeeded`，时间线 `TaskSubmitted → TaskClaimed → TaskSucceeded`。
- [x] **dense 检索质量**
      实测：MRR 0.960 / recall@1 0.947 / recall@3 0.974 / 61ms，38 题 gold set。
- [x] **五条命令起全套 + 一条命令走查**（`scripts/dev.sh`、`scripts/smoke_local.py`）

当前 `--without-chat` 实际服务的路由：`health`、`uploads`、`artifacts`、`tasks`、
`approvals`。

---

## 1. 接上模型 —— 一步解锁五件事

> 这是**唯一的外部依赖**，也是性价比最高的一步。不需要改任何代码。

- [x] **1.1 接一个模型 Provider**（2026-07-31 实测，DeepSeek `deepseek-chat`）
      两条路都行：本地 OpenAI 兼容服务（Ollama / LM Studio，`base_url` 可配，
      DeepSeek 走 OpenAI 协议），或云端 DeepSeek key。环境变量写法见
      [running-locally.md](./running-locally.md#为什么没有-chat)。
      **实测**：模型 id 钉进 `config.local.toml`（公开目录名，不是密钥），key 只走
      环境变量。`scripts/dev.sh` 现在按 `AW_SECRETS__DEEPSEEK_API_KEY` 在不在自动
      选模式并打印选了哪个。真实问答：
      > 问"reciprocal rank fusion 在哪里运行"，答"inside the database rather than
      > in the application"，引用 `chk_57934ada…`，`withheld: false`。

  接上之后**同时**具备（都已实现且测试全绿，只是从没真实走查过）：

  - [x] **1.2 SSE 流式**（实测）：`RunStarted → ContextBuilt → ModelStarted →
        ModelCompleted → RunCompleted`，事件 id 是 `(stream_id, sequence)` cursor。
  - [~] **1.3 Agentic 检索**：**被 4.1 挡住**。模型确实自己发起了检索——事件里
        有 3 次 `ToolProposed`/`PermissionResolved`/`ToolStarted`——但 3 次全是
        `ToolFailed: knowledge_search exceeded its 30s timeout`，然后撞 `max_steps`。
        `sparse_enabled` 是 `Literal[True]`（混合检索是架构不变量，不是开关），
        所以在这台机器上绕不过去。**这条依赖 4.1，清单原来的排序是错的。**
  - [x] **1.4 可验证引用**（实测，并因此改了一处实现）：第一次真实问答返回
        `citations: []`，而答案里明明写着 `chk_5793…`——模型用的是**圆括号**，
        我的扫描要求**方括号**。分隔符从来不是信号，**id 的形状**才是；已改成
        裸写/方括号/圆括号/反引号都认，并补了一条以此为名的测试。
  - [x] **1.5 HITL 真实走查**：见第 3 节，已完成。

---

## 2. 不接模型也能看见检索

- [ ] **2.1 检索的产品出口**
      现在**没有** `/v1/search` 之类的路由——检索只能通过 Chat 或
      `scripts/run_rag_eval.py` 触发。于是不接模型时，索引进去的向量从产品接口上
      **看不见**：能上传、能看到 Qdrant 点数涨，却没法问一句"找什么"。
      **完成条件**：一个按 owner/tenant 授权的检索端点，返回 ContextPacket 与
      citations，并配 IDOR 矩阵（同 tenant / 跨 tenant 已知 ID / 跨 tenant 随机 ID）。
      **注意**：这是新增公开 API 面，不是诊断脚本——要按 Task/Approval 同样的
      授权标准做。

---

## 3. HITL 的真实走查

- [x] **3.1 让图真的停一次**（2026-07-31 实测，两条路径各走一次）
      本轮做的 HITL 全套——`interrupt()` 节点、`approvals` 账本、决定事务、
      `/v1/approvals` API、`NOTIFY task_ready`——**代码在、测试全绿、从没真实走查过**。
      原因：demo 图的 approval 节点**自己回答自己**（`approval_decision: "approved"`），
      不会 interrupt；真正的 interrupt 需要真实 handlers，而那需要模型（依赖 1.1）。
      **实测结果**：

      | 步骤 | 事实 |
      |---|---|
      | 图停住 | 100 秒后 `waiting_approval`，`lease_owner` 与 `lease_until` **都已清空** |
      | 怎么找到审批 | 时间线上的 `TaskApprovalRequested` 带 `approval_id`（无列举端点） |
      | IDOR | 同租户他人 404、跨租户 404，正文一致 |
      | approved | 决定后 `queued` + `resume_kind=approval` + `resume_approval_id` → 20 秒内 `succeeded` |
      | rejected | → `failed`，detail 正是 `a human rejected the approval required before export`，**export 没跑** |

      时间线（截断）：`TaskSubmitted → TaskClaimed → … → TaskApprovalRequested →
      TaskAwaitingApproval`。

---

## 4. 性能：sparse / hybrid 臂目前不可用

- [ ] **4.1 定位 sparse 编码的二十倍**
      **实测**：摄取每篇文档 1–2 分钟；hybrid 评测臂 58 分钟未跑完（dense 臂 2 分钟
      就完成了）。已测得 FlagEmbedding 自动选的 MPS 比 CPU 慢：
      短句 10.6s vs 3.0s、整块（~800 词）12.7s vs 7.4s / 4 条。
      **但量级对不上**：hybrid 臂约 48 次编码 × 3.2s ≈ 2.5 分钟，不是 58 分钟。
      根因**未定位**，不要当成"MPS 的锅"就结案。
      **2026-07-31 定位到了一半**：单条短查询的编码耗时——
      **dense 2.8s，sparse 28.8s**，两者并发 58s（它们互相争用，不是并行）。
      `knowledge_search` 的工具超时是 30s，所以 **agentic 路径直接被这一条挡死**（1.3）。
      不是设备参数的问题：显式传 `devices="cpu"` 仍要 10.8s，不传 13.0s。
      **仍未定位**：为什么单条短文本要十几秒——这已经不是"MPS 比 CPU 慢"能解释的量级。
      **影响面**：固定两步路径照常（它不受工具超时约束，只是每轮也要付这个时间），
      dense 臂评测不受影响。**这条现在是 1.3 的前置，优先级要提到最前面。**

---

## 5. 真用之前（不阻塞演示）

- [ ] **5.1 生产身份认证**
      现在是开发请求头（`x-tenant-id` / `x-principal-id`）+ 强制 loopback 绑定。
      `deployment_scope = "remote"` 会**拒绝启动**，这是对的。作品集演示够，真用不够。
- [ ] **5.2 `task_ready` 的监听端**
      发送端四处都在事务内做完了；没有监听者，Worker 仍靠轮询。与 SSE 的
      LISTEN/NOTIFY 是同一批工作。
- [ ] **5.3 `export_artifact`（WP10-07）**
      副作用协议（`tool_executions`、两段提交、重算授权、人工核对状态）已就位，
      但当前 build 里**没有任何工具带 operation key**——路修好了，上面还没有车。
- [ ] **5.4 两条 Chat 路径的对照评测**
      fixed 与 agentic 都在了，没有任何东西测过第二条到底买到了什么。

---

## 建议顺序（2026-07-31 订正）

**`4.1` → `1.3` → `2.1` → 第 5 节。**

原来的排序把 `4.1` 放在最后，理由是"性能问题不挡演示"。真跑一遍之后这句话是错的：
sparse 单次编码 28.8s 超过了 `knowledge_search` 的 30s 工具超时，**agentic 路径
（1.3）根本跑不完**，而混合检索是架构不变量、关不掉。所以 `4.1` 不是优化，是前置。

`1.1`、`1.2`、`1.4`、`1.5`、`3.1` 已在 2026-07-31 实测通过，不再排队。
