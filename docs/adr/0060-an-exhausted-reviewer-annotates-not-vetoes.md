# ADR-060：用尽修改预算的评审者做批注，而不是行使否决

- 决策点：评审节点在修改预算耗尽后仍要求修改时，Task 是失败还是带瑕疵交付；
  批注走哪条通道到达读者；`approval_id` 要求"通过的评审"这条不变量怎么办
- 状态：**接受**。两张图的耗尽分支都改为按 pass 同路线路由（gate 开则进
  approval，否则 export，没要文件则成功收尾）；未解决的评审以三段接力到达
  Task 行：`TaskState.unresolved_review`（领域事实）→
  `CheckpointPosition.caveat`（完成位置的自白）→
  `mark_succeeded(detail=...)`（行上的记录，控制台读它）
- 日期：2026-08-16
- 影响：`domain/tasks.py`（新属性 `unresolved_review`；`approval_id` 的不变量
  从"通过的评审"放宽为"有评审"）；`workflows/research_graph.py` 与
  `general_graph.py`（耗尽路由 + 删除 `quality_gate_failure_reason` /
  `review_failure_reason`，`terminal_failure_reason` 只剩人的拒绝）；
  `ports/task_workflow.py`（`CheckpointPosition.caveat`，只允许出现在
  完成且未失败的位置上）；`adapters/langgraph/workflow.py`（`_finish_caveat`
  组句，两图共用——它读的是共享领域事实，不是某张图自己的门）；
  `application/task_recovery.py`（`Reconciliation.caveat`）；
  `ports/task_registry.py` + PostgreSQL 实现（`mark_succeeded` 可带 detail）；
  控制台在 succeeded 且带 `status_detail` 时渲染「评审仍有未解决的意见」。
  **`max_revisions=2` 数值不动**：它从"失败门"变成"停止修改门"，仍然是
  循环的上界
- 依赖：ADR-031（两图共享修改预算）、ADR-059（重试的边界以它为对照）、
  用户决策 2026-08-16（「要有瑕疵的成品，不要一次失败」）

## 1. 背景：两次拒绝，读者拿到的是零

旧行为：评审连续两轮仍要求修改，`route_quality_gate` / `route_review` 返回
"无后继节点"，`terminal_failure_reason` 给出一句失败。代价的形状是：草稿写了、
修改了两轮、每一轮都比上一轮好——然后 Task 标记 failed，读者一个字都拿不到，
连同评审自己的意见一起消失。对一个"总是容易失败"的抱怨来说，这是自伤型的
失败：工作是完成的，只是最后一个守门人把它整个扔掉了。

失败门的本意是质量下限。但一个只能在"扔掉全部"与"假装通过"之间二选一的门，
在预算耗尽这个时刻不再有第三个诚实选项——除非把"带着未解决的意见交付"变成
可表达的结果。这正是本 ADR 增加的表达能力。

## 2. 决定：三段接力，每段只说自己层的话

1. **领域**：`TaskState.unresolved_review`——评审要求修改且预算不可再付时非
   空。一个属性而不是三处重复的三段判断。`approval_id requires a passing
   review_result` 放宽为 requires **a** review_result：门可以被问及一份评审
   仍有异议的草稿（人看到的就是评审后的原样），不可表达的仍然是"没有任何
   评审的门"。
2. **位置**：`CheckpointPosition.caveat`，校验器限定它只出现在完成且未失败的
   位置上——批注修饰成功；失败的位置已经有自己的一句话，两个故事不同时讲。
   句子由适配器的 `_finish_caveat` 组装（两图共用：它读共享领域事实），
   截断到 256——完整意见在 checkpoint 的 `review_result` 里，审计不丢失。
3. **行**：`Reconciliation.caveat` → `mark_succeeded(lease, detail=caveat)`。
   succeeded 的 `status_detail` 从此只有一种含义：交付时评审仍有未解决的
   意见。控制台据此渲染「评审仍有未解决的意见，产物按现状导出」。

路由上，耗尽与通过走同一条线：要文件且 gate 开 → approval（人对"评审后的
草稿"作决定，拒绝仍是终态失败——人的否决与评审的否决不同，前者本来就是这道
门存在的意义）；gate 关 → export；不要文件 → 成功收尾，caveat 照走。

## 3. 为什么不是别的做法

- **把最后一轮 revise 改写成 pass**：伪造记录。评审说的话必须原样留在
  checkpoint 里。
- **把意见写进导出的文档**：导出走 gateway 工具调用，幂等台账的规范请求哈希
  只覆盖 draft——把评审意见掺进参数会改变台账语义，且污染交付物本身。
  批注属于元数据通道，不属于成品正文。
- **提高 `max_revisions`**：改的是循环跑多少圈，不改"最后一圈之后发生什么"。
  两者正交，本 ADR 只动后者。

## 4. 证据

- 路由与属性：`tests/workflows/test_research_graph.py`、
  `test_general_graph.py`、`test_cross_graph_invariants.py`（两图同语义，
  一条不变量而不是两种措辞）。
- 编译图端到端：`test_langgraph_workflow.py`（耗尽 → 过门 → 导出，verdict
  不被伪造，`inspect` 的位置带 caveat）、`test_general_graph_execution.py`
  （预算仍然限住循环次数，变的只是落点）。
- 接力下半程：`tests/application/test_task_recovery.py`（reconcile 不丢
  caveat）、`tests/persistence/test_task_registry.py`（行上记录得住）。
