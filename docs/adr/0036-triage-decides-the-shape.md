# ADR-036：提交前的预判决定形态，判不准就问，而不是让人每次都选

- 决策点：graph 与 wants_report 这两个提交时决定由谁做出；"模型自动选图"被 ADR-031
  §2.3 拒绝之后，什么变了
- 状态：**接受**，取代 [ADR-031](./0031-a-second-graph.md) §2.3 的"不做自动路由"，
  并删除两处客户端的报告关键词正则
- 日期：2026-08-10
- 影响：新增 `POST /v1/tasks/triage` 与 `application/task_triage.py`；`TaskSubmitted`
  事件与 `TaskSubmission` 增加可选 `intent` 溯源块；`workflows/structured_output.py`
  从 `task_handlers` 提升两段纯函数；配置新增 `triage` 段（默认关闭）。
  **提交端点的冻结与幂等语义一字不改。**
- 依赖：ADR-031（两张图与提交时冻结）、ADR-034（结构化输出的补问边界）

## 1. 背景：这个决定已经在被猜了，只是猜得很差

ADR-031 §2.3 拒绝自动路由的理由是一句完整的推理：让模型看一眼目标就决定走哪条流水线，
是把一个使用者清楚知道答案的问题交给一个会猜错的东西，而猜错的代价是整条流水线跑错。

这句话描述的是一种特定的自动路由：**静默的、不可见的、猜完即冻结的**。它没有描述今天
实际存在的东西——

- web 的"生成报告文件"开关默认值来自一条正则
  （`报告|报表|文档|文件|导出|输出一份|写一份|report|document|export`）；
- CLI 用另一份更窄的关键词表（`报告/文件/导出/report`）做同一件事；
- 直接调 API 则默认 `false`。

同一句目标，三个入口三种行为，没有任何地方记录"这是谁决定的"。而执行方式那两个
radio 把一个几乎总能从目标读出来的问题，变成每次提交都要人手回答的必答题。

## 2. 决策

### 2.1 预判发生在 Task 存在之前

新增 `POST /v1/tasks/triage`：无副作用、不开 Task、不写任何行。入参是目标与两个
上下文事实（选没选知识库、附件名），出参三选一：

- `decided`——graph、wants_report 与一句理由；
- `ask`——仅当 graph 判不准：一个问题和两个选项，由客户端渲染，用户点选后带**显式**
  graph 重新提交；
- `default`——判定失败、超时或该部署未启用：客户端按部署默认提交，与今天逐字节相同。

这个位置选择是本 ADR 与 ADR-031 §2.3 不冲突的原因：**询问发生在还没有 Task 的时候**，
所以不需要第九个 `TaskStatus`、不需要新的 interrupt、不碰 Worker、恢复与租约回收的
任何一行推理。

### 2.2 提交端点不变，"auto" 不存在于提交语义里

`POST /v1/tasks` 仍然只接受显式的 `graph` 或缺省（部署默认）。预判的结果由**客户端**
变成显式值再提交，于是 ADR-031 §2.3 的冻结不变量原样成立：graph 在提交时冻进
`task_runs.graph_version`，Worker 只读行；幂等冲突仍只比较
`(graph_version, input_fingerprint)`。一个 triage 结果不确定的重试不会把幂等重试
变成 409，因为提交里根本没有"auto"这个值。

### 2.3 决定可见：intent 溯源块

`TaskSubmission` 增加可选 `intent`：`{graph_decided_by, wants_report_decided_by,
reason?}`，取值 `user | model | default`。它**不进** `task_runs` 的列（像
`index_reservation` 一样从列映射排除），**不进**幂等身份，只进 `TaskSubmitted`
事件的 payload——时间线本来就是回答"这个 Task 为什么是这个形状"的地方。

它也**不进** `TaskInput`。那个 artifact 的字节参与 `input_fingerprint`，而
`canonical_bytes()` 不排除默认值：给它加任何可选字段，都会让所有存量 Task 在
`load_state` 的指纹复核上失败。graph 本身不进 intent，绑定事实仍只有
`task_runs.graph_version` 一处。

### 2.4 wants_report 判不准取 false

两张图的审批被拒绝后 Task 的终态都是 **`failed`**（research_graph `route_approval`
与 general_graph 同形）。若判不准取 true，等于替一个可能根本不想要文件的用户强开
一道"批准或失败"的闸门。取 false 的代价是：真想要文件的用户重新提交一次；答案本身
仍然写在任务页上，没有丢失任何工作。正则删除后，这是唯一的默认方向。

wants_report 判不准**不问**。问题只在 graph 判不准时出现，一次最多一个问题——
一个每次提交都可能连问两句的表单，比两个 radio 更繁琐，这个 ADR 就白写了。

### 2.5 结构化输出复用 ADR-034 的边界

预判是一次 toolless 结构化调用。`task_handlers` 里的 `_json_object`（拒绝围栏、
尾注、重复键、非标常量）与 `_restatement_messages`（补问的那一轮）是状态无关的
纯函数，提升到 `workflows/structured_output.py` 供两处共用。补问的边界与 ADR-034
相同：**只有 framing 失败补问一次**；一个把 graph 写成别的词的回答是模型做出并做错
的断言，追问只会把它推向一个"能通过"的答案，直接落 default。

### 2.6 默认关闭，先测再开

`triage.enabled` 默认 `false`。`evals/triage/` 提供一份标注金集（清晰 research /
清晰 general / 真含糊三类）与运行脚本；一个部署在看到自己模型的准确率报告之前，
没有理由打开它。这与 ADR-017 的能力表纪律同源：没有本地证据，只有 Planned。

## 3. 后果

- 新建任务从"两个必答 radio + 一个开关"变成"只填目标"；显式覆盖仍在（高级设置与
  `/graph`、`--graph`），且显式值跳过预判、优先于一切；
- 三个入口第一次共享同一个判定实现，且每个 Task 的时间线记录判定来源与理由；
- 猜错的代价从"整条流水线跑错且无从发现"变成"时间线上一行可读的理由 + 一次可覆盖
  的重提"；判不准时代价是一个问题，而不是一个错误的 Task；
- **代价一**：提交路径可能多一次 LLM 调用（~1-3s）。它有超时和 default 兜底，且
  不阻塞不使用它的调用方；
- **代价二**：两个纯函数从 task_handlers 提升，节点与预判从此共享一段解码语义——
  改它要同时想两处。测试对两处分别钉住行为；
- **不解决**：max_revisions 的选择（保持默认+高级设置）；知识库选择（那是权限与
  范围，不是形状）；预判提示词的多语言。

## 4. 备选方案

**`POST /v1/tasks` 直接收 `graph: "auto"`。** 判定进提交事务，幂等身份里出现一个
不确定性来源：同一个 Idempotency-Key 的重试可能判出不同的图，幂等重试变成 409。
为修它得把 graph_version 从冲突比较里挖掉一半语义。预判挪到提交之前，这个问题
整个不存在。

**判定结果存列而不是存事件。** 多一个迁移、多三列，换来的只是"能用 SQL 查"。
时间线已经是所有"这个 Task 发生过什么"的答案所在，intent 是其中最早的一条。

**wants_report 判不准时问用户。** 见 §2.4——一次最多一个问题是硬约束。

**继续用正则。** 正则不会说"我不确定"，也不会留下理由。它已经用三种口音各错各的。
