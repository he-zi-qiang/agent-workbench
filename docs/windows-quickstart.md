# 在 Windows 上把整套跑起来

这份文档写给**一台什么都没装的 Windows**。读完照做，你会得到一个浏览器里能点的控制台，
里面装配起了这台部署装配得出的全部能力。

不需要 Python，不需要 `uv`，不需要 Node，不需要 git（可以要，见 §2）。
`scripts/dev.sh` 是 bash，Windows 上没有那条路——这里是**唯一的一条**，
而它从 [ADR-0105](adr/0105-one-command-may-assemble-everything-a-container-can.md)
起不再是被刻意做轻的那一条。

> **一句如实说明，放在最前面。** 这个仓库的测试跑在 POSIX 上。下面每一条 Windows 行为
> 都由 `tests/deployment/test_compose.py` 守着，但它守的是**让那个行为成立的规则**，
> 不是一次 Windows 上的真实运行。规则比运行弱，这里和 README 一样写明。

---

## 0. 先算一笔账：这台机器够不够

**这套栈有四个进程各自加载一整套检索模型**——API、两个 Task Worker、摄取 worker。

这件事只有一个实测数字，而且是原生路径上**单个**进程的：约 **12 GB** 可用内存，
其中约 **6.7 GB** 是 dense BGE-M3、sparse BGE-M3、bge-reranker-v2-m3 三个模型文件本身
（2026-07-31 实测，见[本机运行手册](running-locally.md)）。四个进程的两条线是**在那个
数字上做的算术，不是第二次实测**：

| | 约需 | 意思 |
|---|---|---|
| 硬下限 | **29 GB** | 4 × 6.7 GB，只够放下权重。低于它这套栈不是慢，是一直在换页 |
| 舒服 | **51 GB** | 4 × 12 GB，那个实测数字的四倍 |

`scripts\stack.cmd` 会在**开始构建之前**读 `docker info` 的 `MemTotal` 并按这两条线
决定：低于硬下限它停下来，因为另一种做法是让你等几十分钟的构建与下载，然后看着
`up --wait` 在换页里超时——那读起来像「这个项目跑不起来」，而不像「Docker 只分到了 8 GB」。

**这通常是一个设置，不是硬件上限。** Docker Desktop 在 Windows 上默认把物理内存的一半
交给 WSL 2 虚机。要改：

1. Docker Desktop → Settings → Resources → Memory。
2. 那个滑块的上界由 WSL 2 决定，写在 `%UserProfile%\.wslconfig`：

   ```ini
   [wsl2]
   memory=48GB
   ```

3. 改完在 PowerShell 里 `wsl --shutdown`，再启动 Docker Desktop。

真想在不够的机器上看看会怎样：`scripts\stack.cmd anyway`。

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
上下文（这个拓扑有四个），**且**目录名非 ASCII。`COMPOSE_BAKE=false` 躲不开。

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
命令一起打出来。**双击运行的话这个变量传不进去**，需要镜像站就从终端里跑。

---

## 4. 一条命令

```bat
scripts\stack.cmd
```

在资源管理器里双击也行。cmd 与 PowerShell 都能跑。

它按顺序做这些事，每一步失败都会说是哪一步：

1. 探 Docker（两种失败分开报）
2. 量内存（§0）
3. `docker build -t agent-workbench:local .`——首次要拉 Node 24、Python 3.12 和检索运行时
4. `docker compose --profile demo up -d --wait`——十二个容器，等到全部 healthy，最多 600 秒
5. 打开 `http://127.0.0.1:8000/ui/`

**首次运行按几十分钟算**，绝大部分花在构建镜像和下载权重上。这两件事都只发生一次：
权重落在一个具名卷里，第二次启动是秒级到分钟级。

中途它会打印一句摘要，说这套栈**还缺什么**。那句话是刻意的：一个什么也不说的控制台
会被读成一个坏了的控制台，这件事发生过。

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
| 知识库问答（RAG，真 BGE-M3 + 重排） | 有 |
| 知识库检索 `/v1/search`、知识库页的检索面板 | 有 |
| 文档上传入库（真向量，不是哈希替身） | 有 |
| 提交任务 / 真实图 Worker / 人工审批 | 有 |
| 两个 Worker 的 claim / lease / epoch 竞争 | 有 |
| Word MCP（`render_document`） | 有 |
| web MCP（`fetch_page` / `download_document`） | 有 |
| 对话联网搜索 / 任务联网搜索 | 有（用同一把 provider key） |
| Code 会话 | 有，但其中的 `sandbox_run` 那条臂没有 |
| 任务分流（自动选图） / 子代理委派 | 有 |
| 评测页的报告 | 能看；**从界面发起评测不行**，页面会给你手敲的命令 |
| **任务沙箱执行** | **没有**，见下 |
| **Computer use（只读取用 / 截屏）** | **没有**，见下 |

**两件补不了的，说清楚为什么：**

- **沙箱**：`sandbox_run` 每次调用起一个 `--network=none` 容器，也就是说它需要容器里有
  一个能应答的 Docker daemon。给它挂 `docker.sock` 会把这套拓扑的 `no-new-privileges`
  和 `cap_drop: ALL` 变成装饰——一个能跟 Docker API 说话的进程可以创建特权兄弟容器。
  **那笔交易需要它自己的决定，不是一个 mount**，ADR-0105 明确没有做它。
  所以 `code.sandbox_enabled` 在这个 profile 里是关的：一个「配置里说可以跑代码、
  然后每次调用都失败」的会话，是 ADR-057 §3 拒绝过的那种安排。
- **Computer use**：它的三个依赖全带 `sys_platform == 'darwin'`，适配器在非 macOS 上
  直接抛错。**这不是「暂未支持」，是这条拓扑里补不上。** 控制台的「计算机」页在
  Windows 上只能是不可达，而那正是它预期中的答案之一。

---

## 7. 日常操作

```bat
scripts\stack.cmd            :: 构建、启动、等到健康、打开控制台
scripts\stack.cmd status     :: 什么在跑
scripts\stack.cmd logs       :: 跟日志
scripts\stack.cmd restart    :: 只重启 API 与两个 Worker，数据库不动
scripts\stack.cmd down       :: 停掉并删掉容器
scripts\stack.cmd anyway     :: 内存不够也照跑（见 §0）
```

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

## 8. 出问题时

| 症状 | 多半是 |
|---|---|
| `no docker on PATH` | 装完没重开终端 |
| `Docker is installed but the engine is not running` | Docker Desktop 没启动，或鲸鱼图标还在动 |
| 它说内存不够就停了 | §0，改 `.wslconfig` 然后 `wsl --shutdown` |
| `weights-init` 卡住或失败 | 网络。看它打印的 endpoint，然后回到 §3 设镜像站 |
| `the stack did not come up healthy` | `scripts\stack.cmd logs`，看哪个容器在重启 |
| 上传的文档一直是「处理中」 | 摄取 worker 死了。`scripts\stack.cmd logs`，多半是权重或内存 |
| 控制台开了但 Chat 说它没有联网功能 | 没 key，或 key 存了没 `restart`。见 §5 |
| 任务秒过、报告像模像样但引用是假的 | 合成 Worker。见 §5 那一段 |
| 手敲 `docker compose up --build` 直接死在 gRPC header 上 | §2。用 `stack.cmd`，别用 `--build` |

`http://127.0.0.1:8000/ui/` 打不开时先 `scripts\stack.cmd status`：API 只映射到
**127.0.0.1**，局域网上访问不到是有意的。

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
