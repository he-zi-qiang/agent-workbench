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
