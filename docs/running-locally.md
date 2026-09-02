# 在本机跑起来

```bash
scripts/dev.sh up
```

一条命令，从一个刚 clone 出来的 checkout 到一个能打开的控制台。缺 `.venv` 就用 `uv`
建，缺 `.env` 就照 `.env.example` 写一份，然后按顺序把该起的都起起来，每一步等到它
真的就绪再走下一步。

输出的**形状**是这样，每一步带自己的用时（下面这段是示意，不是一次实测的转录——
秒数取决于你的机器和权重有没有缓存）：

```text
  provider key present: the console profile (Word + web + sandbox + Chat)

  [1/9] setup            python env and .env, if this checkout has none
  [2/9] services         PostgreSQL 5433 · Qdrant 6333
  [3/9] migrate          schema to head
  [4/9] mcp-servers      word 8765 · web 8767 · sandbox 8766
  [5/9] mcp-probe        all three answer before anything freezes a catalogue
  [6/9] api              demo-api: Word + web + sandbox + Chat
  [7/9] api-ready        loading BGE-M3; a cold start is minutes, not seconds
  [8/9] ingest           the one absence a browser cannot see
  [9/9] worker           Task worker

console  http://127.0.0.1:8000/ui/
status   scripts/dev.sh status
logs     scripts/dev.sh logs <name>
stop     scripts/dev.sh down
```

第 7 步是那几分钟的去处：API 要先把 BGE-M3 读进来才开始服务。把用时逐步打出来，就是
为了让「它是不是卡住了」有一个不用猜的答案。

**要知识库检索得多一个字。** `up` 不会替你下载几个 GB：

```bash
scripts/dev.sh up --with-retrieval
```

不加这个字也能起，但那台部署**没有检索**——而这正是从浏览器里看不出来的那种缺失：
路由全在、`/health/ready` 回 200、起动还更快（5 秒对两分钟，2026-08-30 实测），只是
上传永远变不成可检索，而摄取 worker 在第一行就退出。所以 `up` 在开始之前先探一次并
把结论说出来（第 2 步），`--plan` 也会说。

配套的三条：

| 命令 | 做什么 |
|---|---|
| `scripts/dev.sh status` | 谁在跑、pid 多少、日志在哪 |
| `scripts/dev.sh logs <name>` | 跟一个进程的日志（名字就是 status 里那一列） |
| `scripts/dev.sh down` | 停掉 `up` 起的每一个进程；**容器留着**，它们装着你的本地库 |

**先看会起什么，而不真的起**：

```bash
scripts/dev.sh up --plan
```

## 顺序不是整齐，是必须

`up` 存在的理由不是少打几个字，是那个顺序**猜不出来也不容错**：

- **`demo-api` 起之前会去探沙箱 MCP server**（8766，`run_python`）。探不到就退出——
  实测对着一个没人听的端口，探针等 11 秒后 exit 1，而 `set -euo pipefail` 让整个 arm
  死在那里。这份手册此前的控制台命令组列了 `word-server` 与 `web-server`，**从没列过
  `sandbox-server`**：照着它一条条敲，`demo-api` 起不来，而报错里不会提这份文档。
- **Worker 的 MCP 工具目录只在启动时冻结一次**，不热更新。晚起的服务器留下的是一个
  健康、正常、却没有那把工具的 Worker——而那把工具正是这份 profile 存在的理由。
- **不起 `ingest`，上传就永远停在「正在索引」**。它同时是创建 Qdrant collection 与绑定
  `knowledge_active` alias 的那个进程，所以不起它就根本没有索引。

这三条以前写在散文里靠人记。现在写在会执行的代码里，并由
`tests/config/test_dev_script_up.py` 钉着。

> **`up` 验到了哪一步，照实写。** 写下它的这台机器上 8000 正被本项目自己的 Compose
> 栈占着，三个 MCP 端口也被上一次手工启动占着，而 swap 已经用掉 7.8 GB——再起一个
> 检索进程会和它们抢内存。所以**没有在这台机器上把 `up` 端到端跑过一次**。
> 已经实测的是：两条 `--plan` 的完整顺序、8000 被占时的拒绝（用一个临时监听端口
> 复现）、`status` 与 `down` 在什么都没起时的行为、`logs` 的名字列表，以及探针对着
> 一个死端口 11 秒后 exit 1。这些测试全部不需要起任何真实进程。
> 端到端那一次留给一台空闲的机器，或者 `docker compose --profile demo down` 之后。

## 和 Docker 那条路有什么区别

**不是同一套东西的两种起法，是两台不同的部署。**

```bash
docker build -t agent-workbench:local . && docker compose --profile demo up -d --wait
```

| | Compose（`scripts\stack.cmd` / `docker compose`） | 原生（`scripts/dev.sh up`） |
|---|---|---|
| 机器要装什么 | 只要 Docker Desktop | Python 3.12 + `uv`（`up` 自己调 uv 装） |
| 知识库检索 | **没有**——镜像 `uv sync` 不带 `embedding` extra | 有，真实 BGE-M3 + 重排，但要 `up --with-retrieval` |
| MCP 工具（Word / web / 沙箱） | **没有**——拓扑里没有这三台 server | 三台都起，且在 Worker 之前 |
| Task Worker | 两个都是 `--demo` 合成 Worker | 真实图 |
| 改一行代码要多久 | 重新 `docker build`，分钟级 | 重启一个进程，秒级 |
| 内存 | 轻，进程里没有模型 | **一个检索进程约 12 GB**，见下 |
| Windows | 唯一的路（`dev.sh` 是 bash） | 走不了 |

所以选哪条不是口味：**要看真实检索、MCP 工具、沙箱或真实图 Worker，只有原生这条路
有**；要一台干净的、能在别人机器上一条命令起来的演示，用 Compose。控制台的「运行状态」
页会把当前这一台**实际装配起了什么**逐行列出来（ADR-102），不用靠猜。

`up` 补的差距不是「原生 vs 容器」，是原生这条路自己的：自 `scripts\stack.cmd` 起
Compose 那边就是一条命令，而原生这边一直是六个终端。

## 没有 provider key 会怎样

`up` 自己看：`AW_SECRETS__DEEPSEEK_API_KEY`，没有就读 `AW_KEY_FILE`（默认
`~/.config/agent-workbench/key`）。

| | 有 key | 没有 key |
|---|---|---|
| 起哪一组 | `demo-api` + `demo-worker` + 三台 MCP server | `api` + `worker`（demo 图） |
| Chat | 有 | **路由根本不注册**，而不是注册了每次调用都失败 |
| 上传 → 向量化 → 索引 | 有 | 有 |
| 提交 Task → 跑完 → 结算 | 有 | 有 |

**key 的路径在 checkout 之外是有意的**——`zip -r` 和访达的「压缩」都不认 `.gitignore`，
工作目录里的 key 会被一起打包带走；CI 的密钥扫描只看提交历史，而这把 key 从没被提交
过。打包只用 `git archive`。

`demo-api` 没有 key 会**拒绝启动**，所以 `up` 在没有 key 时不会去起它。这个拒绝比
`demo-worker` 的更硬：没有 key 时 `_assemble_chat` 吞掉 `ModelNotConfiguredError`，
`chat.router` 与 `events.router` 根本不挂载，这份 profile 打开的 triage 也没了模型、
于是每个 Task 静默回落到 v1 图——而这一切从浏览器上完全看不出来：`/ui` 打得开、页面都在、
Chat 的空状态照常渲染。

**联网搜索是决定出来的，不是写死的**（[ADR-104](./adr/0104-the-native-launcher-yields-to-a-stored-switch.md)）。
`demo-api` 与 `demo-worker` 起进程之前先问 `docker/decide_web_search.py`——和 Compose 的
容器启动脚本同一个文件：shell 里显式导出了 `AW_RESEARCH__ENABLED` 就原样用；控制台
「运行状态」页存下的开关交给加载器应用或搁置；谁也没决定才探 key。

## 想一步一步自己来

`up` 只是把下面这些按顺序调起来，每一条都还在，也都可以单独跑——想盯着某一个进程的输出
时就这么用：

```bash
scripts/dev.sh services   # PostgreSQL 5433 + Qdrant 6333，并建本地库
scripts/dev.sh migrate    # 迁移到 head
scripts/dev.sh api        # HTTP 控制面（--without-chat 连嵌入运行时都不装）
scripts/dev.sh ingest     # 摄取 worker，同时负责创建索引与绑定 alias
scripts/dev.sh worker     # Task worker（--demo 图）
```

控制台那一组要多起三台 server，并且**必须在 API 与 Worker 之前**：

```bash
scripts/dev.sh word-server     # 8765
scripts/dev.sh web-server      # 8767
scripts/dev.sh sandbox-server  # 8766——demo-api 会探它
scripts/dev.sh demo-check      # 一条命令探 word 与 web
scripts/dev.sh demo-api
scripts/dev.sh ingest
scripts/dev.sh demo-worker     # 它自己也会再探一遍 word 与 web
```

还有两组只演示**一样**能力的窄 profile（`word-*` 与 `web-*`），它们被测试钉着保持分离：
每一份 profile 会把自己的工具名冻进每一个新提交的 Task 信封，合成一份就等于把两边的
工具都加给每一个 Task。控制台是一个应用——在 Work 里敲「写一份 Word 报告」的人不是在选
profile——所以并集单独声明成 `config.demo-local.toml`，而不是偷偷塞进哪一个窄的。
（不这么做的后果实测过：web profile 下那个 Task 的信封里根本没有渲染器，模型把 Markdown
写进一个叫 `report.docx` 的文件，控制台展示出一个并不是 Word 的「Word 文档」，
`task_9bb8446a…`，2026-08-12。）

Word MCP 那条路的完整启动、验收、下载与排错见[本地 Word MCP 指南](./word-mcp-local.md)，
设计边界见 [ADR-026](./adr/0026-word-docx-is-an-mcp-artifact.md)。

## 浏览器控制台

`up` 起的 API 已经把控制台挂在同源的 `/ui` 下。要自己构建前端再挂：

```bash
corepack enable
corepack prepare pnpm@11.9.0 --activate
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
PYTHONPATH=src .venv/bin/python -m agent_workbench.apps.api.main --web-dir ./web/dist
```

开发时也可以让 API 运行在 8000，再执行 `pnpm --dir web dev` 并打开
`http://127.0.0.1:5173/ui/`。Vite 会同源代理浏览器看到的 `/v1` 与 `/health`；API
仍不需要开放 CORS。

不给 `--web-dir` 就**根本不挂**——和 `--without-chat` 是同一条原则：一个能打开、然后
每个请求都失败的页面，比一次 404 更糟。目录不存在或没有 `index.html` 会**拒绝启动**，
而不是等到有人用浏览器发现。不要把源码目录 `./web` 直接交给 `--web-dir`：Vite
源码不是可部署静态产物，启动检查会要求只存在于生产构建根目录的 manifest 标记。

三件值得知道的事：

**同源不是图省事。** 浏览器身份是三个请求头。控制台单独起一个端口就要 API 回答
preflight 并允许一份头列表，而那每一条都是「谁可以从哪里调这个 API」的决定——挂在
同源下就没有跨源请求需要放行。

**事件流用 `fetch` 读，不用 `EventSource`。** `EventSource` 的构造函数只接受
`withCredentials`，**设不了身份头**，所以它根本没法对这个 API 认证。自己解析帧的
副作用是好的：`Last-Event-ID` 变成显式发送的游标，正是服务端为它设计的东西。

**Task 是轮询，不是流。** 事件路由按 chat session 挂载，Task 没有流可订阅，所以
控制台按游标轮询它的时间线，Task 到终态就停。这是如实，不是没做完。

## 跑一遍看它是不是真通了

```bash
scripts/dev.sh smoke
```

它会驱动整条链路并把**实际读回来的**事实打印出来：

```text
health
  ready                    200

upload -> document version
  document_id              doc_smoke_b5b6aa1690954c4d
  source_revision          1

ingestion worker -> Qdrant
  points before            2
  points after             3

task -> LangGraph -> settled
  task_id                  task_a5a7693091a547f29baebe42c758a5c0
  status                   succeeded
  timeline                 TaskSubmitted -> TaskClaimed -> TaskSucceeded
```

## 手动驱动

```bash
export PYTHONPATH=src
alias aw=".venv/bin/python -m agent_workbench.apps.cli.main"
IDENT="--tenant-id tenant_local --principal-id user_local"

# 上传一个文档（声明 → 传输 → 完成，三次调用）
aw upload ./README.md $IDENT \
  --document-id doc_readme --knowledge-base-id kb_local --grant user_local

# 看检索到了什么——不需要模型
aw search $IDENT --query "融合在哪里运行" --knowledge-base-id kb_local --timeout-seconds 180

# 提交并观察 Task。--scope 见下面一节
aw task $IDENT --scope artifact:export submit --objective "总结融合是怎么做的" --json
aw task $IDENT list --status queued
aw task $IDENT timeline <task_id>

# 找到并回答等着人的审批
aw approval $IDENT list --status pending
aw approval $IDENT approved <approval_id>

# 把导出的报告读回来
aw artifact $IDENT get <artifact_id> --output ./report.md
```

**身份参数写在子命令之前**（`task $IDENT list`，不是 `task list $IDENT`）——
它们描述的是「谁在调用」，不是「要做什么」。

**`--timeout-seconds` 在这台机器上要调大。** CLI 默认 30 秒，而本机一次检索要
18–82 秒（见最后一节），默认值会得到 `transport_error`。

### `--scope artifact:export`：导出为什么需要它

`export_artifact` 是 v1 唯一会写东西的工具，它声明了 `artifact:export` 这个
permission scope。策略引擎要求**两件事同时成立**：Task 提交时的 envelope 点名了这
个工具（这是部署决定，见 [ADR-015](./adr/0015-export-authorization.md)），并且
**调用方持有该 scope**（这是身份，来自 `x-principal-scopes` 头）。

不带 `--scope artifact:export` 提交的 Task 会一路跑到 `export` 节点然后失败，原因是
`policy_denied`。这是对的：提交这个 Task 的人没有导出权限。

## 几个会咬人的地方（都是实测撞到的）

**8000 被占的时候 `up` 会拒绝，而不是去撞。** 最常见的占用者就是这个项目自己的
Compose 栈——两边都发布 loopback 的 8000。不拦的话，失败发生在 API 加载完 BGE-M3
之后的一句 `address already in use`，读起来像 checkout 坏了，而不像两套栈想要同一个端口。

**端口 5433，不是 5432。** 这台机器自己跑着 PostgreSQL，容器发布到 5432 会被它遮蔽，
症状是一句莫名其妙的 `role "agent" does not exist`——来自一个你根本没启动的服务端。

**本地库和测试库是分开的。** `dev.sh` 用 `agent_workbench_local`。共用测试库会有两个
后果：跑一次测试套件就把你的本地数据清空；以及——这个是实际发生的——你的 Worker 会
领走测试留下的 Task，然后死在一个早被删掉的临时目录里的 artifact 上。

**摄取 worker 第一次启动要两分钟以上。** 它要读几个 GB 的 BGE-M3 权重（dense 与 sparse
两套）。在它就绪之前 `smoke` 会一直等；**每篇文档也要一两分钟**，因为权重是在每个批次
前后加载而不是每进程一次。这是实测数字，不是估计。

**API 不建索引。** `--without-chat` 的 API 根本不构造 Qdrant 客户端——它不检索。
collection 与 `knowledge_active` alias 是**摄取 worker** 启动时创建的
（`qdrant.allow_local_bootstrap`）。所以想看到索引，至少要把摄取 worker 起一次。

**`up` 是启动器，不是 supervisor。** 进程死了不会被拉起来，`var/run/*.pid` 是尽力而为的
记录（pid 会被系统回收）。`status` 只说它看得见的，`down` 也只停它自己起过的。

## 接上一个模型 Provider

两条路都可以，都只需要环境变量、不需要改代码：

```bash
# 云端 DeepSeek
export AW_SECRETS__DEEPSEEK_API_KEY=sk-...
export AW_MODEL__MAIN__MODEL_ID=deepseek-chat AW_MODEL__COMPACT__MODEL_ID=deepseek-chat

# 或本地 OpenAI 兼容服务（DeepSeek 走 OpenAI 协议）
export AW_MODEL__BASE_URL=http://localhost:11434/v1
export AW_SECRETS__DEEPSEEK_API_KEY=ollama
export AW_MODEL__MAIN__MODEL_ID=qwen2.5 AW_MODEL__COMPACT__MODEL_ID=qwen2.5
```

**本地推理那条路没有实测过**——没有起过本地推理服务。

`build_model` 会**拒绝启动**一个它调不通模型的进程，这是对的：一个能通过健康检查、
却每个请求都在 Provider 那里失败的进程，把一次配置错误变成了一场追因路径很长的事故。

## 检索质量

不需要 API，也不需要模型：

```bash
AGENT_WORKBENCH_TEST_QDRANT_URL=http://localhost:6333 \
  PYTHONPATH=src .venv/bin/python scripts/run_rag_eval.py
```

用真实 BGE-M3 对固定语料跑 dense 与 hybrid 两条臂，按 gold set 打分，报告写进
`evals/rag/reports/`。

## 内存下限（2026-07-31 实测）

**一个会检索的进程需要约 12 GB 可用内存。** 混合检索与重排在这个架构里是不变量
（`embedding.sparse_enabled` 与 `reranker.enabled` 都是 `Literal[True]`，配置关不
掉），所以每个检索进程都要同时加载三个模型：dense BGE-M3、sparse BGE-M3、
bge-reranker-v2-m3，合计约 6.7 GB 权重，加上 torch 运行时与余量。

在一台 8 GB 的机器上实测：单次 `retrieve()` 从 18 秒退化到 44 秒（去掉重排），
带重排则是 69 到 82 秒，且三次查询期间 swap 从 3.5 GB 涨到 4.9 GB。**是换页，
不是计算。**

后果，按严重性排序：

- **agentic Chat 用不了**：`knowledge_search` 的工具超时是 30 秒，检索超过它。
  固定两步 Chat 仍然可用——它一次请求只检索一次。
- **一次只跑一个检索进程**。API、摄取 worker 和 `scripts/run_rag_eval.py` 各自
  加载一份完整的模型集；同时跑三个会让机器一直在换页。要跑评测就先把另外两个停掉，
  或者干脆 `scripts/dev.sh down`。

这不是缺陷，是没有写下来的部署下限——现在写下来了。这也是 Compose 那条路更轻的原因：
镜像里根本没有这三个模型。

## 不装 embedding extra 时的 Task Worker

上面那 12 GB 是**会检索**的进程的下限。不打算检索的机器可以完全不装 `embedding`
extra——`pyproject.toml` 里它是可选依赖，CI 也不装，Compose 镜像同样不装——Task Worker
仍然能起来，**但它只跑 `v2_general`**。

装配时 `build_embedder` 返回 `EmbeddingUnavailable`，于是不开 Qdrant 连接、只注册 v2，
并打一条带原因的 WARNING：

```
task_worker_grounding_unavailable
```

只注册 v2 是有意的：v1 的两个研究节点在拿不到 research handlers 时会退成普通的模型调用，
把模型自己写的东西塞进 `evidence_refs`，报告会当成「检索到的证据」引用它。所以装不出检索的
Worker 干脆不注册 v1，v1 的 Task 到了这里 park 成 `waiting_migration` 等一个跑得了它的
Worker。

**必须同时改提交默认值。** `workflow.graph_version` 是 **API** 在客户端不指名 shape 时用的
提交默认值，出厂是 `v1`：

```toml
[workflow]
graph_version = "v2_general"
```

不改的话不会报错，只会所有不指名 shape 的提交都 park。Worker 启动时若发现自己拿到的这个
默认值装不出来，会打一条 `task_worker_default_graph_not_buildable`——但它只看得见自己那份
投影，看不见 API 那份，所以这条日志是提示，不是保证。

仓库里几份 local profile **没有**改成 `v2_general`：这台机器装了完整的检索能力，改它等于
替有能力的部署做决定。要试无检索形态就单开一个 overlay，或者用环境变量
`AW_WORKFLOW__GRAPH_VERSION=v2_general`。
