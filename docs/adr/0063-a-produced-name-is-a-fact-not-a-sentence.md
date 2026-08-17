# ADR-063：产出的文件名是事实，不是句子

- 决策点：一次工具调用往工作区写了哪个文件，控制台从哪里知道；这条信息算
  「运行时正文」（受 `runtime.record_step_inputs` 门控）还是算结构化事实
  （无条件发布，与 `tool_name`、`output_bytes` 同级）
- 状态：**接受**。一次工具调用写进工作区的文件名是**结构化事实**，直接发布在
  `ToolResult.workspace_writes` 与 `ToolCompleted.workspace_writes` 上，
  **不进** `runtime.record_step_inputs` 门
- 日期：2026-08-17
- 影响：`domain/tools.py`（`ToolResult` 新增叶子字段 + `succeeded()` 同名
  关键字参数）、`domain/events.py`（`ToolCompleted` 同名字段，docstring 写明
  它为何不与 `output_preview` 同门）、`runtime/tool_gateway.py`（`_record` 里
  在门**外**赋值）、`adapters/tools/workspace.py`（write 与 edit 各报自己的
  名字）、`adapters/tools/sandbox.py`（报 `written`，即真正落盘的那些）、
  `adapters/tools/mcp_workspace.py`（把 MCP 产物绑进工作集时报那个名字）。
  **带默认值的叶子字段，不抬 `DOMAIN_SCHEMA_VERSION`**（先例见下）；
  持久化无迁移，但**落库的 JSONB 形状变了**（见 §6）；
  `tests/cli/golden/demo_tool_round.jsonl` 与
  `tests/domain/golden/domain_v1.json` 各多出一个 `"workspace_writes": []`
- 依赖：[ADR-019](./0019-run-step-transparency.md)（步骤输入是 opt-in——本 ADR
  划出它管辖范围的**外沿**）、[ADR-054](./0054-a-digest-cannot-be-consented-to.md)
  （摘要没法被同意）、[ADR-055](./0055-a-receipt-is-not-a-transcript.md)
  （`output_preview` 进同一道门）、[ADR-028](./0028-task-workspace.md)
  （名字可变、字节不变）、[ADR-061](./0061-thinking-is-process-not-product.md)
  （本 ADR 随附的控制台改动收窄了它的一条渲染规则，见 §5）

## 1. 背景：唯一的取名途径是解析一句散文

Code 会话跑完一轮，读者想知道的第一件事是**这一轮产出了什么文件**。今天要回答
它，只有两类办法，两类都不是契约：

**一、解析 `ToolProposed.argument_preview`。** 它是
`json.dumps(..., sort_keys=True)`（`domain/tools.py`），而 `workspace_write` 的
键序恰好是 `content < media_type < name`——`name` 排在最后。`BoundedText` 上限
4096（`domain/schema.py`），所以**正文一超过 4KB，名字就正好被截掉**。失效的
条件不是随机的，它精确地挑中了最值得展示的那些产物：大文件。

**二、解析散文。** 四句英文，分布在三个模块：两句在
`adapters/tools/workspace.py`，一句在 `adapters/tools/sandbox.py` 的
`_summary()`，还有一句在 `adapters/tools/mcp_workspace.py`——
"It is in the workspace as {name}."。**没有任何测试钉住它们的措辞**。把界面耦合
到一个谁都可以随手改写的句子上，是把渲染逻辑寄存在别人的自由里。

第四句尤其要点名，因为它是这条论证最硬的例证。`mcp_workspace.py` 的模块
docstring 里记着它存在的原因：一个 Word Task 的全部产物就是那份渲染出来的
`.docx`，评审却以「工作区是空的」判它失败——因为 manifest 从来没学会那个文件的
名字。**这个仓库唯一为「名字没被记下来」赔进去过一整个 Task 的文件类型，就是它。**
把它继续只挂在一句话上，等于在上一层重建同一个洞。

而且这两条路有一个共同的前提：`runtime.record_step_inputs` 得是开的。默认是
关的。也就是说，在默认部署里，这两条路一条都不通。

## 2. 决定：名字不是正文，所以不进那道门

`workspace_writes` 无条件发布，与 `tool_name`、`output_bytes`、`duration_ms`
站在一起，而不是与 `output_preview` 站在一起。

**判据是「披露了什么」，不是「属于哪一次调用」。** ADR-019 那道开关回答的问题
是「这个部署愿不愿意让**运行时正文**落进事件日志」——参数体、提示词、工具回答
的那段文本。这些东西的共同点是：它们复制了内容，而内容可能是这个部署不愿意在
日志里留副本的东西。

一个文件名不复制任何内容。**发出这次调用的 principal 本来就能列出整个工作区**
（`workspace_list` 工具，或 `GET /v1/code/sessions/{id}/workspace`，同一租户
同一 principal 作用域）。把名字写进事件，等于把一个他此刻就能查到的名字重复了
一遍——它不构成新的披露，所以那道门对它没有要保护的东西。

反过来说，**放进门里的代价是它会在最需要它的地方消失**：一个把 preview 关掉的
部署，正是唯一取名途径（解析 preview）已经失效的那个部署。「为了一致性也加上
门控」会让这个功能只存在于它最不必要的地方。

**与 ADR-054 的界线也要划清楚，因为它是另一种例外，不是同一种。** ADR-054 给
`PermissionRequested.approval_preview` 开的是**无条件复制正文**的例外，理由是
那一处的读者正被要求**同意**，摘要没法被同意。那是一张很贵的票：它承认正文入
库，只是主张此处非如此不可。本 ADR 不需要那张票，因为它**根本没有复制正文**。
两者的关系是：ADR-054 在门内破了一个例外，本 ADR 说明有一类字段从来就不在门的
管辖范围内。

一句话：**门管的是内容，名字不是内容。**

## 3. 被拒绝的方案

**客户端按轮次做工作区列表差分。** 每轮结束前后各拉一次
`GET /v1/code/sessions/{id}/workspace`，多出来的名字就是这一轮的产出。零后端
改动，也确实能在**正在看的那一次**给出正确答案。拒绝的理由是它的答案活不过一次刷新：
差分只对页面恰好在场的那一轮成立，重新打开页面，归属信息就蒸发了——而持久事件
日志的全部意义，正是让一个重开的页面能把它重建出来。把一个可以持久的事实做成
只存在于某个标签页内存里的推断，方向是反的。它还会误报：同一轮里被 `edit`
改写的文件名字不变，差分看不见它，而那次调用确实产出了那个文件。

**给 `StoredMessage` / `MessageView` 加 `run_id`。** 让消息能反查那一轮的事件，
从而顺出文件名。它需要一次持久化 schema 变更：一支 Alembic 迁移，加上
`tests/contracts/` 那套参数化套件对 in-memory 与 PostgreSQL **两个实现**各跑
一遍。而本 ADR 的改动是一个带默认值的领域叶子字段——不动库、不动迁移、不动
契约套件。为一件能在领域层解决的事付一次持久化迁移的价钱，代价与收益倒挂。

**给工具输出定一个可解析的句式**（"WROTE: report.md"）。省掉字段，但它把契约
定义在字符串格式上，而字符串格式的唯一执行者是写下它的人的记性。`output_preview`
本身还受门控，所以这个方案连门的问题都没解决。

## 4. 刻意不做：按轮次寻址字节

**产出卡片给读者看的是那个文件名此刻的字节，不是那一轮当时的字节。** 同一个
名字后来被改写，卡片跟着变。这是已知代价（known-gaps F-13），不是缺陷。

修它需要一条**按轮次寻址**的读取路径——用「哪一轮」定位工作区的哪个版本，再从
那个版本读名字。而工作区版本是一个 artifact id，
`tests/architecture/test_a_workspace_version_is_never_asked_for.py` 的存在就是
为了让这个入口关着：它扫描 `apps/api/routes/*.py` 的路由参数与请求体字段、以及
`adapters/tools/*.py` 的工具 schema 属性名，任何一处出现
`workspace_version` / `manifest_id` / `workspace_manifest` 就失败。那份 docstring
把理由写在了前面：读写只按租户与 principal 划界，再没有更细的了，所以一个能
点名版本的 principal 可以点到**他自己另一个会话**正在中途改的工作集，读它或者
覆盖它。「今天不可达，是因为没有入口接受它」——这句话是本条缺口不修的全部理由。
真要做，得先回答那个授权问题，那是另一份 ADR。

另一处刻意留下的边界：**部分失败的 `sandbox_run` 只在错误消息里说落了哪些
文件。** 那条路径返回 `ToolResult.failed`，网关据此发的是 `ToolFailed`——那个
事件里根本没有 `workspace_writes` 字段。要让结构化事实覆盖失败方向，就得同时
改 `ToolFailed`，那是第二个决定，不是这个决定的细节。
`tests/adapters/test_sandbox_tool.py` 里有一条测试把这个限制钉住，免得后来的人
把它当 bug 修一半。

## 5. 随附的控制台改动收窄了 ADR-061 的一条规则

ADR-061 已经决定了**哪些界面显示摘录**（它明确记下 Chat 不渲染思考），但没有
规定在一个显示它的界面**内部**该摆在哪。随附的控制台改动把后者收窄一格：**Code 的思考摘录只出现在按轮次的折叠块里，不再进步骤树。**理由
是步骤树是「这一轮做了哪些事」的清单——工具调用、产出的文件——而摘录是模型对
整轮的推理，把它塞进某一个步骤节点，等于把一段跨步骤的文字挂在一个它并不从属
的节点上，读者会以为那是这一步的推理。

**Work/Task 一侧不变**：Worker 是独立进程，`LiveEventChannel` 只在进程内扇出
（ADR-051 的形状），Task 没有 live 通道，摘录是它唯一能显示的思考——那不是选择，
是架构事实（ADR-061 §2 已记）。收窄只作用于有实时通道的那一侧。

## 6. 版本与兼容：不抬版本，但方向不对称

**不抬 `DOMAIN_SCHEMA_VERSION`，先例是 [ADR-035](./0035-event-schema-and-upcasters.md) §4**，
不是 ADR-042 或 ADR-061——那两份记的都是 `config_schema_version`，是另一个版本号。
ADR-035 给的还不止是先例，是机制：`VersionedModel.reject_unsupported_schema_version`
要求**严格相等**，所以抬版本会让**每一条历史 payload 立刻读不出来**，直到为每个
事件类型都注册好 upcaster。也就是说版本号在这里根本不是兼容杠杆，抬它是净损失。

**两个方向不对称，必须说清楚。**

- **新代码读旧行：安全。** 字段有默认值 `()`，旧 payload 原样通过校验，不欠
  upcaster——这与 `DEFAULT_EVENT_UPCASTERS` 至今为空是一致的。
- **旧代码读新行：会被隔离。** `DomainModel` 是 `extra="forbid"`，
  `event_log` 在读回时重新校验，所以一个还没升级的进程遇到带
  `workspace_writes` 的 `ToolCompleted` 会抛 `ValidationError`，那一行进入
  quarantine（有计数，不是静默丢弃）。**滚动升级要先升读的一侧**——这条规则是
  ADR-035 §4 定的，本 ADR 沿用。

顺带更正一处措辞：本次改动**确实触及持久化**——落库的 JSONB payload 形状变了。
它不需要迁移（`events.payload` 是 JSONB，不是定型列；`tool_executions` 从不存
序列化的 `ToolResult`；`ToolResultBlock.from_tool_result` 也没有带上新字段，
所以 `messages.payload` 逐字节不变），但「不触及持久化」是错的说法。

## 7. 证据

- `tests/runtime/test_tool_gateway.py::test_a_produced_filename_survives_a_deployment_that_records_no_previews`
  ——`record_step_inputs=False` 时字段照样有值，且同一断言里钉住
  `output_preview` 与 `argument_preview` **都是空的**，防止将来有人把它挪进门里
  再把门打开、让这条测试为了错误的理由变绿。
- `tests/adapters/test_workspace_tools.py`：write 与 edit 各报自己的名字；被拒的
  write 与 edit 报空（返回发生在 `session.version` 前进**之前**，所以空是构造出
  来的，不是记得清的）；read 报空。
- `tests/adapters/test_sandbox_tool.py`：一次多产物的运行按写入顺序全报；只计算
  不落盘的运行报空；部分失败的运行报空且名字只在错误消息里（§4）。
- 两份 golden 重新生成，`git diff` 上各只多一行 `"workspace_writes": []`。
