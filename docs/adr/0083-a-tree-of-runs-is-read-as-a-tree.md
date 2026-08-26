# ADR-083：一棵运行树要能按运行读出来，而不是靠客户端筛

- 决策点：ADR-082 让一次运行可以派生另一次运行，写进**同一个 stream**、用**自己的
  `run_id`**。于是事件表里第一次出现了"一个流里有一棵树"，而读这一侧一行没动：
  `events` 只有 `(stream_id, sequence)` 一条索引，`EventLogPort.read` 只按流读，前端把
  父子两个 agent 的模型调用和工具调用平铺在一起。"只看这个子代理做了什么"要不要成为一
  次查询；如果要，它是新端点还是既有 timeline 的一个参数，以及它算不算一次鉴权
- 状态：**接受**。`EventLogPort.read` 新增可选 `run_id`（**两种实现都跟**），`events`
  加一条 `(stream_id, run_id, sequence)` 索引，`TaskService` 新增 `run_tree()` 与
  `timeline(run_id=...)`，API 新增 `GET /v1/tasks/{id}/runs` 与 timeline 的 `run_id`
  查询参数，前端给委派出去的那些行标上它们自己的 agent 名。
  **明确不做**：不新建持久的树表，不做 agent 间投递／mailbox／teams／observer，不给
  `read_isolating` 加窄口形态，不把子运行的花费汇总进父节点，不改共享的步骤流组件去做
  可折叠分组
- 日期：2026-08-26
- 影响：`ports/event_log.py` 的 `read` 新增参数（**契约变更**）；
  `adapters/memory/event_log.py` 与 `adapters/persistence/event_log.py` 各实现一半；
  `adapters/persistence/models.py` 新增一条 `Index`；
  `migrations/versions/0032_events_stream_run_sequence.py`（**一次 migration**）；
  新增 `application/run_tree.py`；`application/tasks.py` 新增 `MAX_TREE_EVENTS`、
  `TaskRunTree`、`run_tree()`，`timeline()` 新增 `run_id`；
  `apps/api/routes/tasks.py` 新增 `TaskRunTreeResponse`、`/runs` 路由、
  `MAX_RUN_ID_LENGTH`；新增 `web/src/features/work/delegations.ts` 并接进
  `WorkPage.tsx`。两处测试替身跟随契约加宽。
  **不动配置契约**：`config_schema_version` 保持 `1.18`——这份 ADR 一个配置叶子都没加

---

## 1. 背景：树已经在表里了，只是没有人能按树读它

ADR-082 之后，一个 Task 的 `events` 表里是这样的：

```
sequence  run_id        event_type
       1  run_parent    RunStarted
       2  run_parent    ToolStarted(delegate_agent)
       3  run_parent    AgentDelegated(child=run_child)
       4  run_child     RunStarted
       5  run_child     ToolStarted(knowledge_search)
       …
```

三件事同时成立，而且它们互相独立：

1. **关系已经落库了。** `AgentDelegated` 是 durable 的，带 `child_agent_run_id`。重建
   一棵树需要的东西一个都不缺。
2. **没有一条查询能只要 `run_child`。** `EventLogPort.read` 的签名里没有 `run_id`，
   `events` 表上也没有能支撑它的索引。
3. **前端把它们平铺。** `WorkPage` 按 `graph_node_id` 分组，而子运行的 scope 继承父
   运行的节点（`EventDelegationChannel.sink_for_child`）——所以它落在**正确的**阶段
   里，只是没有任何东西说这一段是另一个 agent 写的。

一个流里有多个运行**并不新鲜**：Chat 的 stream 是会话、每个回合是一次运行，一直如此。
新的是**第一次有人想只要其中一个**。

## 2. 决策：四件事一起动，每件都先否掉了显然的做法

### 2.1 不新建持久的树表

显然的做法是一张 `run_tree` 表，写入时维护。**否掉**：事件已经是 durable 的、已经有序、
已经有事务边界，第二份同形状的真相是一件要和第一份保持一致的负债，而第一份是带事务的
那个。`application/run_tree.py` 是一个**纯函数**，从事件重建。

代价被写出来而不是藏起来：重建要看整条流。所以 `run_tree()` 内部分页读，并且**有上限**
（`MAX_TREE_EVENTS = 10_000`），撞到上限时返回 `complete=False` 而不是把一棵残缺的树当
完整的交出去——和 `skipped_sequences` 是同一条规则。

### 2.2 `run_id` 是既有 `read` 的一个参数，不是第二个方法

**否掉 `read_by_run(...)`**，两个理由：

- **游标语义必须一模一样。** `after_sequence` 是**流**里的位置，不是过滤后结果的下标。
  只有这样，客户端才能握着一个游标改主意换过滤条件。两个方法会立刻长出两套游标解释。
- **鉴权必须是同一次鉴权。** `TaskService.timeline` 先做 `self.get(principal, task_id)`，
  然后才读。窄口读如果是另一条路径，它就会拥有自己那份"这个 principal 能不能读这个
  Task"的答案，而两份答案里总有一份会先过期。

**并且窄口读走严格路径。** `read_isolating`（ADR 之前就有的、能隔离读不出来的行的读）
**没有**窄口形态：一行解不开的记录读不出 `run_id`，它既不属于窄口页也不属于其余部分，
给它一个 `skipped_sequences` 只能是编一个。窄口读是**导航**而不是这个 Task 的历史，所以
它遇到这种行就停；旁边那条不窄口的 timeline 才是保持可读的那个。

### 2.3 索引三列，而且 `sequence` 在最后

`(stream_id, run_id, sequence)`。前两列是等值，看起来第三列可有可无——**不是**。这条读
是**按 `sequence` 排序并用它做游标**的：没有它在末尾，规划器能找到行但拿不到顺序，于是
为了返回十二条事件去排序整条流。

这不是推测。把索引删掉再跑
`tests/persistence/test_run_tree_index.py`，两条都红，计划里是
`Seq Scan on events` 加一个 `Sort`——正好是这条索引要消掉的两件事。那份测试断言的是
**计划**，因为一条索引的全部价值恰恰是正确性测试看不见的：加不加它，返回的行一模一样。

### 2.4 前端只做"标上名字"，不做可折叠分组

可折叠的子代理分组是更好的 UI，而它要改每个阶段都在用的共享步骤流组件。**这次不做**，
并且说清楚：一行上的名字是让这些事件可读的最小充分改动，可折叠的子树值得单独一次改动。

前端也**不为此多发一个请求**。`/runs` 与 `/timeline?run_id=` 是给**没有整条流**的客户端
准备的（深链到某一个子代理）；页面已经把事件都拉下来了，再去问一次是用第二个请求学第一
个请求已经带回来的东西。

## 3. 被拒绝的方案

| 方案 | 拒绝理由 |
|---|---|
| 在客户端过滤已拉到的页 | 答的是另一个问题——"这个运行的事件里，恰好落在流的前 500 条中的那些"。一个在长 Task 末尾跑的子代理于是**不可见**，而不是空 |
| 一张 `run_tree` 持久表 | 第二份真相。事件已经 durable、有序、有事务边界 |
| 给 `read` 加 `event_types` 过滤，让树只读它需要的那几类 | 会让端口长出一个查询语言。树是唯一的消费者，而它有一个更简单的上限可用 |
| 让 `run_tree()` 无上限地读完整条流 | 一次请求就能让服务器把一个任意长的 Task 装进内存，而这里每一条读都有上限正是为了防这个 |
| 把子运行的花费汇总进父节点 | 父运行的预算从来没看见过子运行的 token（ADR-082 §5）。在读模型里求和会让那个数字读起来像一个它不是的保证 |
| 用 `parent_event_id` 串起父子（那一列至今零赋值） | 它是**事件**之间的嵌套，而这里要的是**运行**之间的。用它就得让子运行的 `RunStarted` 知道父运行那条 `AgentDelegated` 的 `event_id`，也就是把一个事件 id 从 handler 传进 runtime——为一条读路径撬开写路径 |

## 4. 不变量

1. **窄口不是鉴权。** `run_id` 在"这个 principal 能不能读这个 Task"之后才起作用，只在
   它已经有权读的事件里做选择。一个不存在的 `run_id` 与一个还没写过事件的 `run_id`
   得到同一个答案：空页。
2. **游标是流里的位置。** 窄口页返回的 `cursor` 与不窄口的同义，客户端可以拿着它换过滤
   条件。
3. **窄口发生在切页之前。** 两种实现都是先按 `run_id` 过滤再取 `limit`。
4. **树里不丢运行。** 只有 `RunStarted` 没有终态的显示为 `running`（崩溃留下的正是这个
   形状）；被父运行宣布过但自己一个事件都没写的显示为 `unknown`；父运行不在本页的子运行
   是**本页的**根。
5. **一棵不完整的树说自己不完整。** `complete=False`，而不是一棵看起来像"这个 Task 派得
   少"的树。
6. **两种实现给同一个答案。** `tests/contracts/test_event_log_narrowed_read.py` 参数化跑
   memory 与 postgres。

## 5. 这买到了什么，没买到什么

**买到了**：一个子代理可以被单独打开、单独分页、单独读到底，代价是一次索引查找而不是一
次扫流；一个 Task 可以先回答"这里面有哪些运行"再决定读哪个；而前端在**零新请求**的情况
下，把两个 agent 的事件区分开了。

**没买到**：

- **不是可折叠的时间线。** §2.4。
- **不是 agent 间通信的观测面。** 这份 ADR 只让已经写下来的东西可读。ADR-082 §5 拒绝的
  那些（mailbox、teams、observer）没有因为有了读模型而变得更近。
- **不是跨 Task 的树。** 一棵树在一个 stream 之内。跨 Task 的父子关系不存在，因为委派不
  开新 Task（ADR-082 明确不做后台派生）。
- **不是对着真模型验证过的。** 能力梯子停在 **Implemented + Tested**：树的形状是对着构造
  出来的事件页验证的，不是对着一次真实的、由模型自己决定派生的运行。

## 6. 怎么验证

| 测试 | 抓住什么 |
|---|---|
| `tests/contracts/test_event_log_narrowed_read.py`（6 × 2 实现） | 不变量 1、2、3、6。含"窄口发生在切页之前"那条：十二条事件里子运行占最后两条，页大小 3 |
| `tests/persistence/test_run_tree_index.py`（2） | §2.3。断言的是 `EXPLAIN` 的**计划**，一条查索引、一条查没有 `Sort` |
| `tests/application/test_run_tree.py`（10） | 不变量 4、5。含崩溃后的半棵树、只被宣布过的孩子、父运行不在本页的孩子、孙子层的嵌套 |
| `tests/application/test_task_run_tree_service.py`（8） | 不变量 1。含"陌生人窄口读仍然是陌生人"与"未知 run_id 是空页不是披露" |
| `tests/persistence/test_migrations.py`（既有 7 条） | migration 链与 `models.py` 的 metadata 一致 |
| `web/src/features/work/delegations.test.ts`（8） | 前端：不含委派事件的页不猜父运行 |

**索引不是装饰的，当场验了一次**：`DROP INDEX ix_events_stream_run_sequence` 后重跑，
两条计划测试都红，`EXPLAIN` 里是

```
Sort  (cost=154.40..154.43 rows=12 width=1241)
  Sort Key: sequence
  ->  Seq Scan on events  (cost=0.00..154.18 rows=12 width=1241)
        Filter: (((stream_id)::text = 'str_index_a') AND ((run_id)::text = 'run_index_child'))
```

建回去即绿。
