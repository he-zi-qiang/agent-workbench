# 十分钟版本

[README](../README.md) 讲这个项目**是什么、怎么建的**。这一页讲**它的成色**：
有没有真跑起来过、数字是什么、做对了哪几个判断、以及哪些明确没做。

每条陈述都能在仓库里找到出处，本页不引入任何新主张。

---

## 1. 它真的跑起来过

下面是 2026-08-10 在本机跑的一个真实任务（`task_43dc512e…`）留下的事件流，
**逐条来自 PostgreSQL 里的持久事件，不是示意图**：

```text
TaskSubmitted → TaskClaimed
  ToolProposed   external_search        → PermissionResolved  outside_submitted_envelope   ← 授权信封拒绝
  ToolProposed   mcp_web_fetch_page     → PermissionResolved  within_submitted_envelope
  ToolStarted    mcp_web_fetch_page     → ToolCompleted                                    ← 真的读了那一页
  ToolProposed   workspace_list/write   → PermissionResolved  within_submitted_envelope
  ToolStarted    workspace_write        → ToolCompleted                                    ← 产物落进任务工作区
waiting_approval                                                                           ← 停下来等人
  ApprovalDecided approved
TaskClaimed (epoch 2)                                                                      ← 换 epoch 重新领取，从 checkpoint 续跑
  ToolProposed   export_artifact        → ToolStarted → ToolCompleted
TaskSucceeded
```

一条链路同时演示五件事：**只读取用外部世界**、**任务工作区**、**外部研究节点是一个
Agent**、**导出必须由人批准**、**跨进程恢复**。被拒的那次调用**也留痕**，而不是消失。
产出的报告里带着审批 id 与草稿 artifact id，可以从报告倒查回是谁批准的。

另一条产品线是 Chat。问一个语料覆盖得到的问题，它给出答案与 5 条引用，每条带
`chunk_id`、`document_id` 和 `document_version`。问一个语料答不上来的问题，
它**明说答不上来**：

> The evidence does not answer the question. … I cannot infer the knowledge base's
> topic from this evidence.

这比给出一个像模像样的答案更难做到，也更值得看。

第二次真实端到端在 2026-08-11（`task_3ae4d5a0…`，`v2_general` 图）：
`understand → work → review → export → succeeded`，产出可下载可预览的 `.docx`。
**跑通它的代价是修掉四个各自独立的缺陷**，其中两个会静默地让正确的行为失败。

---

## 2. 门禁与规模

<!-- 维护规则：这组数字的事实来源就是本节。唯一的镜像是 README.en.md 的对应表
     （译本没法靠链接绕开需要数值）。改这里，同一个提交里镜像英文版。
     其余文档一律链接本节，不复述具体数值。 -->

**本节是这组数字的事实来源**，其余文档链接本节而不复述——同一组数字存在多处，
就一定有一处先烂掉，而它们看起来一样可信。唯一的例外是 [README.en.md](../README.en.md)
的英文镜像表。

四行来自四种环境，**只能分别引用，不能相加**——两个后端环境的跳过集互相覆盖。
四行实测于 **2026-08-31** 的 `main`（第五十六批之后）。同日量过三组，差额本身有意义：

| 时点 | 真实服务 | 离线 | 五目录 | 前端 |
|---|---|---|---|---|
| `a3619f9`（收尾扫描之前） | 3981 | 3193 | 1376 | 826 |
| 第五十五批（ADR-097 接线） | 3989 | 3201 | 1376 | 826 |
| 第五十六批（C-05 诊断） | 3993 | 3205 | 1376 | 828 |
| 第五十七批（边界收窄测试，本表） | 3993 | **3207** | **1376** | **828** |

后端两次各 +8 与 +4，正是 `test_candidate_funnel.py` 与 `test_task_failure_detail.py`
新增的条数；前端 +2 同理。**五目录那一行两次都没变**，因为两批新增的测试都不在那五个
目录里——它们通过与否由全套那一行覆盖。

这个说明记的是"测量时那棵树"，不是"当前基线"永远成立。**上一版这句话的位置写着
"`main` 本身"，而它在被写下的同一天就不再成立了**——这不是笔误，是本节反复处理的那件事
的又一次：数字与它的出处一起过期，而只有出处过期时更难看出来。

| 环境 | 结果 |
|---|---|
| 后端，真实 PostgreSQL + Qdrant（本机） | `3993 passed / 12 skipped` |
| 后端，不起任何外部服务（本机） | `3207 passed / 800 skipped` |
| 后端，CI 那组服务型目录（`contracts`/`persistence`/`api`/`vector`/`e2e`） | `1376 passed / 2 skipped` |
| 前端 Vitest（本机 52 个文件） | `828 passed` |

**第一行此前记着 3 项失败，现在没有了，而消失的原因值得写下来。** 那三条都在
`tests/e2e/test_worker_process_crash_recovery.py`，症状是 v1 图的 `approval` 节点一次
都没跑（`approval ran 0 times`）而 Task 仍然成功。2026-08-29 复测：整份文件 **11 条
全过**。

它们是被**修好**的，不是被环境糊过去的，而修法在 2026-08-28 的
`174f1f2`：`config/config.test.toml` 没有声明 `[workflow]` 段，`export_requires_approval`
落回出厂的 `false`，于是这些 Worker 起在一个**没有审批门**的部署下——`approval` 一次都不会
跑，而这个文件的每一条断言都在数它。修法是一行：给子进程环境补上
`AW_WORKFLOW__EXPORT_REQUIRES_APPROVAL=true`。那个文件的全部主题就是 v1 图跨过它的人工门，
所以它必须把自己要测的那个部署配出来，而不是继承一个恰好没有门的默认。

**为什么能红着两周**：`tests/e2e` 当时不在任何一个 CI job 里，所以旧条目里那段解释既没被
证实也没被证伪。这一条也已经修了——`014de9e`（同日）把 `tests/e2e` 加进了服务型 job，它
现在跑五个目录，上表第三行也是按五个目录量的。

**2026-08-29 补记，因为它是这份文档自己的题目。** 本节此前的这一段把原因写成了「当年那次
是在没设 `AGENT_WORKBENCH_TEST_DSN` 的 shell 里跑的」，还写着「`tests/e2e` 仍然不在 CI 的
服务型 job 里」。两句都是假的，而且是在真实原因已经被查实并修好的**第二天**写下的：不设
DSN 时那些用例是 skip 而不是 fail（本节第二行 787 个 skip 里就有它们），所以它们当年是
**带着服务**红的。写这一段的人没有去读那次修复的提交，就照着一条过期的印象补了一个听起来
合理的机制——正是本文档反复在处理的那一类错误。

**第四行是本机数字，不是 CI 数字。** 更早的表里前端那一行写的是 CI 数字，理由是本机装不到
`engines` 钉死的 node `24.14.0`。这次的 826 也是**本机**跑出来的，但**跑它的 node 换了一个**：
用的是仓库内 `var/toolchain` 里的 **v24.8.0**，不是系统那个 `26.7.0`。所以上一版那段
`NODE_OPTIONS=--no-experimental-webstorage` 的说明**对本次测量不适用**——它记的是拿系统
26.x 跑时才需要的规避（26.x 会把 `localStorage` 定义成一个求值为 `undefined` 的全局
getter，jsdom 只在该全局**缺席**时才装自己的那份），两条路都能跑通，但别把它当成本行数字
的产生方式。它仍然是一个**本机**数字，不能当 CI 数字引用；Playwright 这次没跑，旧的
`4 passed` 已从表里去掉而不是留着充数。

**第三行值得单独一提**：在 2026-08-29 那次测量里，它在本机和 CI 上**逐位相同**——这是
"CI 与本机跑的是同一条命令、同一组环境闸门"能拿出的最直接证据。**2026-08-31 这一版只重测了
本机（1376），没有对应的 CI 运行可比**，所以"逐位相同"是一句**上次成立**的话，不是本行现在
的属性；要恢复成当前证据，需要一次跑过同五个目录的 CI。CI 的
`Migrations, stores and end-to-end recovery` job 每个 PR 都先
`alembic upgrade head` 再跑它，2026-08-28 起 `tests/e2e` 也在其中。它仍然不覆盖需要模型
Provider 的路径。

静态门禁全绿：`ruff format --check .`（618 files）、`ruff check .`、
Pyright strict `0 errors / 0 warnings / 0 informations`、ESLint `--max-warnings 0`、
`tsc -b`、production build。配置 schema `1.19`，Alembic 单一 head
`0032_events_stream_run_sequence`（32 个迁移）。

**规模**：Python 源码 80542 行、测试 97925 行、前端 TypeScript 50613 行
（只数 git 跟踪的文件）；`docs/adr/` 下 84 份，编号 0012–0097——**不连续**：0050 与
0053 是 2026-08-13 那次号段预留里认领了、至今没有写下来的两个号（`docs/adr/README.md`
末段记着那次预留）。更早这里写过"0012–0083 连续"，两处都不实；此前的"82 份、
0012–0095"被 ADR-096 甩在后面，"83 份、0012–0096"又被 ADR-097 甩在后面——
这一行每加一份 ADR 就过期一次，是 E-04 判据里"数字该由 manifest 生成"最具体的例子。

**测试行数多于源码行数是有意的。** 本项目的规矩是**测试先证明是红的再变绿，且没有
对照组的测试不算数**——只断言"这个被拒绝"的测试，分不出一个正常工作的校验器和一个
把什么都拒绝的校验器。

---

## 3. 四个技术判断

这个项目的多数工作不是加功能，而是**找出没有症状的错误**，以及把一部分错误设计成
不可能发生。

### 3.1 让越权在类型上不可能，而不是靠信任

reranker 跑在**授权之后**。它的 Port 返回的是「每段一个分数、按位置对齐」，
**不是重排好的段落列表**（[`ports/reranker.py`](../src/agent_workbench/ports/reranker.py)）：

> 一个返回段落的适配器可以少给一段、重复一段，或者返回一段从没交给过它的东西，
> 而调用方无法把 bug 与排序区分开。分数让契约可以按长度校验，并把重排留在
> **知道 PostgreSQL 授权了哪些候选**的那一层。

于是"reranker 不可能引入提问者无权读的段落"由构造成立。超时、异常、分数条数不符
——三条路径都窄回退到已授权的原顺序，没有任何一条会扩大授权范围。

**同一种思路守着架构边界**：核心层禁止 import 框架的那条 CI 测试，连**方法调用**
都禁。`as_query_engine` 与 `as_chat_engine` 挂在项目自己会构造的 `VectorStoreIndex`
上，召唤它们**不需要任何新 import**，所以只守 import 是不够的。一个 QueryEngine 会在
检索内部把答案生成出来——位置在 ACL 检查的下游、发布闸门的上游，也就是**文本经由
一条发布闸门看不见的路径到达读者**。规则是照着"它实际会被怎样违反"写的。

### 3.2 一个没有任何症状的 bug

BGE-M3 的词法投影头 `sparse_linear.pt` 与主权重分开存放，而 FlagEmbedding
**不把它的缺失当错误**：它会新建一个 `Linear` 然后继续跑。于是下游每一道检查都通过
——向量宽度是对的（宽度来自词表，不来自这个投影），维度守卫也过——**它们只是一个
随机投影，每个进程都不一样**。

代价记在代码注释里：两份被撤回的诊断，以及一份 hybrid 臂其实是「一串互不相关的
采样」的消融报告。所以检查被前移到构造模型之前，缺权重就**拒绝构造**，错误信息里
附上取回权重的命令。

修复之后重测，结论与作废的那些**相反**，而 [`ABLATION.md`](../evals/rag/reports/ABLATION.md)
公开作废了自己此前的两条论断，并且拒绝给延迟现象一个过早的归因：

> 不写结论是刻意的。本文件上一版就是把整个延迟数量级归因给一个刚找到的 bug，
> 而那个归因后来被证明不完整——找到一个能解释现象的原因，不等于找到了全部原因。

### 3.3 不可复现的根因是分数，不是排序

混合检索曾经跨重建索引不可复现。诊断一度写作"并列检索分数没有确定性次序"，据此
同一个问题两次提问可能得到不同上下文与不同引用。
[ADR-033](./adr/0033-fusion-ranks-are-ours.md) 把它查清楚了：**次序不稳是结果，
分数本身不稳才是原因**——服务端 RRF 按臂内名次计分，而一个点在两臂里都并列时，
它的名次是引擎的任意选择，于是融合分数是任意的（实测 10 次重建索引得到 10 个不同
次序，严格最优点有 2 次不在第一位）。**排序发生在分数之后，任何后排序都够不着它。**

修法是把那一次 RRF 移进本进程，两臂先各自按 `(-score, chunk_id)` 定序再融合；
`chunk_id` 由 chunk 派生，所以重建索引后不变。
`tests/vector/test_tied_score_order.py` 钉住这一点，含"高分仍然压过小 id"的对照组。

### 3.4 检查被它本该拦住的东西满足

同一类缺陷出现过三轮，都是**围栏在场、但被它该拦的东西满足**：

- epoch 比当前 attempt 更旧的 `intended` 行，被下一个 Worker 读成"还没做过，去做"
  ——于是外部副作用做了两次。改成转人工核对；
- 图节点每次向 Registry 问**最新** epoch 再写入——一个已经失去租约的 Worker 会拿
  顶替者的 epoch 通过账本围栏。改成在**领取时**拿到的不可变 `ExecutionLease` 下写入；
- `knowledge_search` 把**检索到的全部**段落记进 journal，于是超出结果预算、根本没
  渲染给模型的段落也能通过引用校验。改成只记**渲染给模型的**。

还有一轮是**"装齐了但没接上"**：能力在组合根装配完整、配置与信封全对，而**真正跑的
那条分支没接上**——外部研究节点从不调用模型，`synthesize` 从不进入工作区会话，三个
工作区工具每次都失败而 run 仍然报告成功。
[ADR-032](./adr/0032-the-external-researcher-is-an-agent.md) 把测试为什么没挡住单独
记了一笔：那条测试断言的是"目录会被交给这个 profile"，没有断言"图里那个节点会用这个
profile 跑起来"——**只测装配、不测调用的断言，正是这类缺陷的藏身处**。

---

## 4. 明确没做的

能力状态只按 `Planned → Implemented → Tested → Demonstrated` 升级，**没有可链接的
测试或演示证据不得升级**。按这条规矩，以下明确**未**完成：

- **生产身份认证**。当前 Identity 适配器只信任请求头，所以 API 只能在受控本机使用；
  监听地址限制在 loopback，但那是防误暴露的机制，不是身份认证；
- **LlamaIndex 检索适配器已建成并通过契约测试，但没有成为默认**——缺的不是实现，是
  一份能把两条检索路径区分开的等价性度量，而那次评测**测不出来**：每个检索器与自己的
  不一致（9-10/38 题）都宽于两条路径之间的差异。**造成那道噪声底的缺陷已由 ADR-033
  修掉，但评测还没有在可复现的检索器上重跑**，所以现在缺的是证据，不是通往证据的路；
- ingestion 未迁移到 LlamaIndex、RAGAS runner 未落地，因此能力表里这两项**整体保持
  Planned**：适配器存在不等于框架集成已完成；
- **界面上还有一半沉默**：Work 的任务时间线已经会说出"这一段历史缺了哪几个位置"，
  Chat 的 `stream.quarantined` 仍然只推游标、不显示；
- **watchdog 只做了 warn 那一半**：abort（标记 unhealthy、停止 claim、取消进行中的
  run）未实现，也没装到 Task Worker；
- Langfuse、CrewAI 对比、benchmark runner、动态 Multi-Agent supervisor 与 agent
  spawn、持久 mailbox、Chat 历史压缩、生产部署与远程对象存储（`s3` 只有配置位，
  装配层拒绝启动）均未完成；
- **控制台管不了的事**：Chat 会话只活在浏览器里（服务端没有 list/rename/delete）；
  知识库只能创建与上传，没有重命名、删除、重建索引或 ACL 管理；没有"逐条消息的临时
  附件"，输入框旁的上传是把文件永久放进所选知识库；Word 只能读（文字预览）与生成，
  不能编辑；
- 当前 Compose 只用于本机演示，不能作为生产部署或生产级多 Worker 的证明。

逐条的分类、仓库位置与"做完了算什么样"，见[已知缺口](./known-gaps.md)。

**一条已经更正过的记录**：这里此前写着"CI 那个真服务 job 不是每次都全绿"，因为
`test_the_hybrid_and_dense_paths_agree_on_the_tie_break` 会偶发失败。那条缺陷已由
ADR-033 修掉，而且当初的诊断是错的（不稳的不是并列分数的次序，是分数本身）。
这条留在这里而不是删掉——**一个被改过口径的结论比一个悄悄消失的结论有用**。

---

## 5. 五分钟自己跑

不需要数据库、不需要联网、不需要 API key，输出逐字节可复现：

```bash
uv run agent-cli demo
```

想看策略拒绝时 handler **完全不会被调用**：

```bash
uv run agent-cli demo --deny
```

完整本机拓扑（PostgreSQL、Qdrant、API、Worker、控制台）见
[本机运行手册](./running-locally.md)与 [Compose 部署](./deployment.md)。

---

> 本仓库为 clean-room 实现，边界见 [NOTICE.md](../NOTICE.md) 与
> [合规说明](./compliance.md)。
