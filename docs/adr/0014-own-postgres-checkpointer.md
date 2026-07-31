# ADR-014：自研 PostgreSQL checkpointer，不用官方 saver

- 决策点：WP06-06 PostgreSQL checkpointer
- 状态：**接受**
- 日期：2026-07-28

## 背景

WP06-05 已把固定研究图编译到 LangGraph 上，但 checkpointer 仍是 `InMemorySaver`，
所以进程重启不保留执行位置——WP06 的验收门槛「节点失败后可从 checkpoint 恢复」
和「进程重启后使用原 `thread_id`」都还没有证据。

自然的下一步是装官方的 `langgraph-checkpoint-postgres`。实测解析结果：

```text
langgraph-checkpoint-postgres  3.0.5   MIT
psycopg                        3.3.4   LGPL-3.0-only
psycopg-pool                   3.3.1   LGPL-3.0-only
```

## 问题

**它撞上本项目两处明确写下的政策，而不是一处疏漏。**

CI 的许可证门禁写着：

> 允许宽松与文件级弱 copyleft；拒绝强 copyleft（GPL、AGPL、LGPL）与未声明
> 许可证，**这正是本门禁存在的目的**。

[合规说明](../compliance.md)重复了同一条。上一轮引入 `langgraph` 时只需要**扩充**
allowlist（`MPL-2.0` 本来就在列表内，新增的两个字符串都是宽松许可的复合写法）；
这一轮要做的是**推翻**该政策。

两者性质不同，不能用同一个理由通过。

第二个问题独立存在：`psycopg` 是**第二个 PostgreSQL 驱动**。项目现有的持久化全部
走 SQLAlchemy + asyncpg，再引入 psycopg3 意味着两套连接池、两套超时与取消语义、
两套故障模式，而协调层（WP08）恰恰要求 guard 连接、LISTEN 连接和事务边界都能被
精确推理。

## 决策

**自研 checkpointer，实现 LangGraph 的 `BaseCheckpointSaver` 契约，底层复用项目
已有的 SQLAlchemy + asyncpg。**

要实现的异步方法是：

```text
aput(config, checkpoint, metadata, new_versions) -> RunnableConfig
aput_writes(config, writes, task_id, task_path)  -> None
aget_tuple(config)                                -> CheckpointTuple | None
alist(config, *, filter, before, limit)           -> AsyncIterator[CheckpointTuple]
```

同步版本在本项目不需要：所有调用点都是异步的，同步入口应当明确拒绝而不是偷偷
起一个事件循环。

## 后果

接受的代价：

- 工作量明显大于装一个包，而且 checkpoint 的序列化格式由 LangGraph 决定，
  我们只负责存取，不解释它；
- `BaseCheckpointSaver` 不是稳定公开契约，LangGraph 升级可能改变它。因此
  `langgraph` 的版本上界必须收紧（当前 `>=0.6,<0.7`），且升级时必须重跑
  checkpointer 契约测试，而不是只看 CI 是否变绿。

换来的东西：

- **不引入 LGPL**，两处政策都不用动；
- **只有一个 PostgreSQL 驱动**，连接、事务与取消语义仍然只有一套可推理的实现；
- **checkpoint 表进本项目的 Alembic**，迁移链仍然只有一条 head。这一条**推翻了**
  [实施计划](../implementation-plan.md) §7.1 的假设「LangGraph checkpoint 表由锁定
  版本的 saver migration 管理，但其版本必须记录」——现在没有第二套 migration 需要
  记录版本，取而代之的要求是：**checkpoint 表的 schema 变更必须走本项目的
  Alembic**，且 `workflow.graph_version` 不兼容时仍按既有约定进入
  `waiting_migration`。

不变的部分：

- LangGraph saver 仍然是**图执行位置**的事实源（架构基线 §9 未变），改变的只是
  这个 saver 由谁实现；
- Task 产品状态、lease 与事件仍属于 `task_runs` / `run_events`，checkpointer
  不得成为它们的第二个 writer。

## 被否决的替代方案

**A. 把 LGPL 加进 allowlist。** 动态链接、未修改、不分发的用法下 LGPL 通常没有实际
风险。否决理由不是法律判断，而是：一个 clean-room 作品集项目的合规门禁，如果在第
一次挡住东西时就被改掉，那它从一开始就没有在约束任何事情。要改这条政策，应当是
一次独立的、有理由的合规决定，而不是为了省掉一次实现顺手做掉。

**B. 继续用 `InMemorySaver`。** 那等于 WP06 的恢复验收门槛永远拿不到证据，
而「可恢复的 Task」是这个项目对外描述的两个产品模式之一。
