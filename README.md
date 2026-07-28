# Agent Workbench

中文 | [English](README.en.md)

Agent Workbench 是一个面向校招与作品集展示的 clean-room 通用 Agent
平台项目，目标是提供两种产品模式：

- **Chat Mode**：多轮对话、知识库问答和带权限校验的 RAG；
- **Task Mode**：可恢复的 LangGraph 工作流和可控 Multi-Agent 协作。

项目的自研 Agent Runtime 保持框架无关。LlamaIndex、LangGraph、LangChain
以及后续对比框架都通过明确的 Port/Adapter 接入，不接管核心 Tool Loop。

## 当前状态

截至 2026-07-28，主分支基线为 `main@4d03f69`；当前开发分支已完成
PR-035～PR-039 的安全发布、多轮上下文、EventLog 可演进回放与幂等 Chat Turn
可靠性切片。已经实现并有测试证据：

- 框架无关的 Domain、Ports、Fake Adapter 与可复现 CLI 演示；
- 自研 `ClaudeLikeAgentRuntime`：Tool Loop、schema/Policy Gateway、预算与
  deadline、取消、并行只读调度、exclusive 屏障和 Hook Bus；
- DeepSeek OpenAI-compatible 流式 Adapter、配置投影与 API 装配；
- PostgreSQL ConversationStore、Alembic 迁移、Document/Version/ACL、
  事务 Outbox、`SKIP LOCKED` 竞争领取和摄取 Worker 组件；
- Local ArtifactStore，以及 FastAPI Upload/Artifact/Health/Chat/SSE API；
- PostgreSQL EventLog 的 per-stream gap-free sequence、显式 envelope schema version、
  生产者时间戳回放和 stream-local durable `event_key` 幂等写入；
- BGE-M3 Dense Embedding、Qdrant Dense/Hybrid 检索和离线 RAG 评测；
- 固定 2-step Chat 的 ACL 双重检查、答案发布门、source revision 读取栅栏、已提交
  会话消息的多轮回放，以及 PostgreSQL `chat_turns` 幂等事实源；
- Chat API 强制 `Idempotency-Key`，同一会话的活跃 Turn 不交错；已提交请求重试不再
  重跑模型，“答案事件已写、Turn 尚未提交”的崩溃窗口可幂等恢复；
- 与固定检索共用 `RetrievalService` 的 `knowledge_search` Tool Adapter。

这些能力仍有明确边界：

- `IngestionWorker` 仍是可调用组件，没有常驻进程、heartbeat、retry/dead-letter 和
  多 Worker 外部副作用 fencing；上传后自动可检索的产品 E2E 尚未贯通。
- 旧 Qdrant Point 已被 revision 栅栏阻止读取，但 replace/delete 物理清理尚未完成。
- Chat 尚未实现 `running` Turn 的 lease/reaper，因此进程在模型执行中硬崩溃后需要
  运维恢复；历史 token window/compaction 和模型实际引用校验也尚未实现。
  `knowledge_search` 尚未装配为可用的 Agentic Retrieval Mode。
- EventLog 能拒绝未知 schema version，但尚未实现旧版本 upcaster、poison-row
  隔离/跳过策略。
- LlamaIndex/LangChain Adapter、LangGraph Task、Task Registry、Multi-Agent、
  CrewAI 对比、UI、生产身份认证和部署仍为 Planned。

> **安全警告：** 当前 Identity Adapter 只信任请求头，因此 `agent-api` 只能用于
> 受控的本机开发，不得暴露到局域网、容器端口映射或公网。监听地址已强制为
> loopback（默认 `127.0.0.1`，Settings 与装配层双重校验），但那是防止意外暴露的
> 机制，不是身份认证——真实身份提供方仍未实现。

完整增量、测试证据、已知缺陷和未实现边界见
[实施状态](docs/status.md)。

## 快速体验

```bash
uv run agent-cli demo
```

脚本化模型离线运行，不联网、不连数据库；同一条命令的输出逐字节可复现。
想看被策略拒绝时 handler 完全不会被调用：

```bash
uv run agent-cli demo --deny
```

## 本地配置检查

前置条件：Python 3.12 和 `uv`。

1. 安装锁定后的开发环境：
   `uv sync --frozen --group dev --no-editable`。
2. 将 `.env.example` 复制为 `.env`，替换仅用于本地开发的占位值。
3. 执行：

```bash
uv run agent-config-check --profile development
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

配置检查只验证结构和安全不变量，不会连接 PostgreSQL、Qdrant 或在线模型。
测试和静态检查在依赖同步完成后可以离线运行。

## 设计依据

- [文档索引](docs/README.md)
- [架构与技术选型基线 v1.3](docs/architecture-baseline.md)
- [代码实施计划 v1.0](docs/implementation-plan.md)
- [配置管理契约](docs/configuration.md)

clean-room 边界见 [NOTICE.md](NOTICE.md) 和
[docs/compliance.md](docs/compliance.md)。

当前实现证据记录在 [docs/status.md](docs/status.md)。
