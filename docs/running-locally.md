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
IDENT="--tenant-id tenant_local --principal-id user_local"

# 上传一个文档（声明 → 传输 → 完成，三次调用）
.venv/bin/python -m agent_workbench.apps.cli.main upload ./README.md $IDENT \
  --document-id doc_readme --knowledge-base-id kb_local --grant user_local

# 提交并观察一个 Task
.venv/bin/python -m agent_workbench.apps.cli.main task $IDENT \
  submit --objective "总结融合是怎么做的" --json
.venv/bin/python -m agent_workbench.apps.cli.main task $IDENT timeline <task_id>
```

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
