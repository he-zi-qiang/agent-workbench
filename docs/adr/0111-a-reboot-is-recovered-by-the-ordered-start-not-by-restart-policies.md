# ADR-0111：重启之后由那套有序启动接管，而不是由每个容器各自的重启策略

- 状态：Accepted
- 日期：2026-09-11
- 关联：ADR-0105（Compose 装配整套栈——本 ADR 说的是这套装配在机器重启之后由谁重放）、
  ADR-0106（一个进程持有权重，其余向它要——`encoder` 要几分钟才 healthy，是排序问题里最长的那条边）、
  ADR-0107（沙箱 broker 独占 socket——`decide_sandbox.py` 每次启动探一次，探不到就整启期关闭）、
  ADR-104（每次启动决定一次联网搜索，同一个形状）、ADR-0102（一台部署要说得出自己没装配起什么）

## 1. 背景

Windows 重启会把这套栈的每个长跑容器打死。它们停在 `Exited (255)`——引擎被硬停时容器拿到的
码——而 `compose.yaml` 里没有任何一个长跑服务声明重启策略，所以 Docker Desktop 回来之后
一个也不会自己回来。实测 2026-09-11：重启后九个服务全部 `Exited (255)`，控制台无应答。

**显而易见的修法是给它们加 `restart: unless-stopped`，而这台栈上它是错的。**

守护进程重启时 `depends_on` 完全不生效——它只在 `compose up` 里有意义。重启策略让每个容器
各自独立拉起，顺序不受约束。而这套栈里有三处「启动时决定一次、此后不再改」的语义：

- `docker/decide_sandbox.py` 探 broker 的**运行时**，探不到就不设 `AW_SANDBOX__ENABLED`。
  API 与两个 Task Worker 于是带着「沙箱关闭」起来，并且**不会崩**（ADR-0107：`enabled` 为真
  而 broker 不答是启动错误，所以这个探针存在的意义就是不把它设成真）。
- `docker/decide_web_search.py` 同一个形状（ADR-104）。
- Worker 的 MCP 工具目录**在启动时冻结一次，永不重载**
  （`adapters/mcp/registry_source.py`），发现失败是 fail-soft。
  `docker/run-task-worker-local.sh` 的首段注释把这件事写死了：先于服务器启动的 Worker
  不是一个会重试的 Worker，而是一个健康的、永久缺着它赖以存在的那些工具的 Worker。

把这三条和「无序拉起」放在一起，结论是：**重启策略会产出一台看起来全绿、实际悄悄少了功能的
部署**。所有容器 Up、`/health/ready` 200、控制台能打开，而沙箱关着、Worker 手里没有沙箱工具，
唯一的痕迹是没人在看的一行日志。这比「明显没起来」更坏——ADR-0102 立的规矩正是一台部署要说得出
自己缺什么，而这种失败连说都不会说。

`docker compose up -d --wait` 恰恰解决的就是这件事：它遵守 `depends_on`，等 `service_healthy`
（`encoder` 要把三个模型加载并预热完，`start_period` 120 秒、retries 60），等
`service_completed_successfully`（`migrate`、`qdrant-ready`、`provider-key-init`）。
那个顺序是承重的，不是整洁。

## 2. 决定

**长跑服务不加重启策略。** `postgres`、`qdrant`、`otel-collector`、`encoder`、`sandbox`、
`api`、`task-worker`、`task-worker-b`、`ingestion-worker` 保持没有 `restart:` 键。五个一次性
容器保持显式的 `restart: "no"`——这个文件本来就在这条轴上思考过，本 ADR 只是把另一半写明白。

**重启之后的恢复，是重放那套有序启动。** `scripts\autostart.cmd install` 装一个登录时触发的
Windows 计划任务，任务做两件事：等 Docker 引擎答话，然后跑 `docker compose --profile demo
up -d --wait`。不构建镜像（镜像已经在），不开浏览器（登录时弹窗是噪声），把结果写进
`var\autostart.log`。

**它跑的是与手敲同一条路径。** 不是一份平行的启动逻辑——那样会有第二个能回答「这台栈怎么起来」
的地方，而两个答案迟早会分叉。

**Docker Desktop 自启由它自己那个开关管。** `AutoStart`。计划任务会等引擎，所以两者的顺序不需要
协调；引擎在超时内没来，任务就把这件事写进日志并退出，而不是去启动一台半截的栈。

**卸载是一条命令。** `scripts\autostart.cmd remove`。一个装上去之后只能靠翻任务计划程序才能
找回来的东西，不该由一个启动器装上去。

## 3. 不做的

**不加 `restart: unless-stopped`，理由在 §1。** 如果将来要加，前提是先把那三处「启动时决定一次」
改成可重入的：探针要么变成会重试直到依赖出现，要么把结论挪到运行期去查。那是一次单独的改动，
需要它自己的 ADR，因为它动的是 ADR-0107 与 ADR-104 立下的语义。

**不改 Worker 的目录冻结。** 冻结一次是有意的，`registry_source.py` 与 ADR-0107 都写了理由。
本 ADR 不碰它，只是不再制造出「Worker 早于依赖启动」这个局面。

**不让计划任务去构建镜像。** 登录时触发一场几十分钟的构建，是比栈没起来坏得多的事。镜像不在
就让 `up` 失败并记进日志，人再去跑 `scripts\stack.cmd`。

**不做成默认开启。** 装不装是一个关于这台机器开机行为的决定，属于用它的人。

## 4. 证据

- 重启后状态（2026-09-11，真实的 Windows 重启）：九个长跑服务 `Exited (255)`，五个一次性容器
  `Exited (0)`，`/ui/` 与 `/health/ready` 均无应答。
- `scripts\stack.cmd` 在该状态下恢复全绿：exit 0，约 70 秒（同日多次，71s / 82s / 262s，
  最长的一次含一次镜像层校验）。
- `scripts\stack.cmd restart` 在该状态下的行为，以及为此加的闸：见
  `docs/windows-quickstart.md` §7「重开机之后」与 `scripts/stack.cmd` 的 `:restart` 段注释。
- 排序依赖的实际形状：`api` 与两个 Task Worker 同时 `depends_on` 三个
  `service_completed_successfully` 与两个 `service_healthy`；`encoder` 的健康检查
  `start_period: 120s`、`retries: 60`。
