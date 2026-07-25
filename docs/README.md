# Agent Workbench 文档索引

本文档目录区分“已经实现的工程事实”和“尚待实现的目标设计”。架构或计划中
出现某项能力，不代表仓库已经具备该能力；实际完成度以
[实施状态](./status.md)和可复现测试证据为准。

## 核心基线

| 文档 | 版本 | 用途 |
|---|---:|---|
| [架构与技术选型基线](./architecture-baseline.md) | v1.3 | 锁定产品边界、分层、组件职责、可靠性协议和技术选型 |
| [代码实施计划](./implementation-plan.md) | v1.0 | 将目标架构拆成工作包、PR、迁移、测试门禁和证据包 |
| [配置管理契约](./configuration.md) | schema 1.1 | 定义配置来源、密钥规则、快照语义和跨域校验 |

## 项目治理

- [实施状态](./status.md)：记录已经实现、测试或演示的能力；
- [Clean-room 与合规说明](./compliance.md)：说明来源边界和可公开表述；
- [仓库 NOTICE](../NOTICE.md)：声明本项目不包含来源不明的私有实现。

## 阅读顺序

1. 先读架构基线第 1–3 节，理解 Chat、Task 与自研 Runtime 的职责边界；
2. 再读代码实施计划第 3、6、10 节，了解依赖图、工作包和 PR 顺序；
3. 实现任何工作包前核对配置契约与对应验收测试；
4. 合并后只按实施状态和 evidence 证据更新 README 或简历表述。

## 当前事实

截至 2026-07-25，PR-001 Bootstrap、PR-002 Config CI、PR-003 Domain、
PR-004 Ports + Fakes、PR-005 CLI Skeleton 与 PR-006 Runtime Serial Loop 已实现
并完成本地验证；`agent-cli demo` 演示的是完整一轮
模型 → Tool → ToolResult → 模型。Tool Gateway（schema 校验与 Hook）、并行只读
调度、RAG、LangGraph Task、PostgreSQL 协调、Multi-Agent、API、UI 和部署仍是
计划能力。
