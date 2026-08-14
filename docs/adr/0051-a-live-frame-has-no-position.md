# ADR-051：实时帧没有位置，所以它不许有 id

- 决策点：进程内的 transient 事件（`ModelDelta`、`ToolProgress`）该不该到达浏览器；
  如果该，它们和"只有 durable 事件有游标、断线可续读"这条契约怎么共存；实时通道
  被慢读者拖住时是断开、是无界缓冲、还是别的
- 状态：**接受**
- 日期：2026-08-13
- 影响：新增 `apps/api/sse.py` 的 `LiveEventChannel` / `LiveSubscription`；
  `ApiDependencies.sink_for` 返回 `ObservingEventSink`；
  `event_stream.subscriber_buffer_events` 补上界并换 owner，新增
  `event_stream.max_live_subscribers_per_stream`。**durable 回放的字节、游标语义、
  隔离读、quarantine 帧一律不变**；配置 schema 保持 `1.14`（沿用 ADR-042 的先例）
- 依赖：ADR-019（事件流里记什么）、ADR-035（答案不是摘要）

## 1. 背景：CLI 早就能逐字，浏览器不能

`domain/events.py` 从第一天起就把 durability 写成事件类型的属性而不是调用方的选择，
并且明说 transient 事件"stream to a subscriber and are never written to PostgreSQL"。
`adapters/events.py` 的 `ObservingEventSink` 也早就写好了，docstring 直说它的用途是
"lets a terminal **or an SSE connection** show token deltas"。

但那一半从来没接上。API 的订阅端点（当时的 `routes/events.py`）只有一条数据源：
按 `(stream_id, sequence)` 从 `run_events` 回放。它每 `catchup_poll_seconds` 查一次库，
把查到的行变成帧。transient 事件不在库里，于是**在这条路径上没有出口**。

结果是 CLI（`apps/cli/rendering.py` 的 `TextRenderer`）能逐字显示回答，
而浏览器只能等答案整段出现——`docs/frontend-design.md` 当时写的
"durable stream 不包含 token delta……不声称逐 token streaming"是对现状的诚实描述，
不是一个设计目标。

## 2. 为什么不是"给 transient 也发一个 id"

因为 id 不是标识符，是**位置**。

SSE 的 `id:` 字段决定浏览器下次重连时 `Last-Event-ID` 发回什么，而这个项目把它
直接定义成 `EventCursor(stream_id, sequence)`。一条 transient 事件没有 sequence——
`EventEnvelope.validate_envelope` 强制"durable 才有 sequence，transient 一定没有"——
所以给它编一个 id，等于把客户端的游标指到日志答不出来的地方：重连时
`resume_from` 要么解不出、要么解出一个不存在的位置。

反过来，SSE 规范给了一条现成的、正确的语义：**没有 `id` 字段的帧不改动 last event id**。
一条实时帧因此可以合法地存在于同一条连接上，而完全不参与续读。

这不是"我们决定不填 id"，而是"填了就是错的"。所以它被做成结构性的：写 id 行的函数只有
两个（`frame_for` 与 `_quarantine_frame`），两个都**必须**收一个 sequence 参数；
`_transient_frame` 与 `degraded_frame` 连能传游标的参数都没有。一个共享的、id 可选的
builder 会把这条规则挪到调用方的参数表里，那是下一个调用方需要记住的地方。

## 3. 决定

### 3.1 实时通道是进程内的，而且这一点要说出口

`LiveEventChannel` 只把事件发给**同一个进程**里的订阅者。这不是一个施工阶段，是
transient 事件的定义决定的：它不写任何地方，所以它到不了别的进程。

后果必须诚实说明：**Task 跑在 worker 进程里，因此 Work 的订阅拿不到实时文字**，
而且是静默地拿不到。这不叫降级——底下的 durable 回放一条不少、一条不晚，
那条流不是残缺的，它只是不实时。要让它实时，需要的是一条跨进程的 transient 传输，
那是另一个决定，不在这里做。

### 3.2 实时不给数据库添一次查询

帧是在轮询循环**本来就要睡的那段时间**里送出去的（`_live_window`），
而不是"来一条 delta 醒一次循环"。后者看起来更直接，实际是把一个快模型变成
PostgreSQL 上的负载源：一轮两千条 delta 乘以 M 条订阅，就是 M×2000 次多余的
`read_isolating`。`LiveSubscription` 在类型上也拿不到 `EventLogPort`——
一个能够到日志的订阅，就是一个能把 token 变成查询的订阅。

`live` 缺省时，`stream_events` 产出的字节与这个参数存在之前**逐字节相同**。
这是第二条订阅路由（Task、Code）能复用它、且客户端分不出两条路由的前提。

### 3.3 缓冲有界，溢出丢最旧的，而且要说

生产者是 emit 路径上的一个同步回调，它**不能**等浏览器——让它等，就是让读者的
节奏决定运行的节奏。所以没有背压，只有缓冲；没有上界的缓冲，就是把一个慢标签页
变成进程里的无界内存。

溢出丢**最旧**的一条：最新的 delta 才是还在描述运行状态的那条，丢最新会留下一段
陈旧前缀并让订阅者越落越远。丢掉的条数不吞掉，它变成一条 `stream.degraded` 帧——
"少了一段实时文本"和"完整"不允许在界面上长得一样。

这一条**收窄**了架构基线里"subscriber 缓冲溢出即断开"的说法：断开会连带毁掉那条
连接上完全健康的 durable 回放，而丢掉的东西本来就是不可回放的。

### 3.4 到顶时拒 429，而且在流开始之前

每流订阅数有上限（`max_live_subscribers_per_stream`，默认 4）。到顶时在构造
`StreamingResponse` **之前**抛错，映射成 429。

理由是：只能在流开始之后拒绝的订阅，只能用一个帧来拒绝，而客户端分不出
"被拒绝了"和"流结束了"。而且那时缓冲的代价已经付了。

`subscriber_buffer_events` 原本只有 `ge=1`。它和新上限一起决定了 API 进程为
"不读的读者"最多持有多少，任何一个开着口，都能把一个配置笔误变成 OOM。

## 4. 后果

- Chat 的订阅现在同时承载两种东西，客户端必须能分辨：带 id 的是历史，没有 id 的是
  现在。前端的帧解析器要为此长出第三、第四个分支（transient 与 degraded），
  且必须强制"transient ⟺ 无 id ⟺ sequence 为 null"。
- CLI 会收到 transient 帧。`stages.py` 的 `chat_stage_of("ModelDelta")` 返回 None，
  于是它被丢弃——CLI 是唯一"收到却不观察"的界面，这是有意的。
  它那条"游标跨帧沿用"的既有行为在 id-less 帧下依然安全：消费端只赋非空游标，
  所以沿用只会把游标写回同一个值（`tests/cli/test_repl.py` 钉住了这一点）。
- `sink_for` 返回类型从 `ScopedEventSink` 变成 `EventSink`。tee **只**加在这一个
  工厂上：恢复 reaper 只写 durable 终态事件，triage 跑在一份 per-call 的内存日志上
  没有人能订阅，给这两个 tee 等于给一条同步提交路径挂一个扇给零个人的回调。
- 实时通道**在发布围栏的里层**。`ChatService` 把拿到的 sink 包进 `AnswerReleaseSink`，
  抹除发生在外层，所以观察者看到的必然是已经过闸的 payload。哪些形态可以放行正文
  是另一个决定，见 ADR-052。

## 5. 重审条件

- 有人需要 Work 或 Code 的实时文字跨进程到达浏览器时，重审 §3.1。那要求 transient
  事件有一条跨进程传输；当前协调面把 `LISTEN/NOTIFY` 限定为"只唤醒"，所以那是一条
  新的架构主张，必须自己写 ADR，不能在这一条下面加个开关。
- 一条订阅同时服务多个租户或多个会话时，重审 §3.4 的"每流上限"——它今天守的是内存，
  不是隔离。
