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
**PR-004 Ports + Fakes**、**PR-005 CLI Skeleton**、**PR-006 Runtime Serial
Loop**、**PR-007 Policy + Tool Gateway**、**PR-008 Runtime Budgets** 与
**PR-009 Parallel Reads** 与 **PR-010 Hook Bus** 的本地实现和验证。真实 Model
Adapter、RAG、Workflow、Multi-Agent、API 和 UI 仍处于 Planned 状态，不能描述为
已经实现。

PR-003 交付的是框架无关的领域契约：消息、工具、事件、上下文、运行预算、
策略决定与错误分类，全部只依赖标准库与 Pydantic。工具调用与结果的配对、
事件持久性、引用可溯源等不变量以构造期校验的形式固定下来。

PR-004 在其上定义了 Model、Tool、Agent、Event 与 Store 的 Protocol，并给出
一套零外部依赖的实现：脚本化 FakeModel、内存事件日志/会话库/产物库、两个
无副作用 Tool 和 deny-by-default 策略引擎。契约测试因此可以离线、确定性地
运行，不需要数据库、向量库或在线模型。

PR-005 把这些零件接成第一条可运行的纵向切片：输入 → 模型 → 统一事件 → 输出。
CLI 只消费事件与返回的 `AgentOutcome`；流式回答来自 transient delta，时间线
来自运行结束后对 durable log 的重放，两者的差异正是持久性规则本身。

PR-006 补上自研 `ClaudeLikeAgentRuntime`：串行
`模型 → Tool → ToolResult → 模型` 循环，基线 7.1 的状态机被写成可执行的转移表，
非法转移直接抛错。每个已暴露的 `tool_call_id` 恰有一个 `ToolResult`——未知工具、
被策略拒绝、handler 抛异常、超时、批次中途取消都会产出结果而不是留空；提交顺序
永远是模型的调用顺序。

PR-007 把这些检查收进唯一的 Tool Gateway：handler 只在**最终参数**同时通过
schema 校验与授权决定之后才会运行。策略若返回 `allow_with_modified_input`，
重写后的参数会被重新校验并重新提交决定——能在检查之后改参数，就等于同时绕过
两道检查。JSON Schema 只支持一个明确的子集，超出子集的 schema 在装配阶段就被
拒绝，而不是在调用时被静默跳过。

PR-008 把各层时限收敛成一个下界：单次模型调用的有效 deadline 是
`min(运行时 envelope, run 剩余时间)`（模型 profile 自己的超时由 Adapter 在更
内层施加），单个 Tool 的时限是 `min(工具声明的超时, run 剩余时间)`——一个被批准
一小时的工具，不该活得比批准它的 run 还久。取消在流式过程中于下一个事件边界生效，
并通过关闭生成器传导到 Adapter；模型完全静默时由 deadline 兜底。

PR-009 让同一批里连续的只读工具并发执行，写/外部/破坏性工具则各自独占一组——
"副作用要过屏障"因此不再只是文档里的一句话。分组是一个**纯函数**，不需要事件
循环就能验证；执行顺序可以变，提交给模型的顺序永远是它自己的调用顺序。

PR-010 加入 Hook：部署方可以在工具调用被判定之前检查、改写或拦截它。Hook 改写
过的参数会**重新走一遍 schema 校验与授权**——能在检查之后改参数就等于绕过检查；
Hook 抛错或超时一律视为拦截，因为"坏掉的安全规则"绝不能等于"放行"。

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
