# 实施状态

## 文档基线

状态：**已纳入 Git 版本管理**。

- [架构与技术选型基线 v1.3](./architecture-baseline.md)；
- [代码实施计划 v1.0](./implementation-plan.md)；
- [配置管理契约 schema 1.1](./configuration.md)。

这些文档描述目标架构和增量计划，不代表其中列出的产品能力已经实现。

## PR-001 Bootstrap

状态：**已实现并通过本地测试，已纳入 Git 版本管理**。

已交付：

- Python 3.12 src-layout 包；
- `pyproject.toml` 和 `uv.lock`；
- 已迁移并适配正式目录的配置基线；
- 脱敏的 `agent-config-check` 入口；
- clean-room 与合规文档；
- 使用 `main` 分支的独立 Git 仓库。

2026-07-23 验证证据：

```text
uv 0.11.31
Python 3.12.13
uv lock --check --offline: passed
pytest: 43 passed
agent-config-check test profile: status=ok
non-editable install with packaged default config: passed
```

仍不属于 PR-001 的内容：

- CI 和配置 ownership 架构测试；
- API、进程 Container 和 readiness；
- Runtime、Domain 与 Ports；
- Persistence、RAG、Workflow 与 Multi-Agent；
- Docker Compose 和外部服务连通性。

## PR-002 Config CI

状态：**已实现并通过本地同构检查**。

已交付：

- 覆盖 230 个 Settings 叶子字段的 `config/ownership.yaml`；
- ownership 唯一性、生命周期和 Task snapshot 正向 allowlist 架构测试；
- 核心层框架依赖、反向依赖、原始配置读取和 `os.environ` 边界测试；
- development、test、production 三个离线配置 profile；
- Ruff、严格 Pyright（产品源码）、pytest、许可证与 Git 历史密钥扫描 CI；
- GitHub Actions 与下载型工具都固定到 release SHA 或 SHA-256；
- 删除没有消费者的 `admin_token`、`webhook_token` 配置入口。

2026-07-23 本地验证证据：

```text
uv lock --check --offline: passed
ruff format --check: passed
ruff check: passed
pyright: 0 errors, 0 warnings
pytest: 60 passed
development/test/production config profile contract tests: passed
dependency license allowlist: passed
Gitleaks 8.30.1 working-tree/history scan: passed
```

该 PR 只证明配置和 CI 合同，不代表 API、Worker、模型或外部 Adapter 已经
启动。下一增量仍是 PR-003 Domain。
