# ADR-056：一条流可以整条消失，但不能变稀

- 决策点：控制台要不要能删除会话与任务；如果要，事件日志那条「只增不删、位置不
  复用」怎么办；删除的粒度是什么
- 状态：**接受**，把「无空洞」这条不变量**明确成流内不变量**，并规定删除只能
  以**整条流**为单位
- 日期：2026-08-16
- 影响：`ConversationStore` 新增 `delete_session`，`TaskRegistry` 新增 `delete`；
  三条 `DELETE` 路由（`/v1/chat/sessions/{id}`、`/v1/code/sessions/{id}`、
  `/v1/tasks/{id}`）。`EventLog` 的**写入路径一字不改**，不新增任何删除事件的
  API；`ArtifactStore` 不获得删除能力；配置 schema 保持 `1.14`；无数据迁移
  （现有外键足够，见 §4）
- 依赖：[ADR-047](./0047-a-session-is-named-by-its-first-sentence.md)（会话列表是
  服务端的）、[ADR-049](./0049-an-evaluation-is-a-process-not-a-task.md)（终态的
  含义）

## 1. 背景：41 条路由，没有一条能删

`src/agent_workbench/apps/api/` 全目录搜 `@router.delete`，结果为空。
`ConversationStore`、`TaskRegistry`、`ArtifactStore` 三个 Protocol 都没有删除方法。
`docs/known-gaps.md` 的 D-02、D-03 记着这件事，措辞是「没有列表、没有改名、
没有删除」—— 列表和改名后来补上了（ADR-047），删除没有。

后果不是「少一个功能」。Chat 的会话列表在浏览器里，删不掉只能清 localStorage，
而服务端那一行会**永久不可达**；Code 和 Work 的列表在服务端，于是每一次试手、
每一条跑失败的任务都永久占据侧栏，把还在用的那几条挤到看不见。一个只增不减的
列表，用得越久越不能用。

## 2. 冲突：`EventLog` 说过不删

`adapters/persistence/event_log.py` 写着：

> The per-stream sequence stays gap-free -- nothing is deleted and no position
> is reused

这条不变量是有用户的。回放靠它：订阅者拿着 `resume_after` 回来，服务端从那个位置
之后接着发，而「接着发」的正确性依赖于中间不会凭空少掉几条。ADR-051 的实时帧不带
位置，也是因为位置这件事必须可靠。

所以删除任务这件事，正面撞上了这条线。

## 3. 决策：不变量是**流内**的，删除是**整条流**的

把那句话读准：它约束的是**一条流内部**的序号连续性。它没有说、也不需要说
「一条流永远存在」。

于是本 ADR 规定：

> **删除的最小粒度是一条流。** 一条流要么完整存在，要么整条不存在；任何删除都
> 不得只拿掉其中一部分。

在这个规定下，「无空洞」在**每一条活着的流上**仍然逐字成立，而它对一条已经不存在
的流不置一词 —— 一个订阅者永远不会看见「这条流第 7 到 12 条没了」，因为不存在
这样的中间状态。它只可能看见流本身不在了，而那是一个 404，不是一个空洞。

反过来说，本 ADR **禁止**的东西同样明确：不得删除单条事件、不得按时间裁剪
（「只保留最近 30 天」）、不得压缩重写序号。那三样都会让存活的流变稀，而变稀正是
这条不变量拦的东西。今天没有代码想做这三件事；写在这里，是为了让将来想做的人
知道它需要的是另一份 ADR，而不是这一份的延伸。

## 4. 每一类删除具体删什么

### 4.1 会话（Chat 与 Code）

两者是同一张表 `conversation_sessions`，`mode` 列区分。

| 数据 | 怎么走 |
|---|---|
| `messages`、`chat_turns` | 外键 `ondelete="CASCADE"`，数据库自己走 |
| `events`（`stream_id == session_id`）、`event_streams` | **显式删**：这两张表和会话之间没有外键，`stream_id` 是裸的字符串列 |
| 工作区 artifact | **不删**，见 §5 |

有回合在跑的会话拒绝删除。理由不是数据完整性，是诚实：那一轮正握着一个协程
（ADR 记录的 Code 前提），删掉它的会话会让那个协程写进一个不存在的地方。

### 4.2 任务

`task_runs` 有两张**没有 `ondelete`** 的子表，它们会直接把 `DELETE` 挡回来：
`approvals` 与 `tool_executions`。所以顺序是显式的：

```
approvals → tool_executions → events(stream_id == thread_id) → event_streams → task_runs
```

checkpoint 那三张表**不在这个序列里**，因为已经有人做了这件事：
`adapters/langgraph/checkpointer.py` 的 `adelete_thread()`，而且它自带一道
「Task 未到终态就抛 `ThreadStillExecutingError`」的保护。复用它，而不是再写一遍。

**不加迁移把那两个外键改成 CASCADE。** 显式按序删只多三行，而 CASCADE 会让「删一
个 Task 会连带删掉哪些东西」变成一个要去读 DDL 才知道的事实。这里希望它是一段能
读的代码。

### 4.3 语义是两步：先取消，再删除

`domain/task_registry.py` 已经有 `CANCELLABLE_STATUSES` 和终态的概念，
`adelete_thread` 已经拒绝非终态。删除接口沿用同一条线：**只有终态可删**，
未终态返回 409，取消这件事仍然由 `POST /v1/tasks/{id}/cancel` 负责。

这不是给使用者添麻烦，是不重新发明取消。一个「删除时顺手取消」的接口，等于在
删除路径上复制一份租约与 epoch 的逻辑，而那份逻辑正是 Task 侧最不该有第二个副本
的东西。

## 5. 为什么 artifact 不跟着删

`ports/artifact_store.py` 没有 `delete`，本 ADR 不给它加。

`application/workspace.py` 已经立了这个立场：工作区的旧版本**不删除，只是不可
达**。删掉一个会话之后，它的工作区 artifact 变成没有引用者的字节 —— 这与一次
`workspace_write` 之后旧版本的处境完全相同，不是新出现的情况。

给 `ArtifactStore` 加删除，需要回答的是另一组问题：一个 artifact 可能被多处引用
（任务的 `input_ref`、导出报告、工具结果），谁来判定最后一个引用没了。那是引用
计数或垃圾回收，是一份独立的 ADR，不该混在「侧栏能不能删一行」里搭车通过。

代价写明：**删除会话或任务不回收磁盘空间。** 这是已知的、被接受的。

## 6. 什么没有变

- `EventLog` 的写入路径、`append` 的序号分配、回放的 `resume_after` 语义、
  quarantine 那一套 —— **一字不改**。本 ADR 没有给事件加删除 API；删除发生在
  持久化适配器里，以整条流为单位。
- 事件本身仍然只增。一次删除不写「某某被删除了」的事件到被删的那条流里 —— 往一条
  正在消失的流里追加一条记录，是这份 ADR 唯一明确无意义的做法。
- 租户与所有者边界不变：删除走和读取同一条鉴权（先 `get`，再删），所以删不掉别人
  的东西这件事，和看不到别人的东西是同一个判断。

## 7. 做完的判据

三处各删一条，刷新后不再出现；未终态的任务删除返回 409，取消后可删；契约测试在
内存与 PostgreSQL 两套实现上跑同一份用例；删除一条会话后，同一条流的事件与
`event_streams` 行都不再存在，而**其它流的序号连续性未受影响**。
