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
`approvals`、`search`（有检索栈时）。加 `--web-dir ./web` 再挂一个同源控制台。

- [x] **浏览器控制台**（2026-08-01 实测）Chat / Work / Search / Approvals 四页，
      同源挂在 `/ui`，无构建步骤、无外部依赖。Chat 与 Work 是**转录流**：节点一行
      一个、工具失败缩进在下面、审批就地给按钮。事件流用 `fetch` 读——`EventSource`
      设不了身份头，根本没法认证。见 [running-locally.md](./running-locally.md#浏览器控制台)。

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

## 2. 不接模型也能看见检索（已完成）

- [x] **2.1 检索的产品出口**（2026-07-31 完成并实测）
      新增 `POST /v1/search`，返回检索包（hits + citations + 用了哪个 retriever）。
      **真正的改动不在路由，在装配顺序**：模型原本在检索**之前**构建，所以没有
      provider key 时 `_assemble_chat` 早早返回，检索也一起没了——能索引、看得到
      点数涨，却没有任何产品接口能看它。现在模型放到最后，**缺 provider 只损失
      chat**。`build_model` 仍然拒绝返回不可用的东西（那个拒绝是对的），变的只是
      它带走多少。
      **授权**：这里没有 id 可以探——调用方只能以自己的身份检索，能读什么由
      PostgreSQL 的 ACL 决定。`knowledge_base_id` 只缩小范围、不授权，读不到就返回
      空而不是报错（"那里没有"和"对你没有"必须是同一个答案）。
      **实测**（进程里**完全没有配 provider**）：

      | | 结果 |
      |---|---|
      | 启动模式 | `no AW_SECRETS__DEEPSEEK_API_KEY: search without chat` |
      | retriever | `hybrid+rerank` |
      | `user_local` | 3 hits / 3 citations，响应里没有 tenant |
      | `user_other` 同一条查询 | **0 hits / 0 citations** |

      破坏验证 6 处，第一轮抓住 4 处。补了"无检索时不挂载路由"（404 而不是每次
      请求 409）后 5 处；余下 1 处**不可证伪**：从 body 取 principal 这件事，因为
      `extra="forbid"` 让那些字段根本进不来——成对冗余，删这一半不可观测，删 schema
      那一半会红。已按仓库惯例写进路由注释。

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
- [x] **5.3 `export_artifact`（WP10-07）**（2026-08-01 实测，全链路）
      此前副作用协议就位但**没有任何工具带 operation key**——路修好了，上面没有车。
      现在 `export` 是真的写节点，走同一条 gateway（schema → policy → 超时 → 审计
      事件 → ledger）。operation key 是 `export:{task_id}`，draft 放进**参数**，
      所以改过的 draft 会撞上同一个 key 被拒，而不是拿到新 key 悄悄导出第二份。
      **实测**（真实 DeepSeek + 真实索引，提交 → 图跑完 → 人批准 → 恢复 → 导出）：

      | | 结果 |
      |---|---|
      | Task | `succeeded` |
      | ledger 行 | `export:task_61b9…` / `export_artifact` / `succeeded` |
      | `outcome_detail` | `art_44a9f044…`（产物 id，恢复路径读它） |
      | `lease_epoch` | `2`（写入被 fence 住） |
      | 报告 | 3109 字节，头部含 task / approval / draft 三个 id，正文是模型综述 |

      顺带查出并修掉的三件读代码看不出的事：**每个 Task 的 envelope 此前允许零个
      工具**（实测 `permits() → False`，也就是 Task 里从来没有任何工具真跑过）；
      **lease epoch 从没进入 ExecutionContext**，ledger 没有东西可 fence；
      **gateway 成功时记 `detail=None`**，崩在结算与 checkpoint 之间就再也找不到
      产出物。授权上限的决定见 [ADR-015](./adr/0015-export-authorization.md)。
- [x] **5.4 两条 Chat 路径的对照评测**（2026-08-01 完成，见
      [评测报告](../evals/chat/REPORT.md)）
      跑这条测出来的头两件事都不是数字，是缺陷：

      **一、agentic 路径此前从来没有检索到过任何东西。** `knowledge_search` 把
      `knowledge_base_id` 列为 required 并从**模型的参数**里取，而系统提示、user
      message、装配三处都没告诉过模型这个 id 是什么。实测模型每次都编了
      `"default"`，语料在 `kb_eval`，于是每次检索都返回「no readable passages
      matched」**而且状态是 ok**——因为「那里没有」和「对你没有」是同一个答案。它
      看起来像搜过但没搜到的模型。**已修**（提示里点名本轮 kb，+2 条测试）；这也
      修正了 1.3 的归因，那一轮看到的 30s 超时只是同一条断链的另一种表现。

      **二、`knowledge_search` 返回不了正常大小的结果**（**已修**）。渲染进
      `ToolResult.content`，而它是 `BoundedText(4096)`——`BoundedText` 其余每一处
      都是**模型写出来的话**，工具结果是**喂给模型的输入**，两者的自然尺寸不同。
      本项目 512 token 分块 + 工具默认 `top_k=8` → 16,732 字符，超限不截断而是
      **整次调用失败**。修法：`ToolOutputText`(65,536) 作兜底，
      `MAX_CONTENT_CHARS`(48,000) 作 `knowledge_search` 自己的预算，超限**整段丢弃
      不截断**（半段证据说的是文档没说的话，而引用栅栏只校验 chunk 被展示过），丢了
      几段写进 `note`。`top_k=20` 满配 41,822 字符现在能返回。破坏验证 8/8。
      **数字尚未按修复后重跑。**

      **数字**（修完一、带着二；hybrid，无 rerank，13 题）：

      | | fixed | agentic |
      |---|---|---|
      | 完整作答 | **11/11** | 9/11 |
      | fact recall | **1.000** | 0.818 |
      | citation recall | **0.955** | 0.818 |
      | 编造引用 | 1 | **0** |
      | 平均 token | **472** | **3,247（6.9×）** |

      **agentic 没买到准确率，买到了 6.9 倍 token。** 唯一测得出的优势是 compound
      题的 citation recall（1.000 vs 0.875）——而两条路的 fact recall 都是 1.000，
      固定路径只是少引一篇。agentic 输掉的两题死于缺陷二，不算在形态头上。
      样本只有 13 题，每个差值都在一两题能翻盘的范围内。

---

## 建议顺序（2026-07-31 订正）

**第 5 节。`1.3` 不再排队——它等的是硬件，不是代码。**

第四版。`4.1` 的预热修掉了 1.3 的一半原因，二次复验暴露了另一半：这台 8 GB
机器装不下这套检索栈（见 4.2）。`1.1`、`1.2`、`1.4`、`1.5`、`2.1`、`3.1`、`4.1`、`4.2`
已在 2026-07-31 实测通过。

**这台 8 GB 机器上的操作提示**：一次只跑一个会检索的进程。要跑评测就先停掉
API 与摄取 worker。固定两步 Chat 能用（一次检索）；agentic 不行（工具超时 30s）。
