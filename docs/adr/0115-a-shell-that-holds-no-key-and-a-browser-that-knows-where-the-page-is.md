# ADR-0115：容器栈里的编码会话拿到一把不持有 key 的 shell，和一个知道页面在哪的浏览器

- 状态：Accepted
- 日期：2026-09-12
- 关联：ADR-0114（搜索不是尺子——本 ADR 给它一把尺子）、ADR-0109 §3.3 与 known-gaps F-37
  （拒绝在 API 容器里放 shell——本 ADR **按它给出的翻案条件**翻案，而不是推翻它的理由）、
  ADR-0107（沙箱 broker 独占 socket：用拓扑给边界定形——本 ADR 照抄这个手法）、
  ADR-0113 §4 与 F-39（编码会话的浏览器——本 ADR 把它在 Compose 档接通，并修一个两条路都有的
  缺口）、ADR-0077（`project_run` 的门与语义——一个字不动）、ADR-0058（提示词描述回合真正
  所在的世界——本 ADR 让它描述两个）

## 1. 背景

ADR-0114 落地、栈重启之后，用户又发了一次「请你编写马里奥」。这一轮跑完了、写了报告、没有
撞步数上限——但 63 步里 53 次 `project_grep`，报告自己承认三件事：

> 我一度把 `project_grep` 当尺子用……搜索工具只能告诉你某行是否匹配，不能测量。
> 没有 shell，我跑不了 `check_level.js`。
> 我没有运行游戏。

用户的判断：「第一还是很多次读取文件，第二没有在自带的浏览器中进行 html 的运行展示，所以你
应该把 shell 也一起补上？」并且明确要求：**参考本机 Claude Code（desktop）的技术实现。**

事件流证实了提示词与提醒各自做到了什么、没做到什么：第 25 次搜索的提醒落下之后模型说
「I've been using grep as a ruler, which it isn't. Let me stop」，然后**又滑回去了**，直到第 50
次再被提醒。一个没有仪器的模型会一再伸手去拿唯一像仪器的东西。ADR-0114 只能让它认错，
不能让它量对。

### 1.1 Claude Code 是怎么做的，对照表

| Claude Code | 它解决的问题 | 这个仓库对应物（本 ADR 之后） |
|---|---|---|
| `Bash` 工具永远在，跑在工作目录里，120 s 超时、输出截断并标记 | 数、量、跑、看 | `project_run`：同一把工具、同一道门，Compose 档跑进 `runner` 容器 |
| 每条命令过一次权限门（allow/deny 规则、自动模式的分类器、或者弹窗问人） | 危险命令不静默执行 | `destructive` 风险、每次调用停在人面前、卡片上是规范化后的真实命令（ADR-0077，不变） |
| sandboxed bash：只准写项目目录、不碰用户的其他东西 | 一条错命令的爆炸半径 | `runner` 容器：只挂 `/projects`、没有 key、没有数据库地址、没有工件卷、只在自己的网络上 |
| 输出过长「saved to file」+ 预览 | 一条 `cat` 不把上下文吃光 | `MAX_CAPTURE_BYTES` / `MAX_INLINE_OUTPUT_CHARS` 与「[N characters; first 8000 shown]」（已有） |
| 桌面版的浏览器面板：打开页面、读可访问性树、截图、看控制台 | 「文件写出来了」≠「页面跑起来了」 | ADR-0113 的六件浏览器工具，本 ADR 在 Compose 档接通，并让项目会话能按相对路径打开自己的页面 |
| 系统提示：「够了就动手；不要重推已知；改完不要再读一遍确认」 | 循环 | ADR-0114 已搬 |

第一行和第三行是本 ADR 的全部：**一把 shell，和一个让这把 shell 不危险的地方。**

### 1.2 为什么不是把 shell 放进 API 容器

F-37 的拒绝有两条理由，本 ADR 接受两条。

第一条：ADR-0077 的提示词、工具描述、审批卡说的全是「用户自己的机器、自己的工具链、自己的凭据」，
容器里一样都不是真的——给模型一把描述错误的 shell，是 ADR-0057 拒绝的「提供兑现不了的工具」。
**答复**：把描述改对（§2 第 5 条）。

第二条：API 容器持有 provider key、数据库与每一个工作区；ADR-0105 §4.1 拒绝让它同时碰 daemon，
「能跑任意 shell 命令」是同一件事的弱形式。**答复**：不放进 API 容器。F-37 自己写了翻案条件——
「回答『容器里的 shell 是谁的 shell』，以及一个不持有 key 的、只跑 shell 的容器该长什么样」——
本 ADR 就是那两个答案。

### 1.3 顺带查实的：F-39 的隧道从来不存在

F-39 写着「API 容器也已经有一条 `docker/loopback_proxy.py` 的隧道通向它的 8773」。
`docker/run-api-local.sh` 里只有 8766（沙箱）、8768（computer）和 8000→8001（控制台反代）。
`api.browser_frame_url` 默认指向 `127.0.0.1:8773/frame`，在 Compose 里指向一个没人监听的端口。
`compose.yaml` 三处注释都说「Also on the browser's network, to reach `browser:8773` through the
tunnel」，隧道本身没有写。本 ADR 补上，并把 F-39 那句话改掉。

### 1.4 顺带查实的：项目会话本来就打不开自己的页面，两条路都是

`browser_open` 的 `workspace_path` 由**浏览器进程**按它启动时的 `--workspace-root` 解析——原生路径
是工件根，Compose 是 `/workspace`。这对平铺工作区是对的，对项目会话是错的：项目文件在
`/projects/<名字>/`，`workspace_path="mario.html"` 打开的是 `<工件根>/mario.html`，不存在。
而模型**没法绕**：`_PROJECT_WORLD` 告诉它路径相对项目根、绝对路径会被拒，所以它永远不知道
根在哪，也就拼不出 `file://` URL。ADR-0113 的实测证据用的是平铺工作区；`demo-local` 下每个
会话都属于一个项目，所以那条路上 `browser_open` 一次也没能打开过项目会话写的页面。

## 2. 决定

**六件事。前四件是那把 shell，后两件是浏览器。**

1. **一个新的、第六个项目自有 MCP server：`agent-runner-mcp`**（`apps/runner_mcp/`）。一个工具
   `run_command(command, cwd, timeout_seconds)`，用 `project_run` 一直在用的那段子进程代码
   （进程组、`SIGKILL`、有界读取、三个上限，原样搬到 `adapters/filesystem/commands.py`）。
   它只多做一件事：`cwd` 必须是它启动时 `--projects-root` 之下的一个**已存在**的目录——
   判断在解析后的路径上做，`..` 与指出去的符号链接一并拒绝。`/health` 在根目录不存在时
   答 503，所以挂载没到位的一次启动不会把 `project_run` 变成每次都失败的工具。

2. **一个新的 Compose 服务 `runner`。** 同一个镜像、同一套 `x-app-hardening`、把 API 挂的那个
   宿主文件夹以**同一路径** `/projects` 读写挂入。**没有** `x-app-environment`（那一块带着数据库
   地址）、没有 key 卷、没有工件卷、没有 socket、没有配置文件。只在一个新网络 `runner` 上，
   这个网络上只有它和 API。API 经 `docker/loopback_proxy.py` 的隧道 `127.0.0.1:8774 → runner:8774`
   调它——ADR-0107 §3.4 的形状，`--host` 的 choice 列表、MCP SDK 的 Host 校验、settings 校验器
   三样一个字不改。

3. **`project_run` 后面多了一道缝：`ports/commands.py` 的 `CommandRunner`。** 两个实现：
   `LocalCommandRunner`（本进程子进程，原生路径照旧）和 `RemoteCommandRunner`（调 runner）。
   工具本身——名字、`destructive` 风险、每次调用停在人面前、回执、输出渲染、给模型的那句话——
   **一个字不改**。装配时 `ProjectRunTool(runner=...)`，`runner` 是 `RunnerSlot`：与 `SandboxSlot`
   同形，启动时 fail-fast 连接、之后转发；没配 `[runner]` 的部署拿到 `None`，行为与本 ADR 之前
   逐字相同。

4. **一个新的配置段 `[runner]`**（schema `1.19 → 1.20`）：`enabled`、`endpoint`
   （默认 `http://127.0.0.1:8774/mcp`）、`timeout_seconds`。根校验器拒绝 `runner.enabled` 而
   `policy.shell_tools_enabled` 为假的组合。`config.compose-local.toml` 打开 `shell_tools_enabled`，
   `runner.enabled` 与 `code.browser_enabled` **留在文件里为假**，由 `docker/run-api-local.sh`
   逐次启动决定：开两条隧道，用 Task Worker 自己的 `scripts/smoke_mcp_server.py` 各探一次
   （真实 MCP 客户端、健康路由、期望的工具名），探到才导出 `AW_RUNNER__ENABLED=true` /
   `AW_CODE__BROWSER_ENABLED=true`；操作者显式给的值不动——沙箱那套安排，再来两次。

5. **提示词说清命令跑在两个地方中的哪一种。** `_HAS_SHELL` 不再说「it is the user's own machine,
   their installed tools」；它说：原生启动器上是用户的机器和工具链，容器栈上是「一个用本项目
   镜像建的 Linux 容器：Python 3.12 和常规 Unix 工具，没有 Node、没有用户自己装的东西，项目目录
   是同一份文件」。并补上 ADR-0114 写不出来的那一句：**需要数、量、解析、跑的事，一条
   `python -c` 或 `wc` 就是答案，一条批准的命令胜过二十次答不了的搜索。** `project_run` 的工具
   描述同步从「the user's real machine」改成「the process this session's commands run in」。

6. **浏览器：接通，并且让项目会话开得了自己的页面。** `run-api-local.sh` 多一条
   `127.0.0.1:8773 → browser:8773` 的隧道；`browser` 容器把项目文件夹**只读**挂在与 API 相同的
   `/projects`；API 侧多一个薄包装 `adapters/tools/browser.py::open_within_project`——回合进入了
   项目（`ProjectFileScope` 已设，每个 `project_*` 工具读的同一个 ContextVar）时，`workspace_path`
   先过 store 的路径判断（`..`、绝对路径由它拒），再拼成该项目目录下的 `file://` URL 发给
   浏览器。`_BROWSER` 提示词相应改成「给 `workspace_path` 你给读工具的那个相对路径，不要绝对
   路径」。平铺工作区回合原样透传，浏览器仍按自己的根解析。

## 3. 为什么是这些做法而不是别的

### 3.1 一个容器，而不是沙箱、也不是主机

三个候选：

- **API 容器里直接跑**（F-37 拒绝的那个）。改对描述能答第一条理由，答不了第二条：key 文件对
  同一个 uid 可读，PostgreSQL 在这套拓扑里是 trust 认证、连密码都没有。人会看到命令，但人看到
  `cat` 一个路径未必知道那是 key。见 §1.2。
- **给项目回合 `sandbox_run`**（F-24 拒绝的那个）。它算得了，但它是 `python:3.12-slim`、断网、
  一次性、文件进文件出——不是 shell，跑不了项目自己的测试命令，跑不了 `node`；而且让它看见
  项目目录要动 ADR-029 逐行论证过的 `ISOLATION_FLAGS`。它解决的是另一件事，并且仍然值得做
  （F-24 保持不变）。
- **一个只跑 shell 的容器。** F-37 自己点名的形状，ADR-0107 已经用过一次的手法（broker 独占
  socket，其他人都没有）。代价是一个进程、一个网络、一条隧道——每一样都有现成模板。

### 3.2 网络：单独一个，不是 `default`，也不是 `internal`

放在 `default` 上，一条模型写的命令能连到 `postgres:5432`（trust 认证、无密码）、`qdrant:6333`、
`encoder:8769`、沙箱 broker——比原生路径**更**暴露：原生的宿主 shell 连 5433 至少要 `dev.sh`
里那个密码。`internal: true` 则连不上外网，而 `pip download`、`curl` 是用户在自己机器上会做的
事，runner 里又没有任何东西值得偷。所以是一个**单独的、非 internal** 的网络，上面只有 runner
和 API。剩下的暴露是 API 自己的控制台反代（`api:8000`，header 信任的身份）——**与原生路径等价**：
宿主 shell 一样能 `curl 127.0.0.1:8000`。`test_only_the_api_shares_the_runners_network` 钉住。

### 3.3 `cwd` 是一个字符串，不是一组文件

沙箱把文件送进容器再收回来，因为它挂不了任何东西（ADR-029）。runner 不用：两边挂的是同一个
宿主文件夹、同一个路径，API 写下的文件在 runner 里就在同一个位置。于是过线的只有命令、目录、
时钟——而「这个目录在我这边存在吗、在根之下吗」只有 runner 自己答得了，所以判断在 runner。
浏览器同理：`file://` URL 由 API 按它自己的视角拼，浏览器按自己的视角打开，两边挂在同一处才
成立——`test_the_browser_sees_the_projects_folder_read_only_at_the_apis_path` 钉住两个挂载共享
一个源。

### 3.4 为什么 `[runner]` 是一个段，而不是 `[code]` 下的一个开关

它是**一个这个进程要拨号的 MCP server**——与 `[sandbox]`、`[[mcp.servers]]` 里的 browser 同类，
有地址、有超时、启动时 fail-fast。ADR-0107 给沙箱的形状是「一个段说地址，`[code]` 的开关说
要不要」；这里 `policy.shell_tools_enabled` 已经是那个开关（它决定 `project_run` 在不在目录里，
并且进 `policy_fingerprint`），`[runner]` 只回答「在哪跑」。两个都真才有远端执行；只有前者是
原生路径的老样子；只有后者被校验器拒——一个读起来像能力、什么都不做的段，是 known-gaps 归为
「口径不实」的形状。

### 3.5 为什么原生路径一个字不变

`demo-local` 没有 `[runner]`，`ProjectRunTool` 拿到 `runner=None`，走 `LocalCommandRunner`——
就是本 ADR 之前那段代码搬了个位置。原生路径的 shell 是用户自己机器上的、带着用户自己的工具链，
这是 ADR-0077 的全部理由，本 ADR 没有反对它。变的只是提示词现在同时说出了另一种可能。

### 3.6 提示词说「两个地方之一」而不是判断自己在哪

进程能不能知道自己在容器里？能猜（`/.dockerenv`），但那是外层事实混进 core，而且猜错的代价是
ADR-0058 那个失败。两个世界共享每一句必须说的话——用户的真实文件、没有 undo、命令先给人看——
只在「谁的工具链」上不同，而这一点两句话就能说清。说两句真话比一句可能错的话好。

## 4. 代价，与没做的

- **一次启动探不到 runner 时，`project_run` 回落到 API 容器内执行**——正是 ADR-0109 §3.3
  反对的安排，保留一次启动而不是永久，launcher 在 stderr 上明说，System 页 `code.host_commands`
  那一行可见。选择回落而不是去掉工具，是因为另一种失败（一条正常的启动因为 runner 慢了几秒
  而没有 shell）更常见也更难解释。要收紧就把 `AW_RUNNER__ENABLED=false` 显式给 launcher。
- **runner 里没有 Node。** 用户那个 `check_level.js` 在这里仍然跑不了；`python -c` 能做同样的事。
  给镜像装 Node 是几百 MB，留给下一个真需要它的场景。
- **浏览器仍是 ADR-0113 的那个浏览器**：`internal` 网络、守卫代理。项目页面经 `file://` 打开，
  页面里若引外部资源照旧被守卫判。
- **`test_computer_consent.py` 在 Windows 上弹真实对话框**（ADR-0114 §4 记的）——本批的全量
  排除了它，另一段会话已修（第八十批）。
- **schema 抬到 `1.20`**，`config.default.toml` 与三条测试跟着改。`docs/configuration.md` 记行。

## 5. 证据

- `tests/apps/test_runner_mcp_server.py`：工具声明与 hint、根之下的目录能跑、根之外被拒、不存在
  的目录被拒、相对路径与超长时钟被契约拒、被时钟杀掉的命令两个通道都说了、`/health` 在挂载
  缺失时答 503；一条真 shell 的测试（POSIX）。
- `tests/adapters/test_remote_command_runner.py`：发过去的三样、收回来的四样、三种「不是结果」。
- `tests/adapters/test_browser_open_within_project.py`：只包 open、项目回合改写成 `file://`、
  平铺回合透传、URL 不改写、`..` 被 store 的规则拒、不存在的文件用项目工具的话说。
- `tests/adapters/test_project_tools.py::TestRunningACommandSomewhereElse`：工具把命令与目录交给
  runner、回执照记、拒绝带码、未打开的连接是一句话、被杀的命令带着已打印的内容。
- `tests/deployment/test_compose.py`：runner 持有的每一样缺席、只有 API 在它的网络上、浏览器
  只读挂着同一个源、launcher 隧道→探针→`agent-api` 的顺序与两个开关的透传。
- `tests/config/test_compose_profile.py`、`tests/config/test_settings.py`：profile 开了 shell、
  两个开关留给 launcher、两个环境变量能压过 profile、`runner.enabled` 不带 shell 被拒。
- `tests/application/test_code_session.py`：`_HAS_SHELL` 说出两个地方、`_BROWSER` 说出相对路径。
- 装配验证记在 status.md 第八十一批。
