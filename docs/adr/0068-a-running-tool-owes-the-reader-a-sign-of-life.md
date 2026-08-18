# ADR-068：跑着的工具欠读者一个活着的信号

- 决策点：模型写字的那段时间，控制台是逐字动的（`ModelDelta` /
  `ModelThinkingDelta`，ADR-061）；**工具跑着的那段时间，控制台一个字都不动**。
  `ToolStarted` 与 `ToolCompleted` 之间没有任何事件——而 `sandbox_run` 声明的超时
  是 300 秒。这段空白该由谁来填、填什么、填多密
- 状态：**接受**。`ToolProgress` 早就在 `domain/events.py` 里定义好、判为
  transient、被 `AnswerReleaseSink` 白名单放行、被 SSE 合并器透传、在
  `workTimeline.ts` 里有中文标签——**唯独没有任何代码发得出它**，因为
  `ToolInvocation` 不带 sink。本 ADR 补上生产端，并把它拆成**两个来源**：
  handler 自己说的**阶段**，与 executor 不问任何人就知道的**时钟**。
  handler 拿到的是一个已绑定 `tool_call_id` 的单动词 reporter，不是 sink
- 日期：2026-08-18
- 影响：`ports/tools.py`（新 `ToolProgressReporter` / `discard_progress` /
  `ToolInvocation.progress`）、`runtime/tool_executor.py`（`_ProgressChannel`、
  心跳任务、`execute(sink=...)`）、`runtime/tool_gateway.py`（把 `_dispatch`
  手上的 sink 传下去）、`domain/events.py`（`ToolProgress.message` 变可选、新
  `elapsed_ms`、至少说一件事的校验）、`adapters/tools/sandbox.py`（三个阶段）、
  `web/src/features/code/useCodeStream.ts`（`progress` 映射）、`CodeTurn.tsx`
  （`Progress` 行、`formatElapsed`）、`CodePage.tsx`、`styles/app.css`
- 依赖：[ADR-061](./0061-a-thought-in-flight-is-not-a-record.md)（在飞的想法不是
  记录——本 ADR 把同一条论证用在工具上，结论也相同：放在列表**旁边**，不放进列表）、
  [ADR-051](./0051-a-live-frame-has-no-position.md)（live 帧没有位置——所以
  `ToolProgress` 不进步骤列表）、[ADR-029](./0029-ephemeral-sandbox.md)（一次性
  沙箱：容器在退出前什么都不返回——这正是中间那个阶段必须由外面来报的原因）、
  [ADR-064](./0064-a-thought-belongs-to-the-action-it-caused.md)（想法属于它引发的
  动作——本 ADR 的进度行落在同一个 `<li>` 里，理由相同）

## 1. 背景：一条已经铺好、只差发送端的管道

`ToolProgress` 不是本 ADR 新造的类型。它在仓库里已经存在，而且**下游每一段都已
经写好并测过**：

| 环节 | 状态 | 出处 |
| --- | --- | --- |
| 类型定义 | 已有 | `domain/events.py` |
| 判为 transient（不落库） | 已有 | `EVENT_DURABILITY` |
| 通过发布围栏 | 已有 | `application/answer_release.py` 的 `_TRANSIENT_HANDLED` |
| SSE 合并器透传 | 已有 | `apps/api/sse.py:live_frames` 的 "anything else transient" 分支 |
| 中文标签 | 已有 | `web/src/features/work/workTimeline.ts:75` |
| **有人发它** | **没有** | 全仓库 `ToolProgress(` 只出现在类型定义那一行 |

`answer_release.py` 里那段注释写得很具体：「its `message` is written by a tool
handler describing its own work」——它在描述一个**并不存在的生产者**。管道通到底，
入口没接。

原因是结构性的，不是遗漏。`ports/tools.py` 的 `ToolInvocation` 只带四样东西：
`call` / `context` / `cancellation` / `timeout_seconds`。**handler 手上没有任何
可以发事件的东西**，所以它想报进度也报不了。

### 这段空白有多长

`sandbox_run` 声明 `timeout_seconds=300`（`adapters/tools/sandbox.py`），是本系统
最长的一个。它中间那一步是一个容器启动、跑任意 Python、再停止；按 ADR-029，容器
`--network=none`、无 tty、一次性——**它在进程退出前不返回任何东西**。也就是说这
300 秒里，服务端没有任何一个字节可以给控制台。

读者在这 300 秒里看到的是：工具名，加一个「进行中」。而这三种情况在屏幕上**完全
一样**：

1. 脚本正在正常跑；
2. 脚本死循环了，会在 300 秒后被超时杀掉；
3. Worker 挂了，这个 turn 永远不会结束。

ADR-061 为模型那半边解决过同一个问题，用的话是「it is the answer to *is it
working or is it stuck*」。工具这半边的那段时间更长，而它当时一个字没动。

## 2. 决策：两个来源，一个信道

进度有两个来源，它们**在种类上不同**，不该合并成一个：

**handler 说的阶段。** 只有 handler 知道自己走到哪了。`sandbox_run` 报三句：
`staging` / `executing in the sandbox` / `saving N output file(s)`。中间那句覆盖
整个 300 秒。

**executor 报的时钟。** executor 不需要问任何人就知道这个调用跑了多久。这一半的
价值在于：**它对每一个工具都成立，包括从没听说过这个信道的每一个工具**。
`workspace_grep`、`web_search`、`export_artifact` 一行代码都不用改，就都有了时钟。

只有第一种的话，这个改动只服务改过的 handler；只有第二种的话，读者知道「还在跑」
却不知道「在跑什么」。两个都要。

### 2.1 handler 拿到的是一个动词，不是 sink

最省事的做法是把 `EventSink` 放进 `ToolInvocation`。**拒绝**：handler 是 adapter
代码——一个 MCP 客户端、一个子进程包装器——给它 sink 就是给它整套事件词表，它可以
在调用它的那个 run 上发 `AnswerCommitted`。

给的是 `ToolProgressReporter`：一个动词，且 `tool_call_id` **已经绑好**。于是
「这条进度是关于哪个调用的」不是一个 handler 可能填错的字段，而是一个它根本填不了
的字段。

它还是 best-effort 的，这一条写进了 Protocol 的 docstring 并由测试钉住：投递失败
被吞掉，handler 返回之后到达的报告被丢弃。**observer 不该成为工具失败的原因**——
executor 欠调用方的是一个 `ToolResult`，订阅者缓冲区满不该顺着这条路回到 handler
里去。

### 2.2 `message` 变可选，`elapsed_ms` 是数字

心跳**没有话要说**——它存在的前提正是「什么新事都没发生」。逼它每 5 秒编一句话就
是每 5 秒编一句话。所以 `message` 变 `ShortText | None`，一次心跳只带
`elapsed_ms`。

反过来，一个三样都不带的 `ToolProgress` 是控制台只能画成空行、或者静默丢掉的帧，
而「静默丢掉」正是进度信道坏了却没人看得出来的方式。所以加了校验：**至少说一件
事**，否则构造就失败。

`elapsed_ms` 是数字而不是「已运行 12 秒」这句话，理由和这个模块其余部分一样——事件
**描述**而不**复制**。同一条事件要被中文 web 控制台、英文 CLI 和一个 tracing 后端
读，只有第一个想要那句中文。**谁显示谁遣词。**

它也不能由信封时间戳推出来：读者要减去的是 `ToolStarted` 的时间戳，而一个中途连上
来的 live 订阅者手里只有心跳，从来没见过那条 `ToolStarted`。

### 2.3 心跳不带 percent

`percent` 字段是有的，心跳**故意不填**。已耗时除以声明超时长得像完成度，但它不是：
一个跑了 30 秒、允许 300 秒的脚本不是「完成了 10%」。**一个填充速率与实际工作无关
的进度条比没有进度条更糟**，因为相信它的读者会继续等，而不是去干预。

同理，`WorkspaceSandbox.run` 报的是阶段名而不是「3 步中的第 2 步」：哪个阶段在跑
是事实，进度百分比不是这个进程知道的事。

## 3. 五秒，以及第一拍要等满一个间隔

`DEFAULT_PROGRESS_HEARTBEAT_SECONDS = 5.0`，两头都有约束：

- **下限来自读者**：这条信道要填的空白以分钟计，一个比这慢很多的时钟读者看一眼、
  发现没变、就不再信它了。
- **上限来自缓冲区**：这些是 transient 事件，和模型 delta 共用
  `event_stream.subscriber_buffer_events`（默认 256），而 delta 的到达率高出好几个
  数量级。5 秒时，一个跑满 300 秒的沙箱调用总共产生 60 条。

**第一拍要等满一个间隔**，这一条是让它不变成噪音的关键。绝大多数调用——一次
workspace 读、一次 list——在毫秒级返回，于是**一条心跳都不发**。一拍的含义是
「这一个跑得有点久了」，而且从第一拍起就是这个含义。

## 4. 为什么它不是一个配置项

本仓库对配置很严格（`docs/configuration.md` §3），所以「不加配置项」需要理由。

这个数字**不改变 run 能做什么、能碰到什么、留下什么记录**——`ToolProgress` 是
transient，永不落库。它是一个纯观察信号的节奏。

而 `runtime.tool_timeout_seconds` 就是反面教材，`settings.py` 里那段注释自己写着：
它曾经读 60、**没有任何代码消费它**，于是运维改了它等于什么都没改。每个 profile 都
要复述一遍、谁也没有理由去改的数字，是陷阱不是默认值。

需要别的节奏的部署把它传给构造函数（`ToolExecutor(progress_heartbeat_seconds=...)`），
传 `None` 则整个关掉时钟——这正是一个数事件条数的测试想要的，而 handler 自己报的
阶段仍然照发。

## 5. 前端：放在列表旁边，不放进列表

`useCodeStream` 的模块注释已经把这条规则写死了：**步骤列表只装 durable 事件**，
因为 live 帧没有位置（ADR-051），一个混进 live 帧的列表会因读者何时连上而不同。

进度和 ADR-061 的「在飞的想法」是同一类东西，所以放在同一个地方：列表**旁边**。
区别只有一个——想法是一个值，进度是一个**映射**。理由：
`runtime.max_parallel_read_tools` 默认 4，同时在飞的调用可以不止一个，单个槽位会把
最后报告的那一个显示在所有行下面。

三条清除规则，缺一不可：

1. `ToolCompleted` → 删掉那个调用。否则「executing in the sandbox · 已运行 12 秒」
   会冻在一个结局已经画出来的步骤下面——控制台在断言一个已经停下来的运动。
2. `ToolFailed` → 同上。
3. `RunCompleted` / `RunFailed` / `RunCancelled` → 全清。被 run 的取消杀掉的工具
   **两条都收不到**，规则 1 和 2 对它永远不触发。这与 `ModelCompleted` 那边的
   不对称是同一个（`agent_runtime.py`：`if turn.finish is not None and turn.finish
   != "cancelled"`）。

渲染端再加一道：只有 `outcome === "running"` 的步骤才画这一行。映射里已经不该有已
结束的调用了，这一道是把「不可能」从「不太可能」升上来。

## 6. 被拒绝的替代方案

**让 sandbox MCP 服务端流式回传 stdout。** 这是 Claude Code 的 `tool_progress`
携带 Bash 分段输出的做法，也确实更有信息量。拒绝的原因不是它不好，是它**不在这次
的边界内**：MCP 的 `notifications/progress` 需要 `adapters/mcp/client.py` 支持通知
路由，而本仓库的 MCP 客户端目前只有请求/响应。它是一条真实的后续路径，记在
[已知缺口](../known-gaps.md)里，不假装已经做了。

**把 `ToolProgress` 变成 durable。** 那是每个跑着的工具每 5 秒一行 PostgreSQL 写
入，换来的是一份没人会回放的记录——调用怎么样了，`ToolCompleted` 已经说了，还带
`duration_ms`。这正是 `domain/events.py` 开篇说的 write-amplification。

**心跳里带 `percent`。** 见 §2.3。

**只做 handler 上报，不做时钟。** 那样这次改动只服务被改过的 handler，而空白最长
的那些调用里有好几个不是本仓库写的（MCP 工具）。时钟是唯一一个对所有工具都成立的
一半。
