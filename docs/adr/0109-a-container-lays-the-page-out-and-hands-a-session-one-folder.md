# ADR-0109：容器这条路上还有两样装配得出的：版面预览，和一个能写的文件夹

- 状态：Accepted
- 日期：2026-09-04
- 关联：ADR-0045（版面是一次转换，不是第三个解析器——§4.2「不给默认镜像装 LibreOffice」
  对**镜像**仍然成立，本 ADR 只改 Windows 启动器传的那一个构建参数）、ADR-0074（每个编码
  会话都属于一个有目录的项目——本 ADR 让容器里也有一个目录可选）、ADR-0077（机器上的
  一条命令在跑之前先被看见——`project_run` 是宿主机的 shell，**本 ADR 不把它放进容器**，
  §3.3 说为什么）、ADR-0102（一台部署要说得出自己没装配起什么——新增四行）、
  ADR-0105（一条命令可以装配起容器装配得出的全部——本 ADR 是它的两条补遗）、
  ADR-0107（沙箱 broker——`code.sandbox` 那一行报的就是它有没有应答）

## 1. 背景

第七十一批之后，一位 Windows 用户的原话是：「没有浏览器和 shell，这些和 mac 版本里都不
一样，word 预览、代码执行都做不到。」四件事，查下来是三种不同的缺，而且没有一处在控制台
上说出来：

1. **Word 版面预览恒 503。** `Dockerfile` 的 `WITH_FIDELITY_PREVIEW` 默认 `0`，
   `scripts\stack.cmd` 照默认构建，于是 `GET /v1/artifacts/{id}/pdf` 在每一台 Windows 上
   都是 503，Tasks 页把 .docx 退回文字预览并说「服务器上没有可用的文档转换器」。
   ADR-0045 §4.2 把默认关掉的理由写得很清楚（1.24 → 1.96 GB，一次几百 MB 的下载曾经断过），
   而那个理由是关于**默认镜像**的。这条启动器不是默认镜像——它自 ADR-0105 起的唯一职责是
   「装配起容器装配得出的全部」，一个已经带着几 GB 检索运行时的镜像，为省 700 MB 把 Word
   报告显示成纯文本，读起来就是「这个控制台不会预览 Word」。
2. **文件夹选择器打开在镜像的只读树里。** Code 的门是选一个文件夹（ADR-0074），选择器从
   服务端的家目录开始浏览。容器里家目录是 `/app`——镜像自己的文件树，`read_only: true`。
   每一个目录都选得中，没有一个写得进：第一次 `project_write` 报一句只读文件系统的错，
   读起来像工具坏了。这就是「代码执行都做不到」里项目会话那一半。
3. **没有 shell，没有浏览器，控制台没说。** 原生 demo 档打开了 `policy.shell_tools_enabled`，
   项目会话有 `project_run`——宿主机的 shell，机器有什么就能用什么，`open https://…` 能开
   浏览器（ADR-0077）。Compose 档没有这一行，也不该有（§3.3）。但唯一说出这件事的地方是
   模型自己在回合里写「本环境没有 shell 与网络」，那句话读起来像模型偷懒。运行状态页
   （ADR-0102）有 `code.sessions` 一行，说 Code **在**，没有一行说 Code 够得到什么。
4. **不挂项目的会话的 `sandbox_run`** 由 ADR-0107 的 broker 提供，探到运行时才打开。它可能
   真的在这台机器上没起来（拉不到 `python:3.12-slim`），但同样没有一行为**编码会话**报它
   ——`task.sandbox` 报的是任务信封。

## 2. 决定

**四件事，全部在容器这一侧做，不给容器一个 shell。**

1. `scripts\stack.cmd` 以 `--build-arg WITH_FIDELITY_PREVIEW=1` 构建；`scripts\stack.cmd lite`
   得到不带 LibreOffice 的那个镜像。`Dockerfile` 的默认值**不变**：CI 和自己敲 `docker build`
   的人仍然拿到轻的那个，ADR-0045 §4.2 的论证对它们继续成立。
2. `compose.yaml` 的 `api` 服务——**只有它**——把宿主的 `var/projects`（或
   `AGENT_WORKBENCH_PROJECTS_DIR`）绑定在 `/projects`；`config.compose-local.toml` 新增叶子
   `code.projects_root = "/projects"`；`FilesystemDirectoryBrowser` 接受一个起始目录，
   存在就从那里开始，不存在退回家目录。启动器在 `compose up` 之前建这个文件夹。
3. `/v1/system/capabilities` 新增四行：`code.sandbox`、`code.host_commands`、`code.web_search`、
   `artifact.layout_preview`。每一行缺失时说是哪一种缺、补法是什么；`code.web_search` 只在
   `policy.search_tools_enabled` 为真时挂「联网搜索」那个开关——策略位关着的时候拨 provider
   开关什么也不会变。
4. Code 起始屏在第一句指令之前画一行「这里能碰到：沙箱运行 · 宿主命令 · 联网搜索」，缺的划掉、
   原因在 title 里、行尾指向运行状态页。它读的就是第 3 条那份清单，不另开接口。

## 3. 为什么是这些做法而不是别的

### 3.1 构建参数放在启动器里，不改 Dockerfile 的默认

两个读者，两个正确答案。`docker build` 的默认读者是 CI（`quality` job 断言 `embedding` extra
**不在**，一个更重的默认只会让它更慢）和任何自己敲命令的人；对他们 ADR-0045 的账（几百 MB、
断过流、不装也不是坏的）一字不差。`stack.cmd` 的读者是一台什么都没装的 Windows，它已经
接受了几十分钟的首次构建与 6.7 GB 的权重下载——在那笔账上，700 MB 换来 Word 报告能看版面，
是这条启动器存在的理由本身。`lite` 留给知道自己不要它的人，是一个词而不是第二个启动器。

### 3.2 一个文件夹，不是 `$HOME`，只给 API

绑定挂载是这个拓扑里**第一个**指向宿主磁盘的可写挂载，所以它要被限制到能说清楚的最小范围。
只给 `api`：Code 回合只在它里面跑（`code.execution_locality = "in_api_process"`，冻结的
`Literal`），Worker 拿到一个可写的宿主目录是一条没有读者的第二写入路。只给一个目录而不是
家目录：一个可以改文件的会话有一个文件夹，不是这块盘。`read_only: true`、`cap_drop: ALL`、
`no-new-privileges` 原样——绑定挂载在只读根上照常可写，硬化锚点一个字不动。

`code.projects_root` 是一片 `[code]` 下的叶子而不是启动脚本猜出来的一个目录，因为选择器是
Code 的正门，决定它打开在哪的设置该和决定有没有这扇门的设置放在一起。带默认值，不抬
schema（`docs/configuration.md` §2 的先例）。十个原生档不设它：进程以用户身份跑在用户机器上，
家目录就是对的。

`AGENT_WORKBENCH_PROJECTS_DIR` 在 `AW_` 命名空间之外，和 `AGENT_WORKBENCH_TEST_DSN` 同理：
设置拒绝未知的 `AW_*` 变量，而这个变量只有 Compose 读，应用从不读。

### 3.3 不把 `project_run` 放进容器

它是本 ADR **拒绝**的那一半，值得单独写。技术上做得到：`ProjectRunTool` 是
`create_subprocess_shell` 加一个 `cwd`，在容器里照样跑。不做，是因为它会同时违反两份已有的
论证：

- ADR-0077 的整个前提是「**这台机器**上的一条命令在跑之前先被看见」——工具描述、
  `_HAS_SHELL` 提示词、审批卡上的措辞全部说的是「用户自己的机器、自己的工具链、自己的凭据、
  含网络」。容器里的 shell 没有一样是真的：没有用户的工具链，没有 git、node、gcc，
  `cap_drop: ALL` 下也开不了浏览器。给模型一个提示词说「机器有什么就能用什么」的 shell，
  然后让它发现什么都没有，正是 ADR-0057 拒绝的形状——「会话不得提供它兑现不了的工具」。
- 这个容器持有 provider key、数据库连接串和每一个工作区。ADR-0105 §4.1 拒绝把 docker socket
  挂进它，理由是「一个能碰到 key 的进程不该同时能碰到 daemon」；一个能碰到 key 的进程
  同时能跑任意 shell 命令，是同一个理由的更弱版本。ADR-0107 用一个只跑沙箱 server 的容器
  解决了沙箱那一半，shell 这一半没有对应的「只跑 shell、什么都不挂」的容器可以拆——它要的
  正是项目目录和工具链。

所以容器这条路上 Code 有两条互补的执行路：不挂项目的会话经 broker 有 `sandbox_run`
（一次性、断网、文件进文件出），挂项目的会话有读写文件的五件工具和**一行说明**。真要
宿主 shell 与浏览器，走原生路径——`scripts/dev.sh up` 的 demo 档打开它，控制台的
`code.host_commands` 那一行就是这么写的。

### 3.4 四行，不是一行

`code.sessions` 已经在了，为什么不把它的 `detail` 填成工具名？因为四件东西缺的方式不同、
补法不同：沙箱是 broker 有没有应答（看容器日志、restart），宿主命令是这条路上永远没有
（换路），联网搜索是一个开关或一行策略配置（按哪个缺分别处理），版面预览是镜像里有没有
`soffice`（重新构建）。挤在一行的 detail 里，读者拿到四个名字和一句补法，而那句补法对
其中三个是错的。ADR-0102 的形状就是每一处缺失自带原因与补法；四行是那个形状，一行不是。

### 3.5 起始屏上那一行读同一份清单

起始屏那行不另开接口：三个答案都是装配期的事实，`/v1/system/capabilities` 就是它们
唯一的出处，第二个接口会是第二份可以说谎的口径。读不到就不画——它是提示不是门，一台
API 比控制台旧的机器不该因为少一行提示而开不了 Code。

## 4. 代价与没做的

- **镜像大了约 700 MB**（1.25 → 1.96 GB，ADR-0045 的实测），首次构建多一次几百 MB 的
  下载，那次下载断过流。`lite` 是退路。
- **Code 在容器里没有 shell、开不了浏览器**，这是决定不是遗漏（§3.3）。登记为
  `docs/known-gaps.md` F-37，分类「拒绝」。
- **`var/projects` 是 Docker Desktop 的绑定挂载**，性能是 Docker Desktop 文件共享的性能；
  大型仓库在里面 `project_grep` 会比原生慢。没量过。
- **这四行和那个文件夹都没有在 Windows 上跑过。** 和 ADR-0105～0108 一样，
  `tests/deployment/test_compose.py` 守的是让行为成立的规则：启动器传那个参数、只有 `api`
  挂 `/projects`、profile 指向它、启动器建它；`tests/api/test_system_capabilities.py`
  对着四行的每一种缺各断言一次。规则比运行弱，这里照旧写明。

## 5. 证据

- `tests/deployment/test_compose.py::test_the_windows_launcher_builds_the_image_that_can_lay_a_document_out`、
  `::test_the_api_alone_can_write_one_host_folder_and_the_picker_opens_there`
- `tests/config/test_compose_profile.py::test_the_compose_profile_opens_the_picker_in_the_mounted_folder`
- `tests/adapters/test_directory_browser.py`（三条：无配置退回家目录、配置了从那里开始、
  配置了但不存在退回家目录）
- `tests/api/test_system_capabilities.py`：新增五条，稳定集合与来路表各多四行
- `web/src/features/code/CodePage.test.tsx`：「起始屏说出这台部署里编码会话能碰到什么」
