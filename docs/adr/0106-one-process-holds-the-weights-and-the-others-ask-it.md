# ADR-0106：权重只住在一个进程里，其余进程去问它

- 状态：Accepted
- 日期：2026-09-03
- 关联：ADR-0105（一条命令可以装配起容器装配得出的全部——本 ADR 推翻它 §3.4 的
  「四个进程各加载一整套模型」这一前提，并把它算出的两条内存线换掉）、
  ADR-013（sparse 必须来自 FlagEmbedding——识别检查照旧，只是多了一处要检查的地方）、
  ADR-042（阻塞调用有界——编码器进程里每个请求都是阻塞调用，那个池就是它的全部并发）、
  ADR-017 / ADR-033（检索与融合的所有者**不变**：融合仍在调用方进程内做一次）

## 1. 背景

ADR-0105 让容器这条路装配起了真实检索，代价写在它自己的 §3.4 里：**四个进程各加载
一整套检索模型**——API、两个 Task Worker、摄取 worker——于是 `scripts\stack.cmd` 在
构建之前先量内存，低于约 29 GB 停下来，低于约 51 GB 先说一声。

使用者的机器是 32 GB。Docker Desktop 默认交给 WSL 2 一半，16 GB；改 `.wslconfig` 把它
推到 24 GB 之后，仍然在 29 之下。**这条路在一台完全正常的 Windows 上根本起不来**，而
它是 Windows 上唯一的路。

再看那四份模型是什么。dense BGE-M3 与 sparse BGE-M3 是**同一份权重**（XLM-RoBERTa-large，
约 2.2 GB），经两个不同的库各加载一次；bge-reranker-v2-m3 又是一份约 2.2 GB。所以「6.7 GB」
是三份 2.2 GB，其中两份是同一个模型——而这三份再乘四。**问题不在模型有多大，在同一份权重
被装进了几个进程。**

## 2. 决定

**一个部署里只有一个进程加载检索模型：`agent-encoder`。API、两个 Task Worker 与摄取
worker 通过 HTTP 向它要向量与分数，自己不装 torch。**

具体是四件事：

1. 新进程 `apps/encoder/`：加载 dense、sparse、reranker 各一次，预热，然后在五条路由上
   服务——`/identity`、`/health`、`/embed`、`/sparse`、`/rerank`。没有 dense 模型就拒绝
   启动；sparse 或 reranker 缺席就照常服务并在 `/identity` 里如实报 `null`。
2. 三个远程适配器（`adapters/encoder/`）：`RemoteEmbedder`、`RemoteSparseEncoder`、
   `RemoteReranker`，各实现一个已有的 port。它们在**连接时**做本地适配器在**加载时**做的
   每一项检查（宽度、词表、身份），并多做一项本地适配器不需要做的（§3.2）。
3. 两片配置叶子：`rag.embedding.service_url` 与 `rag.reranker.service_url`，默认空。
   空就在本进程加载，非空就问那个地址。三个工厂（`build_embedder` /
   `build_sparse_encoder` / `build_reranker`）各读自己那一片，在同一处分叉。
4. Compose 拓扑多一个 `encoder` 服务；`hf_cache` 卷与 `weights-init` 依赖从另外四个服务上
   拿掉，只留给它；那四个改为 `depends_on: encoder: service_healthy`。
   `scripts\stack.cmd` 的两条线从 29 / 51 改为 12 / 16。

## 3. 为什么是这些做法而不是别的

### 3.1 一个服务，不是「让 dense 与 sparse 共享一份权重」

后者也真，也值得做：它把每个进程的权重从 6.7 GB 降到 4.5 GB。但它不改变「乘四」，
四个进程仍要 18 GB 权重，仍在 16 GB 之上。而且它要让 sentence-transformers 与
FlagEmbedding 共用一个 torch module，那是 ADR-013 明确绕开的那种耦合。做成服务之后，
重复加载只发生**一次**而不是四次，那个优化从「必需」变成「可选」，留作后续。

### 3.2 远程适配器多做一项检查：身份

本地适配器的 `identity` 是**从配置拼出来的**（`model_id@revision`），所以不可能与配置
不一致。远程适配器的身份是**另一个进程报回来的**，那个进程读的是它自己的配置文件——两个
进程读两份文件，就是摄取 worker 用一个模型写索引、API 用另一个模型查索引的来路。所以
`connect()` 把报回来的身份与本进程配置拼出的那个比，不一致就拒绝启动并把两个都写进去。
宽度与词表的检查原样照搬（ADR-013 那条「不是分词器词表宽度的就不是 lexical weights」
在线上仍然成立）。

### 3.3 明文 HTTP，理由和 Qdrant 一样，与 MCP endpoint 不一样

`mcp.servers.endpoint` 非回环必须 HTTPS，因为那条连接会带 provider key。这条连接上走的是
租户文本与向量，**没有凭据**，而且是同一张私有 Compose 网络——`http://qdrant:6333` 在
同一张网上明文带着同样的文本。所以校验器和 `qdrant.url` 一样：禁 userinfo、禁 query、
禁 fragment，允许 http 到具名主机。

编码器进程的 `--host` 因此允许 `0.0.0.0`，而且是四个项目自有 server 里唯一允许的一个：
它不是 MCP server、不持有身份、不接受工具提案，只答本部署的其他进程——这正是 Qdrant
在同一张网上的形态。原生路径永远不传这个值。

### 3.4 健康探针等的是「热」，不是「起」

一个加载完的模型不是一个能用的模型：`bootstrap/encoder_warmup` 记着 MPS 上第一次前向
29.4 秒、之后 0.06 秒。`/health` 在预热完成之前答 503，`depends_on: service_healthy`
于是把这 29 秒吃在启动期，而不是吃在某个人的第一次请求上。

### 3.5 两条内存线怎么定

这个仓库不写没量过的数。唯一的实测仍是那一个：原生路径上**一个**加载模型的进程约 12 GB
（2026-07-31）。现在恰好只有一个这样的进程，所以硬下限就是它：**12**。另外三个 Python
进程、PostgreSQL、Qdrant 与 collector **没有量过**，16 与 12 之间那 4 GB 是**留量**，
在 `stack.cmd` 里写明是留量。结果是一台 32 GB 的 Windows 在 Docker Desktop 默认设置
（16 GB）下不用改任何东西就过了第二条线。

## 4. 后果

### 4.1 得到的

- Windows 那条路在 16 GB 的 Docker 上能起来。
- API、Worker、摄取 worker **不再 import torch**：`tests/apps/test_encoder_service.py`
  把 `sentence_transformers` / `FlagEmbedding` / `torch` 全部置为不可导入后，三个工厂
  仍然交出远程适配器。
- 检索质量的**语义不变**：向量是同一个模型算的，融合仍在调用方进程内做一次（ADR-033），
  ACL 过滤与 publish fence 一个字没动。远程适配器测的是「一个向量经过线路后逐字节不变、
  顺序不变」，不是「向量意味着什么」。

### 4.2 代价

- 每次 embed / rerank 多一次回环 HTTP 往返，请求体是 JSON。**没有量过这个开销**；它对
  摄取（成批）几乎不可见，对单次查询是毫秒级的猜测而不是数字。
- 一个新进程要起、要等它健康。冷启动多出的时间是它一个人的加载时间，不再是四个进程
  并行加载的时间——没量过，但方向确定。
- 编码器是单点：它死了，四个进程同时失去检索。Task Worker 与 API 对此的处理和
  `EmbeddingUnavailable` 一样——启动时报一次、降级、不退出；运行中的调用会抛
  `EncoderServiceUnavailableError`，不会静默返回空。
- 原生路径（`scripts/dev.sh`）**这次没有接上**：它的 profile 仍在本进程加载，
  `demo-api` 与 `ingest` 仍各持一份。两片叶子对它同样可用，接上是一次 `up` 顺序的改动，
  留待另一批。

### 4.3 证据口径

**Tested，不是 Demonstrated。** `tests/apps/test_encoder_service.py` 用确定性替身
在真实回环 socket 上跑通三个适配器与服务端的全部路由；`tests/config/test_encoder_settings.py`
钉住两片叶子与投影；`tests/config/test_compose_profile.py` 钉住 Compose profile 指向
`encoder`；`tests/deployment/test_compose.py` 钉住拓扑。本机没有装 `embedding` extra，
**没有用真实权重起过 `agent-encoder`**；Windows 上一次也没跑过。

## 5. 被拒绝的方案

**只让 Worker 不加载模型，API 与摄取 worker 照旧。** 两个进程仍是 2 × 12 = 24 GB，
还是要改 `.wslconfig`。而且 Worker 的 `research_internal` 需要检索，砍掉它就回到
ADR-0105 之前「v2-only Worker」的形态。

**把模型换成更小的。** 改的是检索质量与索引身份（每一个已经写进 Qdrant 的向量都得重算），
是 ADR-013 / ADR-017 那一层的决定，不是部署层的。

**gRPC / 二进制协议。** 少几个字节，多一个依赖与一套 stub；这条线路上的瓶颈是模型前向，
不是 JSON。需要时再换，port 不变。
