# ADR-077：在这台机器上跑的命令，跑之前先给人看

- 决策点：Code 会话已经能读写用户机器上的真实目录（ADR-072、ADR-074），却不能在
  那个目录里跑任何东西。仓库里唯一的「执行」是 `sandbox_run`——一次调用建一个
  断网容器、文件进文件出、调用之间无状态——而 ADR-057 恰恰是为了说清那**不是**
  shell 才把 `code.shell_enabled` 改名成 `code.sandbox_enabled` 的。要不要补上它
  当年划开的另一半；如果补，闸门放在哪一层，以及**人凭什么批准它**
- 状态：**接受**。新增 `project_run`：`destructive` 风险、每次调用都停下问人、
  只进 Code 的项目侧、由 `policy.shell_tools_enabled` 闸住（该字段从冻结
  `Literal[False]` 解冻）。同时修掉一个比工具本身更要紧的先决缺陷：**批准卡片
  此前只显示参数摘要**，本 ADR 把命令本身送到人眼前。**明确不做**：Task 拿不到
  它，扁平工作区的回合拿不到它，没有「本会话都允许」
- 日期：2026-08-24
- 影响：新增 `adapters/tools/project_files.py::ProjectRunTool`、
  `bootstrap/child_environment.py`；`domain/project_files.py` 新增
  `PROJECT_RUN_TOOL` 与 `PROJECT_RUN_SCOPE`；`ports/project_files.py` 新增
  `working_directory`（`FilesystemProjectFileStore.root` 随之改名）；
  `ports/approval_gate.py::InteractiveApprovalGate.request` 新增
  `approval_preview` 参数；`application/code_approvals.py` 的 `_Pending` 与
  `PendingApproval`、`apps/api/routes/code.py` 的 `PendingApprovalView`、
  `web/src/api/types.ts` 与 `CodePage.tsx` 的批准卡片跟着带上它；
  `application/code_session.py` 新增两个工具元组、`code_risk_ceiling()` 与
  `_system_prompt_for()`；`application/code_prompt.py` 新增
  `with_host_commands()`；`bootstrap/projections.py::CodeConfig` 新增
  `host_commands_enabled`；`web/src/app/IdentityContext.tsx` 新增
  `project:run` 并把存储键 `v4` 抬到 `v5`。新增
  `tests/bootstrap/test_child_environment.py`，`tests/adapters/test_project_tools.py`
  增 `TestRunningACommand`。**配置 schema `1.17` → `1.18`**：
  `policy.shell_tools_enabled` 的取值范围变了，一份 1.17 的配置无法要求本版能
  要求的东西。**运行时、`ToolGateway` 的派发路径、账本、`ExecutionLease`、
  Task 的任何一条路径一律不动**
- 依赖：[ADR-057](./0057-a-pure-function-is-not-a-shell.md)（本 ADR **兑现**它
  留下的空位：它退掉了「不是 shell 的那个面」的名字，从未有人做「是 shell 的那个
  面」；**不推翻**它关于沙箱是纯函数的判断）、
  [ADR-058](./0058-the-sandbox-gate-moves-from-the-human-to-the-envelope.md)（本 ADR **收窄**它
  「摘要不能被同意」的结论：该结论对沙箱成立，对参数即效果的工具不成立，所以
  修的是卡片而不是闸门）、
  [ADR-072](./0072-a-project-is-a-directory.md) 与
  [ADR-074](./0074-a-project-is-where-code-happens.md)（**扩展**它们：目录已经
  是用户的真实目录，本 ADR 只是让那个目录里能发生别的事）、
  [ADR-075](./0075-a-ledgered-effect-is-issued-not-proposed.md)（**不再适用**
  于 Code 这一个面——它整套论证建立在重放之上，而 Code 的回合不可恢复；它对
  Task 的拒绝一字不动）、
  [ADR-070](./0070-a-permission-is-about-a-window-not-an-application.md)（**不
  触碰**：屏幕控制仍在它自己的 MCP 进程里，和本 ADR 无关）

## 1. 背景：被退掉的名字，和没人去做的那个面

ADR-057 处理的是一次命名错误。`code.shell_enabled` 这个字段的注释把「给一个
shell」和「授予 `sandbox_run`」当成同一件事，而 ADR-029 的沙箱是纯函数：
`--network=none`、`--read-only`、`--user=65534`、`--cap-drop=ALL`、一次调用一个
容器、`--rm` destroy。那次改名是对的。

但它留下一个洞，而且是**结构性**的洞：仓库从此没有任何东西叫 shell，也没有任何
东西**是** shell。Code 会话能读、能写、能改用户机器上的真实文件（ADR-072、
ADR-074），唯独不能在那里跑一次 `pytest`。一个能改代码却不能运行代码的编码
agent，只能对着自己的输出讲道理——`CODER_SYSTEM_PROMPT_WITH_SANDBOX` 的注释里
记着这个失败的实测版本：一次回合写出了正确的 `fib.py`，然后报告
「本环境没有 shell，我无法实际执行该 Python 文件，以上输出是根据代码逻辑推断的」。

同时，`policy.shell_tools_enabled` 一直在配置里躺着，写着 `Literal[False]`。
2026-08-24 实测：它在 `src/` 里的消费者数量是**零**。`config/config.default.toml`
有它，`config/ownership.yaml` 有它，`docs/configuration.md` §3 把它列为「错误的
环境覆盖会让进程启动失败」的不变量之一——而没有一行代码读它。它是配置对自己说
的一句话，读起来像保证，实际是注释。这是最贵的一种不变量：下一个人会信它。

## 2. 决策：卡住的不是要不要跑，是人凭什么答应

真正拦住这件事的从来不是「能不能派发一个子进程」。是**批准**。

`sandbox_run` 是 `external` 风险，`code.sandbox_requires_approval` 决定它停不停。
ADR-058 把这个默认改成了 `False`，论证是：

> ADR-054 已经确立，批准卡片显示的是一个工具名和一个参数摘要——**摘要不能被同意**，
> 所以这道闸门从来没买到知情同意，只买到延迟。

这个论证对沙箱是对的。一个断网、只读、用完即毁的容器，它的爆炸半径不需要看参数
就能说清：无论脚本写了什么，后果都被容器本身框住了。**参数是效果的细节。**

对一条在你机器上跑的命令，这句话反过来了。`rm -rf build` 和 `ls` 的爆炸半径完全
由参数决定，容器不在了，框住它的东西只剩「那个人读没读」。**参数就是效果。**
所以：

- 不能选 `external`。`code.sandbox_requires_approval` 默认 `False`，一个 `external`
  的 host 命令**默认不经批准就跑**。选 `destructive`：`code_session.py` 里它在
  每一个 Code 信封中无条件武装，和那个开关无关。
- 也不能沿用 ADR-058 的结论去掉闸门。它的前提在这里不成立。
- **但那句「摘要不能被同意」当时是对的，而且到今天仍然是对的**——所以要修的是
  卡片，不是闸门。

### 2.1 先决缺陷：预览一直在，只是没人接

查下来的事实比预想的好一半又坏一半。`ToolGateway` **早就**无条件构造了预览：

```
runtime/tool_gateway.py:603   approval_preview=_approval_preview(canonical)
```

`_approval_preview` 的文档字符串甚至点名了要害——「被截掉而没有标记的预览比一个
短的更糟：批准的人会把它读成完整的请求，而看不出**那个重定向、那个第二路径、
那个 `--force`** 是被长度限制拿掉的，不是本来就没有」。上限
`APPROVAL_PREVIEW_LIMIT = 2048`，截断处标 `...[truncated]`。

坏的一半是：**它到不了任何人眼前。** Code 会话的批准问题走的是
`application/code_approvals.py` 的注册表，而 `PendingApproval` 只带
`argument_digest`；控制台读的是注册表，不是那条事件。2026-08-24 实测：
`web/src/` 里 `approval_preview` 命中数为 **0**。

所以本 ADR 的第一件事不是加工具，是把这条已经存在的信息接到端点：
`InteractiveApprovalGate.request` 加一个 `approval_preview` 参数，`_Pending` 与
`PendingApproval` 带上它，API 的 `PendingApprovalView` 带上它，卡片渲染它。

摘要**留着**，位置降到预览下面。两者回答不同的问题，而且只有一个能被读：摘要是
standing rule 的键（`ports/approval_gate.py` 论证了为什么必须是它），预览是人要
同意的那句话。预览**永远不得**用于匹配 standing rule——两次不同的调用一旦被截断
就可能共享同一个预览，而摘要不会。

### 2.2 闸门放在 `[policy]`，而且只有一个

`policy.shell_tools_enabled` 从 `Literal[False]` 解冻成 `bool`，默认 `False`，
成为唯一开关。三个理由，第三个是决定性的：

1. 它让那句已经写在配置里九个 schema 版本的话**第一次变成真的**。开着还是关着，
   配置说的都是实情。
2. 不再加第二个开关。`code.*` 的孪生字段会是「用两种方式描述同一个决定」，而这类
   配对最有意思的 bug 是两者不一致。
3. **`policy_fingerprint` 会哈希 `[policy]` 段的每一个字段**（除 `revision`），
   而 `policy_identity` 进每一次运行的记录。实测 2026-08-24：`config.code-local`
   的 `policy_identity` 是 `policy-v1:b8d1414911cc29e7`，`default`／`local`／
   `demo-local` 全是 `policy-v1:0e67f8dd84919551`。也就是说，「这次运行跑在一个
   允许驱动本机的部署上」是一件**事后可查**的事，而不是要去翻当时的配置文件。
   `code.sandbox_enabled` 不在这个指纹里，沙箱也不需要在。

### 2.3 Code 可以，Task 不可以，而理由不是「以后再说」

ADR-075 拒绝屏幕工具进 Task，整套论证建立在**重放**上：`operation_key` 在模型
提议的调用上推导不出来，参数派生的键会吞掉合法的第二次点击，位置派生的键在
epoch 3 位置 5 / epoch 4 位置 6 那个走查里会**真的再点一次**。

那套论证在 Code 上一个字都不适用，因为 Code **没有重放**：

> `application/code_session.py`：**A turn is not recoverable.** No lease to
> expire, no `release_pending` to finish, nothing half-written to reclaim.

没有租约、没有纪元、没有检查点，进程死了回合就没了，用户把那句话再说一遍。所以
「同一个意图被重放」这个问题在这里不存在，`operation_key` 也就不需要——
`ProjectRunTool.binding()` 不带它，这既避开了 ADR-075 的 `advertise` 护栏（它
拒绝把任何带键的绑定给模型看），也避开了 Code 网关根本没有账本这件事（带键的
Code 工具会让 API 进程装配不起来）。

反过来，Task 侧的拒绝**不是**沿用 ADR-075，而是有自己的、更简单的理由：Task 的
运行发生在 Worker 里，而决定是在 API 进程里做的，所以那条路上**没有闸门可问**
（`ports/approval_gate.py` 把这写成了一句声明而不是缺口）。一个每次调用都必须
停在人面前的工具，在一个没有人可停的进程里，只能是拒绝。`project_run` 因此从不
进入任何 Task 授权信封——它不在 `projections.py` 的构造函数里，也没有任何 profile
写它的名字。

### 2.4 提示词必须描述它真正所在的世界

`code_prompt.py` 里 "project" 出现 **0 次**。也就是说今天一个项目目录的回合被
告知「你的工作集不是文件系统……没有 shell、没有网络、没有通向外面的路径」，而
这三句对它全是假的。这是 ADR-072／073 留下的既有缺口，本 ADR 不修它的全部（见
`docs/known-gaps.md` F-23），但**必须**修与本工具相关的那一句：一个手里握着
shell、却被告知没有 shell 的模型，会做出对另一个部署正确的行为——这正是
`CODER_SYSTEM_PROMPT_WITH_SANDBOX` 从实测里学到的那一课。

`with_host_commands()` 因此是一次**组合**而不是三个新常量：`project_run` 与沙箱
及其闸门相互独立，写成六份文本就是六处要手工保持一致的地方。它对两种拼法的
「没有 shell」断言做替换，并要求**恰好命中一条**，所以将来任何一份基底提示词被
改动，都会在 import 时炸掉，而不是发出一个握着 shell 却以为自己没有的回合。

## 3. 环境：唯一一件沙箱不需要想、这里必须想的事

容器继承不到任何东西，因为容器里本来什么都没有。`project_run` 以 API 进程自己的
用户身份运行，而命令是模型写的——`env` 是在项目里看看情况时再正常不过的一条命令。

`bootstrap/child_environment.py` 因此把整个 `AW_*` 命名空间从子进程环境里摘掉：
`AW_SECRETS__DEEPSEEK_API_KEY` 是 provider key，`AW_DATABASE__DSN` 和它的两个
兄弟是连接串（`settings.py` 把连接串当作凭据，即使今天这条没有密码），
`AW_KEY_FILE` 指向存着前者的文件。是命名空间而不是清单，因为清单要在每次新增
设置时被一个**正在想那个设置、而不是正在想这个函数**的人记起来。

它住在 `bootstrap/` 不是偏好：`tests/architecture/test_dependency_boundaries.py`
只允许 `bootstrap` 读 `os.environ`。这条规则是对的，而且落在这里刚好——决定一个
子进程能看见什么，本身就是一次配置决定。

**其余一律不擦，这同样是决定。** 在别人项目里跑的命令本来就该看见他们的 `PATH`、
他们的工具链、他们的 `SSH_AUTH_SOCK`。一个够不到 agent socket 的 `git push` 是
坏掉的工具，不是安全的工具；一个已经能跑命令的模型，不会因为让它跑不动而变安全。
不该给它的只有这个平台自己的配置——那是环境里唯一一样不是操作员为项目准备的东西。

## 4. 被拒绝的方案

**把 `project_run` 声明成 `external`，和 `sandbox_run` 同一档。** 名义上说得通
——「内容离开本进程去了外面的执行环境」对两者都成立。按后果否掉：
`code.sandbox_requires_approval` 自 ADR-058 起默认 `False`，而
`code_session.py` 的 `approval_required_risks` 只在它为真时才把 `external` 装进
去。也就是说这个选择的实际含义是「一条在你机器上跑的命令，默认不问你」。
`destructive` 是仓库里唯一无条件武装的一档，也是
`code_approvals.UNREPEATABLE_RISKS` 拒绝给出会话级长效批准的一档——两条性质
恰好都是这个工具需要的，而它们此前一个使用者都没有。

**复用 `sandbox:run` 权限范围。** 少一个名字，少一次 `IdentityContext` 的存储键
抬版。否掉的理由是它会让升级**静默发生**：每一个今天已经持有 `sandbox:run` 的
principal，会在工具上线那天获得这台机器，而没有任何一次动作表示他们要过这个。
ADR-057 花了一整份决定说这两个授予不是同一个授予，在权限范围上把它们合起来是把
那份决定又抹掉一次。

**把「命令只能在项目目录里跑」当成安全边界。** 它不是。cwd 是项目根，而命令是
一条 shell 命令：`cd /` 就出去了，`sudo` 也在 `PATH` 上。把它写成沙箱会是这份
文档里唯一一句假话，而且是最危险的那种假话——读的人会据此少做一次判断。目录是
**便利**（命令在你以为的地方跑），围住它的是批准，不是路径。

**只擦洗 `AW_SECRETS__*`。** 最小改动，也确实堵住了最要紧的那个。否掉是因为它
漏掉三条 DSN 和 `AW_KEY_FILE`，而且更根本地：它把「记得回来更新这里」变成了每次
新增配置字段的一项隐形义务，由一个不在想这件事的人承担。

**读到输出上限就截断，不杀进程。** 看起来更温和。实际上读取一停，管道就满，命令
就阻塞在写上——`wait()` 会在等一个正在等我们的东西。杀是那一步把「我们不听了」
变成「它不说了」的操作，不是可选项。

**让它也能进 Task。** 最诱人，因为 Task 才是长时间无人值守的那条路，也正是最想
跑测试的地方。否掉的不是 ADR-075 的重放论证（那条在 Code 上本来就不适用），是
一件更简单的事：Task 的运行在 Worker 里，决定在 API 进程里，那条路上没有闸门可
问，`ports/approval_gate.py` 把这写成了声明。一个必须每次问人的工具，放进一个
问不到人的进程，只能变成「每次都被拒绝」或者「假装问过了」。

**保持 `policy.shell_tools_enabled` 冻结，另加 `code.host_commands_enabled`。**
改动面更小，不用抬 schema。否掉是因为结果是一份自相矛盾的配置：同一个文件会
同时说「本部署没有 shell 工具」和「本会话可以跑命令」。那个冻结字段唯一的价值就
是它说的那句话，而这个方案会让那句话变成假的还留着它。

## 5. 不变量

1. `project_run` 只出现在**项目是真实目录**的回合里。扁平工作区没有目录可供
   命令运行，没有 `CODE_TOOLS_WITH_RUN` 与之配对，这个缺席是答案而不是遗漏。
2. **每一次调用都停在人面前，并且那个人看得见命令本身。** 风险是
   `destructive`，它在每一个 Code 信封里无条件武装，与
   `code.sandbox_requires_approval` 无关。
3. **没有「本会话都允许」。** `UNREPEATABLE_RISKS` 服务端拒绝，控制台也不渲染
   那个按钮——一个唯一结局是 422 的按钮会教给读者错误的规则。
4. **Task 永远拿不到它。** 不进任何 Task 授权信封，没有 profile 写它的名字。
5. **`AW_*` 一个都不进子进程。** 其余环境原样继承。
6. **命令启动的一切随命令一起被杀。** `start_new_session=True` 建组，
   `killpg` 杀组；只杀 shell 会留下一个被重新挂载的孤儿进程，握着端口，而系统里
   再没有东西知道它存在。
7. 风险上限由工具清单**推导**，不与之并列配置。两个开关是描述同一个决定的两种
   方式，而它们不一致时模型会被给到一个自己的信封拒绝的工具。

## 6. 怎么验证

- `tests/adapters/test_project_tools.py::TestRunningACommand` —— 退出码是结果不是
  失败、两条流按写入顺序合并、在项目目录里运行、只继承拿到的环境、stdin 是
  `/dev/null` 所以读 stdin 不挂死、超时被杀且说出来、**它启动的后台进程不比它
  活得久**、输出超限会杀掉并说明、风险与权限范围、绑定不带 `operation_key`。
- `tests/adapters/test_project_tools.py::TestExclusivity` —— 运行工具只出现在
  opt-in 的两个元组里、任何扁平工作区元组都不含它、上限推导的四个取值、以及
  「每个被提供的工具都在它推导出的上限之内」（对着 spec 断言，不是对着那条 `if`）。
- `tests/bootstrap/test_child_environment.py` —— provider key 与连接串不被继承、
  一个**还没有人写出来的** `AW_*` 设置自动被覆盖、操作员自己的
  `PATH`／`SSH_AUTH_SOCK`／`GITHUB_TOKEN` 原样保留、名字里**含有** `AW_` 但不以
  它开头的变量不被误伤。
- `tests/application/test_code_session.py::test_a_turn_holding_the_run_tool_is_not_told_there_is_no_shell`
  —— 两个基底上「没有 shell」都消失、工具被点名、上限跟着动、`destructive` 被
  武装。
- `web/src/features/code/CodePage.test.tsx` —— 卡片显示 `rm -rf build` 而不只是
  64 个十六进制字符，并显示「不可撤销」。
- 实测 2026-08-24：`config.code-local` 的 `policy_identity` 为
  `policy-v1:b8d1414911cc29e7`，其余 profile 为 `policy-v1:0e67f8dd84919551`。
- **整链实测 2026-08-24**，本机，把真的 `ToolGateway`、真的
  `EnvelopePolicyEngine`、真的 `CodeApprovalRegistry` 和一个真的临时目录接在一起，
  走 `propose → prepare → authorize → invoke` 四个阶段。这是单元测试看不到的那一段
  ——人被展示的那句话，和随后真正执行的那句话，是不是同一句：

  ```
  risk ceiling derived from the tool list : destructive
  held at the gate                        : project_run
  risk shown on the card                  : destructive
  digest shown on the card                : 3c4f25321818598e…
  COMMAND shown on the card               : {"command":"ls && echo '--' && cat src/main.py"}
  standing approval refused               : project_run is destructive: it may be
                                            approved once, not for the session
  --- what the command actually returned ---
  exit code: 0
  README.md
  src
  --
  print('hi')
  ```

  四件事同时成立：上限由工具清单推导出 `destructive`；调用**停住了**；卡片上是命令
  本身而不只是那 64 个十六进制字符；`approve_for_session` 被服务端拒绝，只有
  `approve_once` 放行。输出里没有任何 `AW_` 开头的东西。
- 完整门禁：`ruff format --check`、`ruff check`、`pyright`、`pytest`、
  `pnpm --dir web check`。
