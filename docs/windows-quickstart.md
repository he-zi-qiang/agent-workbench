# 在 Windows 上把整套跑起来

这份文档写给**一台什么都没装的 Windows**。读完照做，你会得到一个浏览器里能点的控制台，
里面装配起了这台部署装配得出的全部能力。

不需要 Python，不需要 Node，不需要 git（可以要，见 §2）。**只有 computer use 例外**：
它够不着容器外的桌面，所以在这台 Windows 上原生跑，需要装一个 `uv`（§7）。
`scripts/dev.sh` 是 bash，Windows 上没有那条路——这里是**唯一的一条**，
而它从 [ADR-0105](adr/0105-one-command-may-assemble-everything-a-container-can.md)
起不再是被刻意做轻的那一条；从
[ADR-0106](adr/0106-one-process-holds-the-weights-and-the-others-ask-it.md)、
[ADR-0107](adr/0107-the-sandbox-broker-alone-holds-the-socket.md)、
[ADR-0108](adr/0108-a-screen-adapter-for-windows-composes-its-own-frame.md) 起，
它在 16 GB 的 Docker 上起得来，有沙箱，有 computer use。

> **一句如实说明，放在最前面。** 这个仓库的测试跑在 POSIX 上。下面每一条 Windows 行为
> 都由 `tests/deployment/test_compose.py` 守着，但它守的是**让那个行为成立的规则**，
> 不是一次 Windows 上的真实运行。规则比运行弱，这里和 README 一样写明。

---

## 0. 先算一笔账：这台机器够不够

**这套栈里只有一个进程加载检索模型：`encoder` 服务**
（[ADR-0106](adr/0106-one-process-holds-the-weights-and-the-others-ask-it.md)）。API、两个
Task Worker 与摄取 worker 通过 HTTP 向它要向量，自己不装 torch。在那条 ADR 之前是四个进程
各加载一整套，下限因此是现在的四倍。

实测数字只有一个，而且正好就是「一个加载模型的进程」的：约 **12 GB** 可用内存，其中约
**6.7 GB** 是 dense BGE-M3、sparse BGE-M3、bge-reranker-v2-m3 三个模型文件本身
（2026-07-31，原生路径，见[本机运行手册](running-locally.md)）。另外三个 Python 进程、
PostgreSQL、Qdrant 与 collector **没有量过**：

| | 约需 | 意思 |
|---|---|---|
| 硬下限 | **12 GB** | 那一个实测数字。低于它 encoder 自己就在换页，整套栈不是慢，是一直在换页 |
| 舒服 | **16 GB** | 同一个数字加 4 GB **留量**——留给没量过的那几个进程，不是第二次实测 |

`scripts\stack.cmd` 会在**开始构建之前**读 `docker info` 的 `MemTotal` 并按这两条线决定：
低于硬下限它停下来，因为另一种做法是让你等几十分钟的构建与下载，然后看着
`up --wait` 在换页里超时——那读起来像「这个项目跑不起来」，而不像「Docker 只分到了 8 GB」。

**一台 32 GB 的 Windows 什么都不用改。** Docker Desktop 默认把物理内存的一半交给 WSL 2
虚机，16 GB 正好在第二条线上。16 GB 的机器默认拿到 8 GB，在硬下限之下，要改：

1. Docker Desktop → Settings → Resources → Memory。
2. 那个滑块的上界由 WSL 2 决定，写在 `%UserProfile%\.wslconfig`：

   ```ini
   [wsl2]
   memory=12GB
   ```

3. 改完在 PowerShell 里 `wsl --shutdown`，再启动 Docker Desktop。

真想在不够的机器上看看会怎样：`scripts\stack.cmd anyway`。

### 还有一笔磁盘的账，它比内存那笔更容易被漏掉

内存不够会被 `stack.cmd` 拦住并说清楚。磁盘不够**不会**：Docker 把卷写满之后，构建停在
`unpacking to ...` 这一行不动——不报错、不退出、日志不再增长，守护进程开始对每个 API 调用
返 500。它读起来像卡死，不像磁盘满。

一次成功构建之后实测（2026-09-11）：

| | 大小 | |
|---|---|---|
| `agent-workbench:local` 镜像 | **18.6 GB** | embedding 运行时与 LibreOffice 占了大半 |
| 具名卷 | **9.3 GB** | 其中约 6.7 GB 是模型权重 |
| 构建缓存 | **19.8 GB** | 事后可回收，但它**在构建期间存在**，而那正是最紧的时候 |
| `docker_data.vhdx` 合计 | **48.5 GB** | |

| | 约需 | 意思 |
|---|---|---|
| 硬下限 | **30 GB** | 低于它，就算把缓存清光，构建产物也放不下 |
| 舒服 | **50 GB** | 上面那个实测数字取整。低于它，构建仍可能在缓存峰值时耗尽 |

**关键在于量的是哪块盘。** Docker Desktop 默认把数据放在系统盘
（`%LocalAppData%\Docker\wsl`），所以 C 盘空间再紧、D 盘再空也没用——Docker 一个字节都不
会往 D 盘写。要改：Docker Desktop → Settings → Resources → Advanced → **Disk image
location**，指到一块有空间的盘，Docker Desktop 会把已有数据搬过去。

> [!NOTE]
> 只改 `settings-store.json` 里的 `DataFolder` **对 WSL2 后端无效**——那个键管的是
> Hyper-V 后端的虚拟机磁盘。WSL2 后端的数据在 `docker-desktop` 这个发行版里，位置由发行版
> 注册信息决定，得走上面那个 UI，或者把目录搬走再在原位置留一个 junction。

`stack.cmd` 在构建之前会量这块盘，并且会先穿过 reparse point 再量——否则留了 junction 的
机器上，它报的会是系统盘的剩余，而不是数据实际所在那块盘的。

---

## 1. 装 Docker Desktop

从 [docker.com](https://www.docker.com/products/docker-desktop/) 下载安装，
安装时勾上 WSL 2。装完**重开一次终端**，PATH 才认得 `docker`。

`stack.cmd` 把「没装 Docker」和「装了但引擎没起来」分开报，因为这两件事的解法不同，
而只有第一件从 Docker 自己的报错里看得出来。它是**试着跑**而不是问名字解析得到不——
一个卸载过的 Docker Desktop 常常在 PATH 上留下 `docker.exe` 壳子，那东西解析得到、
跑起来就失败。

---

## 2. 把代码弄下来

两条路，随便哪条：

- **不装 git**：打开
  [github.com/he-zi-qiang/agent-workbench](https://github.com/he-zi-qiang/agent-workbench)，
  Code → Download ZIP，解压。
- **装了 git**：

  ```bat
  git clone https://github.com/he-zi-qiang/agent-workbench.git
  ```

**目录名带中文没关系**，这一条值得单独说，因为它踩过：`docker compose build`——
连带 `compose up --build`——走 buildx bake，那条路会用构建上下文目录**自己的名字**去拼一个
gRPC header（`x-docker-expose-session-sharedkey`）。名字里有非 ASCII 字符，这个 header
就非法，构建在第一层跑起来之前就死，而报错里**既不提路径也不提目录**：

```
failed to dial gRPC: ... header key "x-docker-expose-session-sharedkey"
contains value with non-printable ASCII characters
```

2026-09-01 在 Docker 29.4.0 上实测。它需要两个条件同时成立：两个以上服务共用一个构建
上下文（这个拓扑今天有八个），**且**目录名非 ASCII。`COMPOSE_BAKE=false` 躲不开。

`scripts\stack.cmd` 因此**分两步**：先 `docker build`，再一个不带 `--build` 的
`compose up`。`docker build` 不走那条路，不受影响。所以你照着这份文档做不会撞上它——
**只有当你自己去敲 `docker compose up --build` 才会。**

---

## 3.（中国大陆网络）指一个 Hugging Face 镜像站

首次启动要下载约 6.7 GB 模型权重。默认从 `huggingface.co` 拿——把每一台部署的权重指向
第三方镜像站是一个供应链决定，属于跑这套栈的人，所以这个仓库不替你默认。

在跑 `stack.cmd` 的**同一个终端**里：

```bat
set HF_ENDPOINT=https://hf-mirror.com
```

PowerShell 里是 `$env:HF_ENDPOINT = "https://hf-mirror.com"`。

不设也能跑，只是下载可能卡住；卡住时 `weights-init` 那个容器会把用到的 endpoint 和这行
命令一起打出来。**双击运行的话这个变量传不进去**，需要镜像站就从终端里跑——或者把它
写进桌面快捷方式里，见 [§4 做成一个桌面图标](#做成一个桌面图标)。

---

## 4. 一条命令

```bat
scripts\stack.cmd
```

在资源管理器里双击也行。cmd 与 PowerShell 都能跑。

它按顺序做这些事，每一步失败都会说是哪一步：

1. 探 Docker（两种失败分开报）
2. 探镜像仓库通不通（`docker pull hello-world`，四分之一秒）。这一步是为一种**不像网络
   问题的网络问题**加的：Docker Desktop 自己存着一个手动代理，Clash / v2ray 升级之后
   本地端口会变（旧版 7890，现在多是 7897），而没有任何东西会去改 Docker Desktop 那一栏。
   引擎于是每次请求都去拨一个没人监听的端口，buildkit 把同一句拒绝按基础镜像打四遍，
   通篇只提代理、不提是哪个设置存着它。探到不通就停，并把 Docker Desktop 当前存的那几行
   原样打出来
3. 量内存（§0）
4. 量磁盘（§0）——量的是 Docker 数据卷所在的那块盘，不是 C 盘
5. `docker build --build-arg WITH_FIDELITY_PREVIEW=1 -t agent-workbench:local .`——首次要拉
   Node 24、Python 3.12、Docker CLI、LibreOffice 和检索运行时。LibreOffice 是给 Word 版面
   预览的（[ADR-0109](adr/0109-a-container-lays-the-page-out-and-hands-a-session-one-folder.md)），
   约 700 MB；不要它就 `scripts\stack.cmd lite`
6. `docker compose --profile demo up -d --wait`——十四个容器，等到全部 healthy，最多 600 秒。
   `encoder` 要把三个模型加载并预热完才算 healthy，其余四个进程都等它
7. 打开 `http://127.0.0.1:8000/ui/`

**首次运行按几十分钟算**，绝大部分花在构建镜像和下载权重上。这两件事都只发生一次：
权重落在一个具名卷里，第二次启动是秒级到分钟级。

中途它会打印一句摘要，说这套栈**还缺什么**。那句话是刻意的：一个什么也不说的控制台
会被读成一个坏了的控制台，这件事发生过。

### 做成一个桌面图标

`.cmd` 双击得了，但它和机器上所有别的 `.cmd` 长得一模一样——Windows 的文件图标来自
**文件类型**，不存在「只给这一个文件换个图标」。要一个认得出、还能钉到任务栏的入口，
那东西得是一个快捷方式：

```bat
scripts\shortcut.cmd
```

跑一次（双击它也行），桌面上多一个叫 **Agent 工作台** 的图标，指着 `scripts\stack.cmd`，
戴着 `scripts\agent-workbench.ico`——就是控制台侧栏顶上那个方块：同样的两个颜色、同样的
圆角，白底一个 A。它由 `scripts/make_icon.py` 画出来，只用标准库，所以改这个图标就是改
那里的几个坐标。右键「固定到任务栏」可以；`.cmd` 本身钉不了，快捷方式可以。拿掉它：
`scripts\shortcut.cmd remove`。

那个 A 是**画**出来的，不是用字体排的：`--aw-display` 落到 `var(--aw-sans)`，也就是系统
界面字栈——侧栏那个字母在 Windows 上是 Segoe UI，在 macOS 上是 SF，没有哪一个轮廓是
「对的那个」，只有形状是。它也比 CSS 里那个（28 px 方块里 14 px 的字）更大更粗一档：
资源管理器列表视图按 16 px 画图标，照原比例下去横杠不足一个像素，中间那个三角会糊死。

**仓库里为什么不直接放一个 `.lnk`。** 它存的是绝对路径，而这个 checkout 在每台机器上的
路径都不一样。而且一个随 ZIP 下载下来的 `.lnk` 会带着 mark of the web，Windows 会为你
刚刚自己解压出来的东西弹一次警告。所以仓库里放的是生成器，不是产物。

**它顺带补上了 §3 那个洞。** 双击进来的窗口不继承任何终端，所以 `HF_ENDPOINT` 从来到不了
它。现在只要在**跑 `shortcut.cmd` 的那个终端里**先设好，它就被写进快捷方式自己的命令行，
以后每一次双击都带着：

```bat
set HF_ENDPOINT=https://hf-mirror.com
scripts\shortcut.cmd
```

改主意就再跑一次，同名的快捷方式会被覆盖。不设也能跑，只是回到 §3 那条默认路。

> **这条路没有在一台真的 Windows 上跑过**，和本文开头那句说明是同一个性质。
> `tests/deployment/test_windows_shortcut.py` 守的是让它成立的规则——文件是 ASCII、
> 换行是 CRLF、`rem` 行里不含会被 cmd 当成命令的条件操作符、那段 PowerShell 里不出现
> 任何一个会被 cmd 二次解析的引号或 `&`、图标是一个九档尺寸的 ICO 且仍然是
> `make_icon.py` 画出来的那一个——规则比运行弱。

---

## 5. 存一把 provider key

**第一次起来时 Chat 还答不了，Task 也还不是真的。** 这不是坏了。

没有 key 时：

- Chat 那条路由**根本没有被挂载**（不是挂上了然后报错）。
- 两个 Task Worker 跑**合成 handler**——它们不联系 provider、不执行任何工具，
  approval 节点自己批自己。**任务会走到 `succeeded`，而一次模型调用和一次工具调用都
  没有发生过**，并且从控制台上看它和真 Worker 一模一样（控制平面目前没有 Worker 上报
  通道，能力表只能把它报成 `unknown`）。这是这台部署最容易被误读的一件事，所以它同时
  写在容器日志、`stack.cmd` 的首启摘要和这里。

存 key：左下角头像 → 设置 → 模型密钥，粘贴保存。然后重启**读配置的那三个进程**：

```bat
scripts\stack.cmd restart
```

只重启 API 和两个 Task Worker，PostgreSQL、Qdrant、collector 和那个权重卷都不动，
所以这是秒级的，而不是重来一次构建。

key 和「运行状态」页上的开关都只对**下一次启动**生效
（[ADR-101](adr/0101-the-console-may-hand-over-a-key-it-can-never-read-back.md)、
[ADR-103](adr/0103-an-optional-part-can-be-switched-from-the-console-for-the-next-start.md)），
所以这条命令也是拨完开关之后要跑的那条。

---

## 6. 这台部署有什么、没有什么

控制台的**「运行状态」**页会把这台部署**实际装配起了什么**逐行列出来，附原因和补法
（[ADR-102](adr/0102-a-deployment-says-what-it-could-not-assemble.md)）。不用靠猜，
也不用信这张表——**以那一页为准**，这里只是给你一个预期：

| 能力 | 存了 key 之后 |
|---|---|
| 直接对话 | 有 |
| 知识库问答（RAG，真 BGE-M3 + 重排，模型只在 `encoder` 里加载一次） | 有 |
| 知识库检索 `/v1/search`、知识库页的检索面板 | 有 |
| 文档上传入库（真向量，不是哈希替身） | 有 |
| 提交任务 / 真实图 Worker / 人工审批 | 有 |
| 两个 Worker 的 claim / lease / epoch 竞争 | 有 |
| Word MCP（`render_document`） | 有 |
| 任务产出里 .docx 的**版面**预览（LibreOffice 转 PDF） | 有（`lite` 构建的没有，只有文字预览） |
| web MCP（`fetch_page` / `download_document`） | 有 |
| 对话联网搜索 / 任务联网搜索 | 有（用同一把 provider key） |
| Code 会话，含 `sandbox_run`（不挂项目的会话） | 有——沙箱在 `sandbox` 容器里，见下 |
| Code 项目会话：读写 `var\projects` 下的真实文件 | 有，见下 |
| **Code 项目会话的宿主命令 `project_run`、开这台机器的浏览器** | **没有，而且不会有**——原生路径才有，见下 |
| **任务沙箱执行** | **有**，见下 |
| 任务分流（自动选图） / 子代理委派 | 有 |
| 评测页的报告 | 能看；**从界面发起评测不行**，页面会给你手敲的命令 |
| **Computer use** | **有，但不在容器里**：在这台 Windows 上另起一个进程，见 §7 |

**沙箱是怎么进来的，以及它唯一会缺席的情形：**

- `sandbox_run` 每次调用起一个 `--network=none` 容器，所以它的 server 要一个能应答的 Docker
  daemon。ADR-0105 拒绝把 `docker.sock` 挂进 API 或 Worker 的容器——那会把它们的
  `cap_drop: ALL` 变成装饰。[ADR-0107](adr/0107-the-sandbox-broker-alone-holds-the-socket.md)
  的做法是**只让一个只跑沙箱 server 的容器**（`sandbox`）持有 socket：它不挂 key、不挂
  产物卷、不读配置、不连数据库，被攻破买到的只是 daemon。API 与 Worker 经一条两端都是回环
  的隧道够到它，所以它们的容器里仍然没有 socket。
- 它**唯一**会缺席的情形：broker 首次启动要拉 `python:3.12-slim`，拉不到时它照常启动、
  `/health` 报 503，API 与 Worker 的启动脚本探它 90 秒，探不到就让沙箱在这次启动**留关**
  并在日志里说明；「运行状态」页 `task.sandbox` 那一行会是「缺失」。
  `scripts\stack.cmd logs` 看 `sandbox` 那个容器，在 PowerShell 里
  `docker pull python:3.12-slim`，然后 `scripts\stack.cmd restart`。
- 想让脚本能画 PDF：`scripts\stack.cmd sandbox-image` 构建一个带 reportlab 与中文字体的
  镜像（约 70 MB，要网络），再 `scripts\stack.cmd restart`。broker 起来时会在日志里说它用的
  是哪个镜像，**不静默回退**。

**Code 会话在哪个文件夹里，以及它够不着什么：**

- 容器够不着你的磁盘，只够得着一个文件夹：这个 checkout 下的 `var\projects`，
  `stack.cmd` 会建它，Compose 把它挂在 `/projects`，Code 的文件夹选择器就从那里打开
  （[ADR-0109](adr/0109-a-container-lays-the-page-out-and-hands-a-session-one-folder.md)）。
  把要编码的项目放进去，或者从终端里 `set AGENT_WORKBENCH_PROJECTS_DIR=D:\projects`
  再跑 `stack.cmd`。选择器里能看到 `/app` 之类别的目录，别选——那是镜像自己的只读树。
- **这里的编码会话有一把 shell，但不是你这台机器的。** `project_run` 跑在 `runner` 容器里
  （[ADR-0115](adr/0115-a-shell-that-holds-no-key-and-a-browser-that-knows-where-the-page-is.md)）：
  它只挂着 `var\projects`、没有 key、没有数据库地址，里面是 Python 3.12、不带 npm 的 Node 和常规 Unix 工具，
  **没有你装在 Windows 上的任何东西**。每条命令先出现在审批卡上，你点了才跑。
  Mac 上原生跑（`scripts/dev.sh up`）的 demo 档给的才是你机器上的真 shell（ADR-0077）。
  浏览器也有：ADR-0113 的受控 Chromium 在 `browser` 容器里，模型能打开它刚写的页面、读
  可访问性树、截图、看控制台错误——但它出网只经守卫代理，这台机器的桌面浏览器它开不了。
  两样都由 `stack.cmd` 起栈时探一次：`runner` 或 `browser` 容器没起来，那一次启动就没有对应
  的工具，「运行状态」页会说。其余照旧：项目文件的七件工具（读、写、改、列、搜，以及 ADR-0116
  加的删与改名），不挂项目时的 `sandbox_run`（一次性容器、断网），以及打开「联网搜索」后的
  `web_search`。容器栈上输入框旁边还多一档「放手做」（ADR-0116）：命令不再逐条问你，只有会毁掉工作
  的几种形状例外，你事后看记录——它只在命令跑进 `runner` 容器时出现，Mac 原生路径上没有，因为那里
  的命令跑在你自己的机器上。同一条命令连发两次、中间什么也没跑，第二次运行时直接沿用上次的结果，
  不再问你；审批卡上「本会话都允许」对命令也开放了，答应的是那一条命令。右栏「浏览器」那一张
  现在能点、能按键（ADR-0117），而且**什么时候都能**（ADR-0119）：模型正在跑的那一轮里你照样可以
  点画面、按方向键，面板只会说一声「模型这一轮也在动这个页面」，模型则在它下一次浏览器调用上被
  告知页面被人碰过。画面底下那一行是这一页在**浏览器所在那台机器**上的路径——容器栈里是
  `browser` 容器里的路径，形如 `/projects/你的项目/mario.html`。右栏打开一个项目文件之后，页眉上
  有「重命名」和「删除」；打开的是 `.html` 时还有「**在浏览器中打开**」（ADR-0120），把它放进「浏览器」
  那一张——一轮结束之后、或者栈重启把浏览器清空之后，这是把你自己的网页放进去操作的办法。反过来，
  画面底下那行路径在项目目录里时，旁边有「在文件夹中打开」。在画面上**按住**方向键就是按住（按下和
  抬起分开送进浏览器，游戏里的角色会一直走），点一下画面先让它拿到焦点。收起过的右栏由输入框上方
  那颗「文件夹」开关叫回来。一轮跑到步数上限时，最后一步会留给模型写报告，所以「这一轮没有跑完」
  下面仍然能看到它做了什么、还差什么。

---

## 7. 日常操作

```bat
scripts\stack.cmd                :: 构建、启动、等到健康、打开控制台
scripts\stack.cmd lite           :: 同上，但镜像不带 LibreOffice（没有 Word 版面预览）
scripts\stack.cmd status         :: 什么在跑
scripts\stack.cmd logs           :: 跟日志
scripts\stack.cmd restart        :: 只重启沙箱、API 与两个 Worker；数据库与 encoder 不动
scripts\stack.cmd sandbox-image  :: 构建能画 PDF 的沙箱镜像，然后 restart
scripts\stack.cmd down           :: 停掉并删掉容器
scripts\stack.cmd anyway         :: 内存不够也照跑（见 §0）
scripts\shortcut.cmd            :: 在桌面放一个图标，指向上面第一条（一次性）
scripts\shortcut.cmd remove     :: 把那个图标拿掉
```

`restart` 不碰 `encoder`：重启它等于重新加载三个模型，几分钟；而它不读 key、不读开关，
没有理由重启。

### Computer use：在这台 Windows 上另起一个进程

容器够不着桌面，所以屏幕控制 server（`agent-computer-mcp`）在 Windows 主机上原生跑
（[ADR-0108](adr/0108-a-screen-adapter-for-windows-composes-its-own-frame.md)）。
这是整条 Windows 路上**唯一**需要 Python 的地方，而它通过 `uv` 要——`uv` 会自己取一个
3.12——所以要装的只有 `uv`：

```powershell
winget install --id astral-sh.uv -e
```

重开一次终端，然后：

```bat
scripts\computer.cmd
```

它做三件事：`uv sync --frozen --extra computer-use`（Windows 这一半的 extra 只有 Pillow；
pyobjc 那三行带 darwin 标记，跳过），在 `127.0.0.1:8768` 起 server，把每一次「批准哪些应用」
的对话框弹在你屏幕上。**名单每次启动都是空的**，批准只对这次会话有效，默认按钮是「否」。

控制台的「计算机」页通过 API 读它的 `/session`——API 在容器里，经一条
`127.0.0.1:8768 → host.docker.internal:8768` 的隧道够到主机；没起 server 时那一页说
「没在应答」，和一台从没起过它的机器一样。工具本身由本机的 MCP 客户端直连
`http://127.0.0.1:8768/mcp` 调用，不进任务（ADR-075），机器之外谁也够不着。

三件 Windows 上的实情，读之前先知道：

- 截图是把**已批准的窗口逐扇渲染再合成**的，未批准窗口的像素从没被画出来过；代价是一扇被
  未批准窗口挡住的已批准窗口会完整出现在图里（macOS 上会被盖住）。
- 把一扇窗切到前台受 Windows 前台锁限制，server 不会用「敲一下 Alt」那种把戏（那是一次落进
  未批准窗口的按键），切不过去会如实报现在在前面的是谁。**这一段没有在 Windows 上实测过。**
- 前台是提权（管理员）窗口时，Windows 拒绝非提权进程的输入；server 会报错而不是假装点到了。


`down` **保留具名卷**，也就保留了你的 key、开关、文档、向量和已经下好的模型权重。
要连这些一起清掉：

```bat
docker compose --profile demo down -v
```

> **从旧的 demo 摄取切过来的话，这一步是必须的。** 在此之前写进 Qdrant 的是哈希向量，
> 而它们**既不会被清掉也不会被查询侧过滤掉**——`chunk_id` 里含 `index_identity`，
> 所以哈希点和 BGE 点会并存，查询只按租户 / 知识库 / 授权主体过滤。
> 要么删卷，要么换一个 collection 名。

---

### 重开机之后

Windows 重启会把容器全部打死——它们会停在 `Exited (255)`，那是引擎被硬停时容器拿到的
退出码。**没有任何东西会让它们自己回来**：这里没有声明重启策略。

```bat
scripts\stack.cmd
```

不是 `restart`。`restart` 只重启那四个读一次配置的进程，不管它们的依赖；对着一个停着的栈跑
它，会起来一个背后什么都没有的 API，然后报成功。`stack.cmd` 会在这种状态下拒绝执行
`restart` 并告诉你改用哪条命令（实测 2026-09-11，重启后约 70 秒回到全绿）。

Docker Desktop 本身不会自启（`AutoStart=false`），所以先手动打开它，等引擎起来，再跑上面
这条。

#### 让它自己回来

```bat
scripts\autostart.cmd install     每次登录自动把栈起起来
scripts\autostart.cmd remove      不要了
scripts\autostart.cmd status      装没装，上一次跑成什么样
scripts\autostart.cmd run         现在就做一遍登录时做的事
```

`install` 往**启动文件夹**放一个转发用的小 cmd（不是计划任务：`Register-ScheduledTask` 与
`schtasks /create` 在普通登录用户下都答 `Access is denied`，实测 2026-09-11，那条路要提权）。
登录时它等 Docker 引擎答话——最多 20 分钟，因为那是一台刚开机、同时在做所有别的事的机器在起
WSL 2 虚机——然后跑 `docker compose --profile demo up -d --wait`，在最小化窗口里，结果写进
`var\autostart.log`。

**它不构建镜像**，也**不开浏览器**。登录时触发一场几十分钟的构建，比栈没起来坏得多；没人在看一次
登录。镜像不在就让 `up` 失败并记进日志。

还要在 Docker Desktop → Settings → General 勾上 **Start Docker Desktop when you sign in**，
否则引擎不会自己出现。两者不需要协调先后：这个启动项等的是引擎，谁先谁后都行。

实测 2026-09-11：从完全停止的栈到 `/health/ready` 200，**60 秒**，且沙箱与 Worker 的 MCP
工具目录都完整（见下）。

> [!NOTE]
> **为什么不给容器加 `restart: unless-stopped`。**
> 那个显而易见的修法在这台栈上是错的：守护进程重启时 `depends_on` 不生效，容器各自无序拉起，
> 而沙箱开关、联网搜索开关、Worker 的 MCP 工具目录都是**启动时决定一次、此后不再改**的。
> 结果会是一台全绿而悄悄少了功能的部署——比明显没起来更坏。完整论证见
> [ADR-0111](adr/0111-a-reboot-is-recovered-by-the-ordered-start-not-by-restart-policies.md)。

---

### Docker Desktop 自己打不开时

```bat
scripts\docker-unstick.cmd
```

见 §8 最后一段。引擎健康时它拒绝运行，所以误跑一次不会打断正在跑的东西。

---

## 8. 出问题时

| 症状 | 多半是 |
|---|---|
| `no docker on PATH` | 装完没重开终端 |
| 重启电脑之后容器全是 `Exited (255)` | 正常。引擎被硬停时容器就是这个码。跑 `scripts\stack.cmd`，不是 `restart`；想让它自己回来见 §7 |
| 装了 autostart 但登录后栈没起来 | 看 `var\autostart.log`。多半是 Docker Desktop 没自启（Settings → General）|
| `restart only restarts the four processes that read config once` | 栈没起来，`restart` 帮不上。跑 `scripts\stack.cmd` |
| Docker Desktop 弹 `An unexpected error occurred` 然后自己退出 | 孤儿套接字，见本节最后一段。`scripts\docker-unstick.cmd` |
| `Docker is installed but the engine is not running` | Docker Desktop 没启动，或鲸鱼图标还在动 |
| `Docker's engine cannot reach a registry` | Docker Desktop 里存着的手动代理指向一个没人监听的端口。它会把 `settings-store.json` 里那几行原样打出来；Docker Desktop → Settings → Resources → Proxies，把端口改对（Clash 现在多是 7897），或切回系统代理，Apply 并重启 |
| `Docker's disk has about N GB free` | §0 的磁盘那一节。改 Disk image location，或 `docker system prune -a` |
| 构建停在 `unpacking to ...` 不动，日志不再增长，`docker` 命令开始返 500 | 数据卷写满了。这不是卡死，是磁盘满。见 §0 磁盘那一节 |
| 它说内存不够就停了 | §0，改 `.wslconfig` 然后 `wsl --shutdown` |
| `weights-init` 卡住或失败 | 网络。看它打印的 endpoint，然后回到 §3 设镜像站 |
| `the stack did not come up healthy` | `scripts\stack.cmd logs`，看哪个容器在重启 |
| 上传的文档一直是「处理中」 | 摄取 worker 死了，或 `encoder` 没起来。`scripts\stack.cmd logs`，多半是权重或内存 |
| 「运行状态」页说沙箱缺失 | broker 拉不到 `python:3.12-slim`。看 `sandbox` 容器的日志，手动 pull，然后 restart |
| 「计算机」页说服务器没应答 | 没跑 `scripts\computer.cmd`，或它退出了。看那个窗口 |
| Code 里点开 .docx 只有文字，任务页说「没有可用的文档转换器」 | 用 `lite` 构建的，或是 2026-09-04 之前构建的镜像。`scripts\stack.cmd` 重新构建，「运行状态」页 `Word 版面预览` 那一行会变成可用 |
| Code 的文件夹选择器里只有 `/app` 这类目录，写文件报只读 | 镜像是 ADR-0109 之前的。重新跑 `scripts\stack.cmd`；选 `/projects` 下的文件夹 |
| Code 会话说「本环境没有 shell」或「没有浏览器」 | 起栈时 `runner` 或 `browser` 容器没探到（§6）。`scripts\stack.cmd status` 看它们是不是 healthy，然后 `scripts\stack.cmd restart`；「运行状态」页 `code.host_commands` 那一行说当前是哪种情况。联网搜索要开「联网搜索」开关并 restart |
| 控制台开了但 Chat 说它没有联网功能 | 没 key，或 key 存了没 `restart`。见 §5 |
| 任务秒过、报告像模像样但引用是假的 | 合成 Worker。见 §5 那一段 |
| 手敲 `docker compose up --build` 直接死在 gRPC header 上 | §2。用 `stack.cmd`，别用 `--build` |
| 桌面图标画的是通用图标，不是那个白底的 A | Explorer 的图标缓存还留着旧的。注销再登录最稳；图标本身在不在，`scripts\shortcut.cmd` 会自己说 |

`http://127.0.0.1:8000/ui/` 打不开时先 `scripts\stack.cmd status`：API 只映射到
**127.0.0.1**，局域网上访问不到是有意的。

### Docker Desktop 起不来：孤儿套接字

Docker Desktop 弹 “An unexpected error occurred” 然后退出，日志
（`%LocalAppData%\Docker\log\host\com.docker.backend.exe.log`，搜
`backend cancelling with error`）里写的是：

```
starting services: initializing Ingest server:
listening on unix://.../Docker/run/sailor-ingest.sock:
rename sailor-ingest.sock sailor-ingest.sock.stale:
The file cannot be accessed by the system.
```

这是 AF_UNIX 套接字文件。持有它的进程没了之后，Windows 把文件留在一个既打不开、也删不掉、
也改不了名的状态里，而 Docker Desktop 启动时的第一件事就是把它改名成 `.stale`——于是此后
每次启动都死在同一行。

> [!IMPORTANT]
> **这不是强杀 Docker 造成的。** 第一次遇到它是在一次强制停止之后，很容易就当成活该；
> 但它随后在一次返回 0 的、干净的 `docker desktop stop` 之后又发生了一次
> （实测 2026-09-11，Docker Desktop 4.89.0）。正常停止就会留下这些套接字，下一次启动撞上它们。

```bat
scripts\docker-unstick.cmd
```

它把两个目录整个改名挪开——`%LocalAppData%\Docker\run` 和
`%LocalAppData%\docker-secrets-engine`——Docker Desktop 启动时会重建它们。**改名目录有效而
删文件无效**：文件碰不得，但它们的父目录是普通目录。只修第一个的话，崩溃会移到第二个上，
第二个就是这么被发现的。

挪开的 `*.orphan-*` 目录留在原地不删，因为删它们会因同样的原因失败；重启一次 Windows
就能清掉。引擎健康时这个命令拒绝运行，所以误跑一次不会打断正在跑的东西。

---

## 9. 这不是一台可以给别人用的部署

> [!WARNING]
> **Identity Adapter 只信任请求头。** 这套东西只能用于受控的本机开发，
> **不得暴露到局域网或公网**。监听地址与 Compose 端口都限制在 loopback，
> 但那只是防止意外暴露的机制，**不是身份认证**
> （[ADR-044](adr/0044-no-remote-no-production-identity.md)）。

当前 Compose 只用于本机演示，不能作为生产部署或生产级多 Worker 的证明。

---

## 相关

- [Compose 部署](deployment.md)——容器拓扑的细节（英文）
- [本机运行手册](running-locally.md)——macOS / Linux 的原生路径，以及那条 12 GB 的实测
- [ADR-0105](adr/0105-one-command-may-assemble-everything-a-container-can.md)——为什么是这些做法
- [已知缺口](known-gaps.md)——没做的部分，逐条附判据
