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

- [ ] **1.1 接一个模型 Provider**
      两条路都行：本地 OpenAI 兼容服务（Ollama / LM Studio，`base_url` 可配，
      DeepSeek 走 OpenAI 协议），或云端 DeepSeek key。环境变量写法见
      [running-locally.md](./running-locally.md#为什么没有-chat)。
      **完成条件**：去掉 `--without-chat` 后 API 正常启动，`/v1/chat/sessions`
      能回答一个问题并带回引用。
      **未实测**——本轮没有 key，也没起本地推理服务。

  接上之后**同时**具备（都已实现且测试全绿，只是从没真实走查过）：

  - [ ] **1.2 SSE 流式**：`events` 路由与 chat 绑在同一个 `serves_chat` 开关上。
  - [ ] **1.3 Agentic 检索**：`chat.retrieval_shape = "agentic"`，模型自己决定何时检索。
  - [ ] **1.4 可验证引用**：只在模型点名且被展示过时才给引用。
  - [ ] **1.5 HITL 真实走查**：见第 3 节。

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

- [ ] **3.1 让图真的停一次**
      本轮做的 HITL 全套——`interrupt()` 节点、`approvals` 账本、决定事务、
      `/v1/approvals` API、`NOTIFY task_ready`——**代码在、测试全绿、从没真实走查过**。
      原因：demo 图的 approval 节点**自己回答自己**（`approval_decision: "approved"`），
      不会 interrupt；真正的 interrupt 需要真实 handlers，而那需要模型（依赖 1.1）。
      **完成条件**：提交一个 Task → 图停在 approval → `task_runs` 变
      `waiting_approval` 且 lease 已清 → `POST /v1/approvals/{id}/decisions` →
      另一个 Worker 接手跑完。approved 与 rejected 各走一次。

---

## 4. 性能：sparse / hybrid 臂目前不可用

- [ ] **4.1 定位 sparse 编码的二十倍**
      **实测**：摄取每篇文档 1–2 分钟；hybrid 评测臂 58 分钟未跑完（dense 臂 2 分钟
      就完成了）。已测得 FlagEmbedding 自动选的 MPS 比 CPU 慢：
      短句 10.6s vs 3.0s、整块（~800 词）12.7s vs 7.4s / 4 条。
      **但量级对不上**：hybrid 臂约 48 次编码 × 3.2s ≈ 2.5 分钟，不是 58 分钟。
      根因**未定位**，不要当成"MPS 的锅"就结案。
      **下一步**：在 `_measure` 里把索引 / 编码 / 检索三段分别计时，直接指出那
      二十倍在哪一段。
      **影响面**：dense 臂不受影响，第 0 节三条链路照常。

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

## 建议顺序

`1.1` → （顺带验 `1.2`–`1.5` 与 `3.1`）→ `2.1` → `4.1` → 第 5 节。

理由：`1.1` 一步解锁五件事且不用改代码；`2.1` 是当前最违和的产品缺口；`4.1` 是
真实性能问题但不挡演示；第 5 节都是"真用"而非"能跑"的门槛。
