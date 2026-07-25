# Agent Workbench

中文 | [English](README.en.md)

Agent Workbench 是一个面向校招与作品集展示的 clean-room 通用 Agent
平台项目，提供两种产品模式：

- **Chat Mode**：多轮对话、知识库问答和带权限校验的 RAG；
- **Task Mode**：可恢复的 LangGraph 工作流和可控 Multi-Agent 协作。

项目的自研 Agent Runtime 保持框架无关。LlamaIndex、LangGraph、LangChain
以及后续对比框架都通过明确的 Port/Adapter 接入，不接管核心 Tool Loop。

## 当前状态

目前已完成 **PR-001 Bootstrap**、**PR-002 Config CI**、**PR-003 Domain**、
**PR-004 Ports + Fakes** 与 **PR-005 CLI Skeleton** 的本地实现和验证。
Runtime 循环、RAG、Workflow、Multi-Agent、API 和 UI 仍处于 Planned 状态，
不能描述为已经实现。

PR-003 交付的是框架无关的领域契约：消息、工具、事件、上下文、运行预算、
策略决定与错误分类，全部只依赖标准库与 Pydantic。工具调用与结果的配对、
事件持久性、引用可溯源等不变量以构造期校验的形式固定下来。

PR-004 在其上定义了 Model、Tool、Agent、Event 与 Store 的 Protocol，并给出
一套零外部依赖的实现：脚本化 FakeModel、内存事件日志/会话库/产物库、两个
无副作用 Tool 和 deny-by-default 策略引擎。契约测试因此可以离线、确定性地
运行，不需要数据库、向量库或在线模型。

PR-005 把这些零件接成第一条可运行的纵向切片：输入 → 脚本化模型 → 统一事件
→ 输出。CLI 只消费事件与返回的 `AgentOutcome`；流式回答来自 transient
delta，时间线来自运行结束后对 durable log 的重放，两者的差异正是持久性规则
本身。当前的单轮 executor 不拥有 tool loop——模型提出工具调用时它会先记录
再让 run 失败，而不是默默丢弃。

## 快速体验

```bash
uv run agent-cli demo
```

脚本化模型离线运行，不联网、不连数据库；同一条命令的输出逐字节可复现。
想看"没有 tool loop 时提出工具调用会怎样"：

```bash
uv run agent-cli demo --propose-tool read_document
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
