# ADR-025：MCP 工具在启动时冻结成本地绑定，schema 不合规的被丢掉而不是被迁就

- 决策点：`optional_labs.mcp_adapter` 的真实实现；第三方工具怎么进静态注册表、怎么命名、
  怎么过 schema 校验、怎么进 Task 授权信封
- 状态：**接受**
- 日期：2026-08-09
- 影响：新增 `adapters/mcp/`；`config_schema_version` 1.6 → 1.7；新增 `[mcp]` 配置段；
  `task_authorization_envelope` 增加一个参数；`SUPPORTED_KEYWORDS` **不变**

## 背景

架构基线第 48 行把 MCP Adapter 列为 Optional Lab，`optional_labs.mcp_adapter` 从
WP00 起就是 `false`，`adapters/mcp/` 目录从未存在。基线第 1132 行只留下一句约束：
"所有 MCP Tool 仍必须进入统一 Tool Gateway"。

这句话是对的，但它没说清代价。把它当成实现说明去写，会在四个地方卡住，而且每一处
卡住的方式都不一样：

| 卡点 | 现状 | MCP 那边是什么样 |
|---|---|---|
| 工具名 | `ToolName` = `^[a-z][a-z0-9_]{0,63}$` | 任意字符串，两个 server 可以都叫 `search` |
| 输入 schema | `assert_schema_supported` 只认 17 个关键字，不认的**在装配时抛异常** | 真实 server 大量使用 `oneOf`/`$ref`/`format`/`const` |
| 风险与并发 | `ToolSpec` 强制：非 read ⇒ `exclusive` + 至少一个 permission scope | 协议不描述风险，也不描述幂等 |
| 注册表 | `StaticToolRegistry` 一次装配、终生不变 | `tools/list` 是运行时 RPC，随时可变 |

第二行是真问题。`schema_validation` 模块的开头写着它的设计意图：

> Silent under-validation is the failure mode worth designing against.

对自研工具，"不认的关键字就炸进程"是对的——schema 是本仓库写的，炸了就去改。对
MCP 不成立：schema 来自第三方进程，可以在我们没有部署任何东西的情况下变。照搬这条
规则的结果是**远端改一行 schema，worker 起不来**。

而反过来放宽 `SUPPORTED_KEYWORDS` 去迁就第三方，正好掉进模块自己警告的那个坑：
加了 `oneOf` 却不实现它的语义，每个调用都会报告"有效"而实际上什么都没校验。

## 决策

### 一、发现只发生在启动时，注册表仍然是静态的

bootstrap 阶段对每个配置的 server 调一次 `tools/list`，把结果冻结成 `ToolBinding`
装进 `StaticToolRegistry`。进程运行期间不再问第二次。

`StaticToolRegistry` 的 docstring 已经给出理由，这里原样适用：一个能中途长出工具的
注册表，会让"当时有哪些工具可用"对已经写完的事件流变成无法回答的问题。远端 server
在我们运行期间加了工具，本进程视而不见；要它生效就重启——这跟改配置要重启是同一件事。

**传输只支持 HTTP。** `stdio` 意味着派生一个本地子进程，那是与"调用一个服务"不同的
威胁模型——文件系统、环境变量、进程生命周期全都变成这个功能的一部分。本 ADR 没有决定
它，所以 `transport` 是单值 `Literal["http"]`：要加 stdio 得改这里，而不是改配置文件。

**server 在启动时连不上，进程照常启动，只是不带那些工具。** 这不是新发明的容错策略，
是 `embedding_factory` / `reranker_factory` 已有的做法："why this process has no
embedder, in words somebody can act on"。MCP 照抄：进程报告它没能建起什么，然后继续
提供它能提供的东西。

### 二、名字重整为 `mcp_<server>_<tool>`，且 `<server>` 是本地别名

`ToolName` 的正则不放宽。适配层把远端名字重整进这个形状，冲突用 server 段消解。

`<server>` **取配置里我们自己写的别名，不取远端自报的 server name**。远端自报的名字
是远端可以改的，而这个名字会进三个不可回溯的地方：事件流、副作用账本的 key、Task
授权信封。这些东西的稳定性不能托付给第三方进程。

重整后仍然非法的（撞名、超长、含无法映射的字符）→ **丢掉这一个工具**，记结构化日志，
不影响同一 server 的其他工具，更不影响进程启动。

### 三、schema 不合规的工具被丢掉，`SUPPORTED_KEYWORDS` 一个字不加

适配层在造 `ToolBinding` **之前**自己先跑一遍 `assert_schema_supported`。不过的工具
不进注册表。

于是同一个函数在两条路径上有两种后果，而这正是想要的：

- **自研工具**不合规 → 抛异常、进程起不来。schema 是我们写的，这是 bug。
- **MCP 工具**不合规 → 跳过它、其余照常。schema 是别人写的，这是现实。

校验器本身不动。它继续是那个"不认就拒绝"的小子集，没有为了兼容谁而变松。

**这一条的代价必须说清楚：真实 MCP server 会有相当比例的工具进不来。** `oneOf`、
`$ref`、`format`、`const`、`patternProperties` 在生态里很常见。第一版接受这个代价，
理由是两害相权——"能用的工具少"是看得见的、可以逐个排查的；"校验形同虚设"是看不见
的，而且要到某个工具收到畸形参数时才会以别的面目出现。

后续若要提高覆盖率，正确做法是**在校验器里真正实现某个关键字的语义**（那是一次
独立的、有测试的改动），而不是在 MCP 这一侧开一个"信任远端 schema"的旁路。

### 四、一律 `risk="external"`、`concurrency="exclusive"`、scope 为 `mcp:<server>`

协议不描述风险，所以不猜。`ToolSpec.validate_risk_consistency` 要求非 read 工具必须
exclusive 且至少一个 permission scope，按最保守的那档填满。

代价是**所有 MCP 工具串行**，拿不到并行工具调用。诚实记下来：这是"不猜风险"的直接
后果，不是实现偷懒。哪天协议或配置能可信地表达"这是只读的"，再单独开 ADR 放宽。

### 五、第一版只接**远端副作用可重复**的工具，由配置逐 server 显式声明

`ToolBinding` 的 `operation_key` 是副作用账本的入口，它要求一个能从调用推出来的稳定
业务键。MCP 不提供这种东西。

没有账本的写工具意味着：重试会在远端产生第二次真实副作用，而我们无从知道第一次成不
成功。所以第一版划一条线：

- **我们这边的写**（把返回的 resource 存进 artifact store）可以记账，key 从
  `task_id` + `argument_digest` 推，与 `export_artifact` 同一套路；
- **远端的副作用**我们既管不了也记不了账。因此配置里每个 server 必须显式声明
  `retryable_effects = true|false`，声明为 `false` 的 server，其工具不进 Task 图。

不设默认值。忘了写就启动失败——这比默认成任何一边都好，因为两边猜错的后果都不对称。

### 六、授权信封增加一个配置驱动的变体，存的是解析后的具体工具名

`task_authorization_envelope(*, external_search)` 增加一个参数，返回的信封里
`allowed_tools` 含**逐个列出的** MCP 工具名，不是通配。

理由和 ADR-020 给 `external_search` 的理由是同一条，原文就在 `ResearchSettings` 的
docstring 里：信封随 Task 存盘、每次 resume 重放，所以一个从没开过这个功能的部署，
不能因为升级就让历史 Task 的权限变宽。通配符会正好造成这个后果。

**Task 提交后运维改了 MCP 配置怎么办**：resume 时信封里的名字可能已经不在注册表里。
这条不需要新机制——`ports/tools.py` 已经写明"unknown tool is not an exception"，
gateway 对未知工具返回一个说明拒绝原因的 `ToolResult`。既有语义正好兜住。

## 后果

- `config_schema_version` 1.6 → 1.7，新增 `[mcp]` 段，字段按规矩登记进
  `config/ownership.yaml`（owner `bootstrap.adapter_factory`，lifecycle `startup`）；
- `optional_labs.mcp_adapter` 从"占位开关"变成"真的有实现的开关"，默认仍为 `false`；
- 所有 MCP 工具串行，Task 的并行工具调用只在自研工具之间发生；
- 启动日志多一类结构化记录："哪个 server 的哪个工具因为什么被跳过"。这是这个功能
  最主要的可运维面，不是调试输出，要当成产品的一部分写；
- **Word 文档这件事被这条路顺带解决，但不是自动解决**：MCP 侧产出的 `.docx` 要能落地，
  依赖 `ToolResult.artifact` 这条既有通道（`ArtifactRef.media_type` 的正则已经容得下
  OOXML 的媒体类型）。但 Task 图终点的 `export_artifact` 仍然把媒体类型和文件名写死成
  `text/markdown` / `report.md`。**让最终报告本身变成 .docx 是另一件事，需要单独的 ADR。**

## 备选方案

**动态注册表，运行时热更新工具清单。** 基线第 94 行已经把"MCP OAuth 全流程和热更新"
划在 v1 之外。除了排期，它跟事件流的可回答性直接冲突：见决策一。

**放宽 `SUPPORTED_KEYWORDS` 到覆盖常见 MCP schema。** 被否决的不是工作量，是方向——
在不实现语义的前提下接受关键字，等于把校验器变成装饰。真要提高覆盖率就实现语义。

**给 MCP 工具一个"信任远端 schema、跳过本地校验"的旁路。** 同上，而且更糟：它把
"哪些调用被校验过"变成了逐工具的配置问题，gateway 就不再是"唯一能拦住工具的地方"。

**按远端自报的 server name 做命名空间。** 省一个配置字段，代价是把事件流和账本 key
的稳定性交给第三方。不换。

**第一版就支持远端写操作，账本 key 用 `tool_call_id` 推。** 这正是 `OperationKeyFor`
的注释点名反对的做法："deriving it from `tool_call_id` would defeat the whole point"
——重试会 mint 新的 id，于是每次重试都像新工作。
