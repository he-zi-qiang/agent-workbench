# 在本机跑起来

**目标**：不接任何模型 Provider，把「上传 → 真实向量化 → 索引」和「提交 Task →
LangGraph 跑完 → 结算」两条链路真跑通。**Chat 生成不在其中**，原因见最后一节。

## 五条命令

```bash
scripts/dev.sh services   # PostgreSQL 5433 + Qdrant 6333，并建本地库
scripts/dev.sh migrate    # 迁移到 head
scripts/dev.sh api        # HTTP 控制面（--without-chat）
scripts/dev.sh ingest     # 摄取 worker，同时负责创建索引与绑定 alias
scripts/dev.sh worker     # Task worker（--demo 图）
```

## 可选：Word MCP Work 模式

普通五条命令仍使用 `config.local.toml`，MCP 保持关闭。要演示“writer 生成 `.docx` 并经
Gateway/事件流进入 ArtifactStore”，使用独立的 `config.word-local.toml` 命令组，不能把
Word 工具悄悄加给每个普通 Task：

```bash
scripts/dev.sh word-server  # 前台 loopback MCP；Ctrl-C 停止
scripts/dev.sh word-check   # /health + MCP initialize/tools/list
scripts/dev.sh word-api --without-chat # Task 控制面显式选择 Word profile
scripts/dev.sh word-worker  # 真实图；没有 Provider key 会拒绝启动
```

Word Server 必须先于 Worker 启动；Worker 只在启动时冻结一次工具目录。真实 Task 的调用者
还必须持有 `mcp:word` scope，若要执行最终报告导出再加 `artifact:export`。无模型 key 只能
验健康与工具目录，不能把 demo Worker 描述成 Word Task 已闭环。完整启动、验收、下载和
排错步骤见[本地 Word MCP 指南](./word-mcp-local.md)，设计边界见
[ADR-026](./adr/0026-word-docx-is-an-mcp-artifact.md)。

## 控制台要的是两样都在：`config.demo-local.toml`

上面两组命令各演示**一样**能力，两个 profile 也被测试钉着保持分离。但控制台是一个应用：
在 Work 里敲“写一份 Word 报告”的人不是在选 profile，而 web profile 下这个 Task 的信封里
根本没有渲染器——模型于是把 Markdown 写进一个叫 `report.docx` 的文件，控制台展示出一个
并不是 Word 的“Word 文档”（`task_9bb8446a…`，2026-08-12）。

所以并集单独声明成一份 profile，而不是偷偷塞进哪一个窄的：

```bash
scripts/dev.sh word-server   # 8765，先起
scripts/dev.sh web-server    # 8767，先起
scripts/dev.sh demo-check    # 一条命令探两个，缺哪个说哪个
scripts/dev.sh demo-api      # Word + web 都在信封里
scripts/dev.sh ingest        # 摄取 worker：不起它，上传就永远停在"正在索引"
scripts/dev.sh demo-worker   # 两个探针都过了才起 Worker
```

**`ingest` 不是可选的。** 上传分片全部成功之后，文档停在 `processing` 等一个消费者；没有
消费者时，界面上"排队没人取"和"正在向量化"是同一个样子（`knowledge_bases.py` 的
`else_="processing"` 里没有"过期"这个概念），页面只会每 2 秒空转一次。控制台演示前先传一份
测试文件，确认它翻到"可以检索"，再往下走。

**`demo-api` 没有 key 会拒绝启动**，和 `demo-worker` 一样。原因比后者更硬：没有 key 时
`_assemble_chat` 吞掉 `ModelNotConfiguredError`，`chat.router` 与 `events.router` 根本不挂载，
这份 profile 打开的 triage 也没了模型、于是每个 Task 静默回落到 v1 图——而这一切从浏览器上
完全看不出来：`/ui` 打得开、六个页面都在、Chat 的空状态照常渲染。要一个明确不带 Chat 的
API，用 `scripts/dev.sh api`。

**联网搜索在这两个 arm 上是决定出来的，不是写死的**（[ADR-104](./adr/0104-the-native-launcher-yields-to-a-stored-switch.md)）。
`demo-api` 与 `demo-worker` 起进程之前先问 `docker/decide_web_search.py`——和 Compose 的
容器启动脚本同一个文件：shell 里显式导出了 `AW_RESEARCH__ENABLED` 就原样用；控制台
「运行状态」页存下的开关（开或关，`~/.config/agent-workbench/switches.json`）交给加载器
应用或搁置；谁也没决定才探 key，而这两个 arm 走到这里 key 一定在，所以默认是开。探针把
走了哪条路打在 stderr 上。此前这两行是无条件 `export AW_RESEARCH__ENABLED=true`，页面会把
原生路径上的每一次启动报成「启动环境里显式给了这个值」——而没有人给过。

key 从哪来：先看环境变量 `AW_SECRETS__DEEPSEEK_API_KEY`，没有就读 `AW_KEY_FILE`
（默认 `~/.config/agent-workbench/key`）。**这个路径在 checkout 之外是有意的**——`zip -r`
和访达的"压缩"都不认 `.gitignore`，工作目录里的 key 会被一起打包带走；CI 的密钥扫描只看
提交历史，而这把 key 从没被提交过。打包只用 `git archive`。

`demo-worker` 在启动 Worker **之前**依次探两个服务器：MCP 目录只在启动时冻结一次，晚起的
服务器不会热更新，症状是一个健康、正常、却没有那把工具的 Worker。这份 profile 同时按
[ADR-038](./adr/0038-the-export-gate-guards-a-list-not-a-boundary.md) §2.1 关掉导出审批
（仓库默认仍是 `true`），并带上两处实测过的预算上限；Chat 在这份 profile 下是
`retrieval_shape = "routed"`，也就是说只有"语料没覆盖这个问题"那条分支才会去联网搜——这一条
写在 config 里，不在任何启动器里，两种起法拿到的是同一个控制台。

## 浏览器控制台

React 控制台先做锁定构建，再由 API 在**同源**的 `/ui` 下提供 Chat / Work 主界面
以及 Knowledge / Approvals / Evaluation / System 辅助页：

```bash
corepack enable
corepack prepare pnpm@11.9.0 --activate
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
PYTHONPATH=src .venv/bin/python -m agent_workbench.apps.api.main --web-dir ./web/dist
open http://127.0.0.1:8000/ui/
```

开发时也可以让 API 运行在 8000，再执行 `pnpm --dir web dev` 并打开
`http://127.0.0.1:5173/ui/`。Vite 会同源代理浏览器看到的 `/v1` 与 `/health`；API
仍不需要开放 CORS。

不给这个参数就**根本不挂**——和 `--without-chat` 是同一条原则：一个能打开、然后
每个请求都失败的页面，比一次 404 更糟。目录不存在或没有 `index.html` 会**拒绝启动**，
而不是等到有人用浏览器发现。不要把源码目录 `./web` 直接交给 `--web-dir`：Vite
源码不是可部署静态产物，启动检查会要求只存在于生产构建根目录的 manifest 标记。

三件值得知道的事：

**同源不是图省事。** 浏览器身份是三个请求头。控制台单独起一个端口就要 API 回答
preflight 并允许一份头列表，而那每一条都是"谁可以从哪里调这个 API"的决定——挂在
同源下就没有跨源请求需要放行。

**事件流用 `fetch` 读，不用 `EventSource`。** `EventSource` 的构造函数只接受
`withCredentials`，**设不了身份头**，所以它根本没法对这个 API 认证。自己解析帧的
副作用是好的：`Last-Event-ID` 变成显式发送的游标，正是服务端为它设计的东西。

**Task 是轮询，不是流。** 事件路由按 chat session 挂载，Task 没有流可订阅，所以
控制台按游标轮询它的时间线，Task 到终态就停。这是如实，不是没做完。

后三条各占一个终端。都起来之后：

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
它们描述的是"谁在调用"，不是"要做什么"。

**`--timeout-seconds` 在这台机器上要调大。** CLI 默认 30 秒，而本机一次检索要
18–82 秒（见最后一节），默认值会得到 `transport_error`。

## `--scope artifact:export`：导出为什么需要它

`export_artifact` 是 v1 唯一会写东西的工具，它声明了 `artifact:export` 这个
permission scope。策略引擎要求**两件事同时成立**：Task 提交时的 envelope 点名了这
个工具（这是部署决定，见 [ADR-015](./adr/0015-export-authorization.md)），并且
**调用方持有该 scope**（这是身份，来自 `x-principal-scopes` 头）。

不带 `--scope artifact:export` 提交的 Task 会一路跑到 `export` 节点然后失败，原因是
`policy_denied`。这是对的：提交这个 Task 的人没有导出权限。

## 几个会咬人的地方（都是实测撞到的）

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

## 为什么没有 Chat

没有模型 Provider。`build_model` 会**拒绝启动**一个它调不通模型的进程，这是对的：一个
能通过健康检查、却每个请求都在 Provider 那里失败的进程，把一次配置错误变成了一场追因
路径很长的事故。

所以这里选了同一个问题的另一个诚实答案：`--without-chat` 直说这个部署不提供 Chat，
路由**根本不注册**，而不是注册了然后每次调用都失败。

要接上生成，两条路都可以，都只需要环境变量、不需要改代码：

```bash
# 云端 DeepSeek
export AW_SECRETS__DEEPSEEK_API_KEY=sk-...
export AW_MODEL__MAIN__MODEL_ID=deepseek-chat AW_MODEL__COMPACT__MODEL_ID=deepseek-chat

# 或本地 OpenAI 兼容服务（DeepSeek 走 OpenAI 协议）
export AW_MODEL__BASE_URL=http://localhost:11434/v1
export AW_SECRETS__DEEPSEEK_API_KEY=ollama
export AW_MODEL__MAIN__MODEL_ID=qwen2.5 AW_MODEL__COMPACT__MODEL_ID=qwen2.5
```

然后去掉 `--without-chat`。**这条路本轮没有实测过**——没有 key，也没有起本地推理服务。

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
  加载一份完整的模型集；同时跑三个会让机器一直在换页。要跑评测就先把另外两个停掉。

这不是缺陷，是没有写下来的部署下限——现在写下来了。

## 不装 embedding extra 时的 Task Worker

上面那 12 GB 是**会检索**的进程的下限。不打算检索的机器可以完全不装 `embedding`
extra——`pyproject.toml` 里它是可选依赖，CI 也不装——Task Worker 仍然能起来，**但它只跑
`v2_general`**。

装配时 `build_embedder` 返回 `EmbeddingUnavailable`，于是不开 Qdrant 连接、只注册 v2，
并打一条带原因的 WARNING：

```
task_worker_grounding_unavailable
```

只注册 v2 是有意的：v1 的两个研究节点在拿不到 research handlers 时会退成普通的模型调用，
把模型自己写的东西塞进 `evidence_refs`，报告会当成"检索到的证据"引用它。所以装不出检索的
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
