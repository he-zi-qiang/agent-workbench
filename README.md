# Agent Workbench

中文 | [English](README.en.md)

Agent Workbench 是一个面向校招与作品集展示的 clean-room 通用 Agent
平台项目，目标是提供两种产品模式：

- **Chat Mode**：多轮对话、知识库问答和带权限校验的 RAG；
- **Task Mode**：可恢复的 LangGraph 工作流和可控 Multi-Agent 协作。

项目的自研 Agent Runtime 保持框架无关。LlamaIndex、LangGraph、LangChain
以及后续对比框架都通过明确的 Port/Adapter 接入，不接管核心 Tool Loop。

## 当前状态

截至 2026-07-25，`main@f071323` 已合并 **PR-001～PR-015**，并完成
ADR-012 身份边界决策。当前已经实现并测试：

- 框架无关的 Domain、Ports、Fake Adapter 与可复现 CLI 演示；
- 自研 `ClaudeLikeAgentRuntime`：Tool Loop、schema/Policy Gateway、预算与
  deadline、取消、并行只读调度、exclusive 屏障和 Hook Bus；
- DeepSeek OpenAI-compatible 流式 Adapter 的离线协议契约；
- PostgreSQL ConversationStore、Alembic 迁移、Document/Version/ACL、
  事务 Outbox 与 `SKIP LOCKED` 竞争领取；
- Local ArtifactStore，以及 FastAPI Upload/Artifact/Health API。

这些能力仍有明确边界：

- DeepSeek Adapter 尚未接入 Bootstrap/API/CLI 的进程装配，也没有真实在线模型
  E2E；当前 `agent-cli demo` 仍使用脚本化 FakeModel。
- 已实现的是上传相关 API，不是完整 Chat/Task API。Chat RAG、LangGraph Task、
  Multi-Agent、SSE、Approval、UI、生产身份认证和部署仍为 Planned。
- PostgreSQL 已用于会话、文档和 Outbox；Task Registry、lease、fencing、
  checkpoint 与 `LISTEN/NOTIFY` 协调尚未实现。

> **安全警告：** 当前 Identity Adapter 只信任请求头，且默认
> `api.host = "0.0.0.0"`。在 loopback 强制校验落地前，`agent-api` 只能用于
> 受控的本机开发，不得暴露到局域网、容器端口映射或公网。

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
