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
  - [~] **1.3 Agentic 检索**：**代码就绪，这台机器跑不了**（2026-07-31 二次复验）
        模型确实自己发起检索——事件里 4 次 `ToolProposed`/`PermissionResolved`/
        `ToolStarted`——但 4 次全是 `ToolFailed: knowledge_search exceeded its 30s
        timeout`。预热修好之后**仍然如此**，所以 4.1 的预热不是全部原因。
        实测单次 `retrieve()`（预热之后，同一进程连续三次）：

        | 配置 | 第 1 次 | 第 2 次 | 第 3 次 |
        |---|---|---|---|
        | dense + sparse + reranker | 68.8s | 74.5s | **82.0s** |
        | dense + sparse（去掉 reranker） | 18.2s | 28.8s | **44.1s** |

        **单调变慢**，且三次查询期间 swap 从 3.5 GB 涨到 4.9 GB——是换页不是计算。
        原因见 4.2：这台 8 GB 机器装不下这套检索栈。
        **不是代码缺陷**：换一台内存够的机器应当直接可用，不需要改代码。

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

## 4. 性能与容量

- [x] **4.1 定位 sparse 编码的十几秒**（2026-07-31 查清，两个原因，都不是"sparse 慢"）

      **原因一：一次性的 MPS kernel 预热。** 同一个模型对象连续编码同一条短查询：

      | 设备 | 构造 | 第 1 次 | 第 2–4 次 |
      |---|---|---|---|
      | mps | 4.6s | **29.42s** | **0.06s** |
      | cpu | 4.4s | 2.84s | 0.12s |

      500 倍差距全在第一次。**MPS 预热后反而比 CPU 快一倍**，所以此前"MPS 比 CPU 慢"
      的结论是拿冷启动比温启动，**已作废**。
      这一次就是 1.3 挂掉的全部原因：`knowledge_search` 工具超时 30s，而进程里
      **第一次**检索要 29.4s。第二次只要 0.06s——但那时 run 已经失败了。
      **已修**：`bootstrap/encoder_warmup.py`，在 `ApiDependencies.startup()` 里
      预热。实测请求路径 **29s → 0.32s**。故意不致命：预热失败只记 warning，
      因为拿"慢一点的首次请求"换"进程起不来"是笔坏买卖。
      破坏验证 4 处，第一轮抓住 2 处；漏的两处（startup 不调用、装配不交出 encoder）
      各补了一条断言对象同一性的测试，补后 4/4。

      **原因二：这台机器只有 8 GB 物理内存。** 评测那 58 分钟不是计算，是换页——
      当时 API 与摄取 worker 也在跑，**每个进程各自加载一份 BGE-M3**，实测
      `swapusage used = 21.9 GB`，三个进程的 RSS 被换出到接近 0。
      单独跑评测仍然慢：评测进程自己就同时持有 BGE-M3 与 bge-reranker-v2-m3
      （合计约 4.5 GB 权重），8 GB 机器装不下。
      **不是代码缺陷，是容量约束。** 但可以改进：评测按需加载 reranker（只有
      rerank 臂用得到）、臂与臂之间释放；以及**不要同时跑多个持有模型的进程**。

- [x] **4.2 这套检索栈的内存下限**（2026-07-31 实测，**新发现**）
      架构把混合检索与重排都定成了不变量——`embedding.sparse_enabled` 与
      `reranker.enabled` 都是 `Literal[True]`，**配置关不掉**（这是有意的，见
      ADR-013）。后果是：**每一个会检索的进程都要同时加载三个模型**——
      dense BGE-M3、sparse BGE-M3（FlagEmbedding 另一份）、bge-reranker-v2-m3，
      合计约 6.7 GB 权重。
      这台机器 8 GB。实测后果见 1.3 的表：即使去掉 reranker，检索也从 18s 退化到
      44s，swap 持续增长。
      **这不是缺陷，是没有写下来的部署下限。** 应当写进部署文档：
      **一个检索进程需要约 12 GB 可用内存**（6.7 GB 权重 + torch 运行时 + 余量），
      并且**不要在同一台机器上跑多个检索进程**（API、摄取 worker、评测脚本各自
      加载一份）。
      **可做的改进**（都不违反不变量）：三个模型共享同一份 BGE-M3 权重（dense 与
      sparse 现在各加载一份，这一份是纯浪费）；reranker 按需加载。

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

**`2.1` → 第 5 节。`1.3` 不再排队——它等的是硬件，不是代码。**

第四版。`4.1` 的预热修掉了 1.3 的一半原因，二次复验暴露了另一半：这台 8 GB
机器装不下这套检索栈（见 4.2）。`1.1`、`1.2`、`1.4`、`1.5`、`3.1`、`4.1`、`4.2`
已在 2026-07-31 实测通过。

**这台 8 GB 机器上的操作提示**：一次只跑一个会检索的进程。要跑评测就先停掉
API 与摄取 worker。固定两步 Chat 能用（一次检索）；agentic 不行（工具超时 30s）。
