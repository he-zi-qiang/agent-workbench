# ADR-0107：沙箱 broker 独占 Docker socket，经回环隧道被访问

- 状态：Accepted
- 日期：2026-09-03
- 关联：ADR-0105 §4.1（明确不做沙箱——本 ADR 就是它要求的「那笔交易自己的 ADR」）、
  ADR-029（沙箱是纯函数——隔离标志一个字没动）、ADR-057 §3（`SandboxSession.open`
  fail-fast——本 ADR 因此让启动器决定，而不是让 profile 声明）、ADR-104（原生启动器对存下的
  开关让路，用同一个探针——本 ADR 沿用「启动器探一次、显式值不碰」的形状）

## 1. 背景

ADR-0105 把沙箱留在了「装配不出」那一栏，理由是具体的：`sandbox_run` 每次调用起一个
`--network=none` 容器，它的 server 要一个能应答的 Docker daemon；把 `docker.sock` 挂进
API 或 Worker 的容器，就把 `no-new-privileges` 与 `cap_drop: ALL` 变成装饰——一个能跟
Docker API 说话的进程可以创建特权兄弟容器，而 API 容器里同时还躺着 provider key、数据库
连接与每一个工作区。

那段话的每一个字都对，而它论证的是「**不能挂进那些容器**」，不是「不能挂进任何容器」。

## 2. 决定

**一个只跑 `agent-sandbox-mcp` 的容器持有 socket，其它容器一律不持有；API 与 Worker
通过两端都是回环的 TCP 隧道访问它；沙箱开不开由启动器按 broker 的运行时探针逐次启动决定。**

具体是五件事：

1. `compose.yaml` 新增 `sandbox` 服务：`agent-workbench:local` 镜像、
   `docker/run-sandbox-local.sh`、挂 `/var/run/docker.sock`、**硬化锚点原样套上**
   （`read_only`、`no-new-privileges`、`cap_drop: ALL`）、`user: "0:0"`（§3.2）。
   它不挂 key 卷、不挂 artifact 卷、不读配置文件、不连数据库。
2. `Dockerfile` 从 Docker 官方 CLI 镜像复制一个静态 `docker` 二进制进主镜像（§3.3）。
3. `docker/loopback_proxy.py` 参数化监听地址与上游地址，从「向外」多出「向内」一种用法：
   在 API 与 Worker 容器里各起一个 `127.0.0.1:8766 → sandbox:8766` 的隧道；broker 容器
   里则是 `0.0.0.0:8766 → 127.0.0.1:8776`（§3.4）。
4. `docker/decide_sandbox.py`：启动器探 broker 的 `/health`，等到 `container_runtime_available`
   为真才导出 `AW_SANDBOX__ENABLED=true`（Worker）与 `AW_CODE__SANDBOX_ENABLED=true`（API）；
   显式给了值的变量不碰；等不到就留关并说明（§3.5）。
5. `scripts\stack.cmd` 新增 `sandbox-image`，`restart` 多重启 `sandbox`。

## 3. 为什么是这些做法而不是别的

### 3.1 这笔交易到底买了什么、卖了什么

原生路径上（`scripts/dev.sh sandbox-server`），沙箱 server 以使用者本人的账号跑，
手里有 docker、有 `~/.config/agent-workbench/key`、有整台机器。容器路径上它现在只有
socket：一个攻破它的人拿到的是 daemon，**再没有别的**——没有 key（不挂那个卷），没有
数据库（不读 DSN），没有工作区（不挂 artifact 卷）。这比原生路径**严格更窄**。

而 API 与 Worker 得到的是什么也没变：它们的容器里没有 socket，`cap_drop: ALL` 仍然
是真的，ADR-0105 那句「一个能跟 Docker API 说话的进程」在它们身上不成立。

代价要说清楚：**攻破 broker 仍然等于 root on the VM**。这条 ADR 没有让那件事不成立，它
让那件事只在一个只做一件事、什么也不持有的进程上成立。broker 的攻击面是 `run_python`
的输入契约（`contract.py`，封闭、有界、不接受路径）与 MCP 传输；模型写的脚本从来
不在 broker 进程里执行，它在 broker 用固定标志起的子容器里执行。

### 3.2 root，因为 socket 是 root 的

Docker Desktop 的 VM 里 `/var/run/docker.sock` 是 `root:root 660`。非 root 用户要
`group_add: ["0"]`，那是把 root 组给它——比直接用 uid 0 更绕，买到的一样。uid 0 加
`cap_drop: ALL`：打开 socket 靠的是**所有者位**（DAC），不需要任何 capability；此外 uid 0
平时能做的事——挂载、改网络、发原始包、越过文件权限——一件也做不了。

`tests/deployment/test_compose.py::test_every_application_service_keeps_the_hardening`
对这个服务照常断言三样硬化；新增一条断言**恰好一个**服务挂 socket，且不是 api / worker。

### 3.3 CLI 进主镜像，不建第二个镜像

`executor.py` 对着 `docker run` 的 stdin/stdout 说话，ADR-029 的隔离测试也对着它跑。换成
Docker Engine HTTP API 是重写 executor 外加一段 hijacked attach 的流协议，为了省一个
40 MB 的静态二进制。不值。

二进制从 `docker:29-cli` 镜像 `COPY --from`，走的是构建已经在用的那个 registry；不加 apt
源。放进主镜像而不是第二个镜像：没有 socket 的 CLI 是惰性的——另外四个容器有这个文件而
没有可指的东西，和没有一样；而第二个镜像是 `stack.cmd` 要多解释的一步构建。

### 3.4 隧道不是绕过守卫，是守卫为之而在的东西

ADR-0105 §3.1 数过三道互相独立的守卫：`--host` 是回环白名单；MCP SDK **正因为** host 是
回环才打开 Host 头校验；`validate_endpoint` 要求非回环 http 必须 HTTPS。它当时的结论是
「三处代码改动，换来的拓扑一样什么也不多买」，于是 word / web 做成 sidecar。

沙箱不能做 sidecar：那等于三个容器各挂一个 socket，正是 §1 拒绝的形状。

隧道让三道守卫**一个字不改地成立**：API 容器里的客户端拨 `127.0.0.1:8766`（settings
校验通过，回环），发出 `Host: 127.0.0.1:8766`；隧道逐字节转发到 `sandbox:8766`，那头的
隧道再转到 broker 容器里 `127.0.0.1:8776` 上的 server，它是用 `--host 127.0.0.1` 起的，
SDK 的允许列表是 `127.0.0.1:*`（实测 mcp 2.x 源码，端口通配），Host 头校验通过。

这不是绕过。三道守卫没有一道是为了阻止**同一部署的两个进程**说话而写的，它们写下来
是为了阻止一个进程**监听在陌生人够得着的地方**——一条两端都是回环监听的隧道没有在任何
新的地方监听。broker 容器里那一端向外的 `0.0.0.0:8766` 与 API 容器早就有的
`0.0.0.0:8000 → 127.0.0.1:8001` 是同一件东西。

`docker/loopback_proxy.py` 的 docstring 现在完整地写着这段论证，因为下一个读到「两端回环」
的人会先问它是不是在作弊。

### 3.5 启动器决定，profile 不声明

`code.sandbox_enabled = true` 而沙箱不应答，API **拒绝启动**（ADR-057 §3，fail-fast 是对的）。
broker 首次启动要拉 `python:3.12-slim`，socket 挂载在某台机器上可能失败，某个人可能把
`sandbox` 从拓扑里拿掉——任何一种都会让一个写死 `true` 的 profile 变成「整栈起不来」，在首次
运行三十分钟之后。这正是 ADR-0105 §3.3 为 key 拒绝过的形状，ADR-102 §3 为 `research.enabled`
拒绝过的形状。

所以和 web search 完全一样：profile 里留 `false`，启动器探一次，探到就导出 `true`，操作者
显式给了值就不碰。探的是 broker `/health` 里的 `container_runtime_available`——那条路由在
`docker version` 答上来之前是 503（ADR-029 §3.6 的 health 契约），探针等它变 200，最多 90 秒。

Worker 侧本来就 fail-soft（自己再发一次真的 `run_python`，失败只记日志），但 Worker 的
投影在 `[sandbox].enabled` 为假时**根本不探**，所以两个启动器用同一个探针、导出同一个变量，
API 冻进信封的与 Worker 注册的才是同一件事。

broker 自己的 Compose 健康检查因此是「HTTP 应答即健康」而不是「运行时应答即健康」：
后者会让 `up --wait` 因为一次慢拉取而判整栈失败。真正的判断留给两个启动器。

## 4. 后果

- Compose 栈有沙箱了：Code 会话的 `sandbox_run` 臂与 Task 的 `sandbox_run` 工具在 broker
  运行时应答时都在，控制台「运行状态」页 `task.sandbox` 一行相应变绿。
- 一个新的拒绝理由进入「运行状态」页的补法文案：`docker compose logs sandbox`。
- `stack.cmd restart` 多重启一个容器，仍是秒级。
- **证据口径：Tested，不是 Demonstrated。** `tests/deployment/test_compose.py` 断言拓扑
  （socket 只在一处、隧道先于进程、探针在启动器里）；`tests/deployment/test_sandbox_decision.py`
  与 `test_loopback_proxy.py` 在真实回环 socket 上跑探针与隧道。**没有在任何机器上把这套
  容器栈端到端起过一次**（本机镜像未构建，Windows 一次也没跑过）。特别是 §3.2 那句
  「Docker Desktop 的 socket 是 root:root 660」是从文档与经验写下的，不是这台机器量的。

## 5. 被拒绝的方案

**docker-socket-proxy 一类的 API 白名单代理。** 它限制的是能调哪些 endpoint，而 `--network=none`
等隔离标志是 `containers/create` 的**参数**，代理挡不住一个改了参数的 create。它多一个镜像、
多一层，没有换来 §3.1 之外的任何东西。

**长驻沙箱容器 + 共享卷排队。** broker 容器 `network_mode: none`、不挂 socket，请求文件写进
共享卷、它轮询执行、写回结果。隔离故事最干净，但它推翻 ADR-029 §3.1「一次调用一个容器、
调用之间无状态」——长驻容器的文件系统在两次调用之间存在，而那是重放保证的前提。

**在 broker 里用 subprocess 直接跑脚本。** ADR-029 §5 第一条，理由不变。
