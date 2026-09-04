# ADR-0105：一条命令可以装配起容器装配得出的全部

- 状态：Accepted。**§3.4 的前提与 §4.1 的两条「装配不出」已于同日被推翻**：
  [ADR-0106](./0106-one-process-holds-the-weights-and-the-others-ask-it.md) 让权重只住在
  一个进程里（四个进程各一份 → `encoder` 一份，内存线 29 / 51 → 12 / 16）；
  [ADR-0107](./0107-the-sandbox-broker-alone-holds-the-socket.md) 是 §4.1 要求的「那笔交易
  自己的 ADR」，socket 只挂进一个只跑沙箱 server 的容器；
  [ADR-0108](./0108-a-screen-adapter-for-windows-composes-its-own-frame.md) 给 Windows 一个
  屏幕适配器，server 在容器外的主机上跑。本 ADR 其余部分（profile、sidecar、`--demo` 由 key
  决定、`weights-init`、先量内存）原样成立。
- 日期：2026-09-03
- 关联：ADR-044（不远程、不生产身份）、ADR-057（会话不得提供它兑现不了的工具）、
  ADR-101（控制台可以交出一把它读不回来的 key）、ADR-102（一台部署要说得出自己没装配起什么）、
  ADR-103（附加零件可以从控制台上拨动）、ADR-104（原生启动脚本对存下的开关让路）

## 1. 背景

Windows 上只有一条路：`scripts\stack.cmd` → Compose。`scripts/dev.sh` 是 bash。

那条路起来的东西，比这个仓库的读者以为的少得多，而且**少在看不见的地方**：

- `compose.yaml` 里**没有任何一个服务设 `AW_CONFIG_FILE`**，所以整栈加载
  `config.default.toml`——`[mcp] servers = []`、`optional_labs.mcp_adapter = false`、
  triage / Code / 子代理委派全是关的。这不是一个关于这台部署的决定，这是**没有人做决定**
  时会发生的事。
- `Dockerfile` 的 `uv sync` 不带 `--extra embedding`，于是 `build_embedder` 返回
  `EmbeddingUnavailable`：Chat 没有知识库、`/v1/search` 这条路由**根本没被挂载**、
  摄取 worker 写进去的是哈希向量。
- 两个 Task Worker 都带 `--demo`。`workflows/demo_handlers.py` 自述是 demonstration
  fixture，**既不联系 provider 也不执行工具**；approval 节点写死 `approved`，自己批自己。
  任务会走到 `succeeded`，而从未有过一次模型调用或工具调用。

前两条 ADR-102 已经让控制台说得出口了。第三条说不出——控制平面没有 Worker 上报通道，
能力表只能把 `task.worker` 报成 `unknown`。

`docs/running-locally.md` 把这件事写成了一条选择题（「要看真实检索、MCP 工具、沙箱或
真实图 Worker，只有原生这条路有」），那对 macOS / Linux 的读者是准确的，对 Windows 的
读者不是一个选择：**那边没有第二条路**。

## 2. 决定

**容器这条路装配起一台 Linux 容器拓扑装配得出的全部，并且把装配不出的那些说清楚。**

具体是五件事：

1. 镜像带 `--extra embedding`。检索是这个项目最花力气的一半，而它的缺席在浏览器里
   完全看不出来——控制台又快又健康，只是什么也检索不到。
2. 新增第十一个 profile `config/config.compose-local.toml`，由 `AW_CONFIG_FILE` 指名。
   MCP adapter、Word / web 两台 server、triage、Code、子代理委派都在里面。
3. Word 与 web MCP server 作为 **loopback sidecar** 跑在每个 Worker 容器里，由
   `docker/run-task-worker-local.sh` 先起、先探、再 exec Worker。
4. 摄取 worker 去掉 `--demo`；Task Worker 的 `--demo` 改为**由 key 在不在决定**，
   没有 key 时退回合成 handler 并把这件事打进日志与控制台。
5. 权重由一次性服务 `weights-init` 取到具名卷，`scripts\stack.cmd` 在构建之前先量内存。

## 3. 为什么是这些做法而不是别的

### 3.1 sidecar 而不是独立 service

跨容器网络访问 MCP server 要连破三道互相独立的守卫：四个 `main.py` 的 `--host` 是
loopback 白名单（argparse `choices`）；MCP SDK **正因为** host 是 loopback 才打开
Host 头校验；`MCPServerSettings.validate_endpoint` 规定非 loopback 的 http endpoint
必须是 HTTPS。三处代码改动，换来的拓扑一样什么也不多买——两台 server 都是无状态的，
而唯一连接它们的进程就是 Worker。

**顺序是硬约束不是整齐。** Worker 在启动时冻结一次 MCP 工具目录，从不热重载；发现失败
是 fail-soft，只记一条 `mcp_connection_failed` 然后继续。所以起早了的 Worker 不是一个
会重试的 Worker，是一个**健康地、永久地少了它赖以存在的那些工具**的 Worker。
`/health` 不足以当门：两台 server 从 uvicorn 绑上端口那一刻就回 `ok`，早于 MCP 应用
能列出任何一件工具。所以门是 `scripts/smoke_mcp_server.py`——真 MCP 客户端，断言工具名。

### 3.2 权重不烤进镜像，但也不能等到第一次用时下载

**稀疏那半在冷缓存上是拒绝启动，不是去下载。** `adapters/embedding/bge_sparse.py` 用
`try_to_load_from_cache` 查 `sparse_linear.pt`，查不到就抛——因为 FlagEmbedding 自己的
行为是新建一个随机 `Linear` 然后继续，产出宽度正确、含义为零的向量。所以一个空缓存不会
让首次启动变慢，它会让**四个进程同时退出**。

烤进镜像也不行：几个 GB 落在层里，每次重建都要重新拉，而构建机即使旁边就有一个热卷也
必须能连上 Hugging Face。

于是 `weights-init`：一次性、跑完退出、`api` 与三个 worker 都 `depends_on` 它
`service_completed_successfully`。

`HF_ENDPOINT` 透传但**默认不设**。把每一台部署的模型权重指向第三方镜像站是一个供应链
决定，属于跑这套栈的人，不属于这个文件。`docs/windows-quickstart.md` 点名了在中国大陆
网络下让它跑完的那一个，作为一行**有人特意敲下**的东西。

### 3.3 没有 key 时退回合成 handler，而不是退出

这跟 `scripts/dev.sh demo-api` 在同样缺 key 时的选择是**相反的**，而相反是对的：
那个启动器跑在有人盯着的终端里，退出本身就是一句话。这个跑在 `docker compose up -d
--wait` 底下，一个退出的容器不是一句话——它是整栈起不来，在首次运行三十分钟之后，
为了「还没输 key」这个完全正常的状态。

代价必须说清楚，而且必须说在人看得见的三个地方（容器日志、`stack.cmd` 的首启摘要、
控制台「运行状态」页）：**一个合成 Worker 会把任务带到 `succeeded`，一次模型调用和一次
工具调用都没有发生过，而从控制台上看它和真 Worker 一模一样。**

### 3.4 先量内存

四个进程各加载一整套检索模型。这件事只有一个实测数字，而且是原生路径上**单个**进程的：
约 12 GB 可用内存，其中约 6.7 GB 是三个模型文件本身（2026-07-31，
`docs/running-locally.md`）。两条线是**在那个数字上做的算术，不是第二次实测**：
4 × 6.7 ≈ 29 GB（只够放下权重），4 × 12 ≈ 51 GB。

不问的代价是具体的：几十分钟构建加下载，然后 `up --wait` 在换页里超时——读起来像
「这个项目跑不起来」，而不是「Docker 只分到了 8 GB」。

## 4. 后果

### 4.1 这台部署仍然装配不出的

- **沙箱执行**：`sandbox_run` 每次调用起一个 `--network=none` 容器，需要容器内有能应答的
  Docker daemon。挂 `docker.sock` 会把 `compose.yaml` 的 `no-new-privileges` 与
  `cap_drop: ALL` 变成装饰——一个能跟 Docker API 说话的进程可以创建特权兄弟容器。
  **那笔交易需要它自己的 ADR，不是一个 mount。** 本 ADR 明确不做它。
- **Computer use**：`computer-use` extra 的三个依赖全带 `sys_platform == 'darwin'`，
  `adapters/screen/__init__.py` 在非 darwin 直接抛错。这不是「暂未支持」，是这条拓扑里
  补不上。
- **控制台分不出合成 Worker 与真 Worker**：需要一条 Worker → 控制平面的心跳上报，
  那是改控制平面的事实来源，要另一条 ADR。在它落地之前，这件事靠三处文字说，不靠界面。

### 4.2 代价

- 镜像新增 54 个包，按 `uv.lock` 记录的压缩 wheel 求和为 x86_64 2.88 GB / arm64 3.00 GB。
  **解压后的镜像体积与构建耗时本 ADR 不给数字**——没有实测过，而这个仓库不写没量过的数。
  量到之后写进 `Dockerfile` 的注释里，连日期一起。
- 首次启动要下载约 6.7 GB 权重。
- 从 demo 摄取切到真摄取时，此前写进 Qdrant 的哈希点**既不会被清掉也不会被查询侧
  过滤掉**：`chunk_id` 含 `index_identity` 所以两种点并存，而查询只按
  tenant / kb / authorized_principals 过滤。必须删卷或换 collection 名。

### 4.3 证据口径

本 ADR 落地时的证据只到 **Tested**。`tests/deployment/test_compose.py` 与
`tests/config/test_compose_profile.py` 断言的是结构与规则，而这个仓库的 Windows 相关
测试一律跑在 POSIX 上——它们断言的是「让 Windows 行为成立的那条规则」，不是「在
Windows 上跑过了」。**任何文档不得把这条路写成已经在 Windows 上跑通。**
