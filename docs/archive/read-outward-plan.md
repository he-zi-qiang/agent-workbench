# WP14-02 只读外部世界：实施方案

决策依据：[ADR-027](../adr/0027-read-outward-write-inward.md)。本文只讲怎么落地和怎么
证明它是对的。

沿用 WP14-01 的规矩：**一个 PR 只做一件事**；每个 PR 的测试先证明是**红**的再变绿；
**没有对照组的测试不算数**——只断言"这个被拒绝"的测试分不出一个正常工作的校验器和一个
把什么都拒绝的校验器。

## 0. 先读这些，别边写边发现

| 事实 | 位置 |
|---|---|
| `external_search` 已经自己 GET 页面并抽正文 | `adapters/research/deepseek_web_search.py` |
| 正文抽取是现成的，复用它 | `adapters/research/page_text.py` |
| SSRF 只查字面主机名，注释自陈缺口 | 同上，`_LOCAL_HOSTS` |
| MCP server 的形状范例（无路径、无租户、纯内容） | `apps/word_mcp/` |
| 动态工具源现在只给 writer | `workflows/agent_profiles.py:179` |
| `retryable_effects=false` 的 server 被整个跳过 | `apps/task_worker/composition.py:510` |
| MCP 结果 → artifact 的映射与 media type 回退 | `adapters/mcp/result_mapping.py` |

三条硬约束不变：`SUPPORTED_KEYWORDS` 一个字不加；新配置字段必须登记进
`config/ownership.yaml`（会递归展开嵌套模型）；抬 `config_schema_version` 时
`test_the_configuration_schema_version_is_pinned` 失败是机制不是障碍。

## 1. PR 序列

### PR-1：SSRF 从字面地址升级为解析后校验

**必须最先做，且独立成 PR。** 后面两个 PR 让模型能自己命名 URL，那一刻起这个缺口从
"要先污染搜索引擎索引"变成"往网页里写一句话"。

新建 `adapters/research/address_guard.py`：

```python
async def assert_public_destination(url: str) -> None: ...
```

- 解析主机名，拒绝解析结果落在回环、链路本地（含 `169.254.169.254`）、私有网段、
  唯一本地地址、以及 `0.0.0.0/8`；
- IPv4 与 IPv6 都要判，含 IPv4-mapped IPv6；
- **在连接前**完成；
- 既有 `external_search` 的抓取路径改为共用这一份。

**测试的牙**：
- 一组解析到私有地址的主机名被拒（用可注入的解析器，不依赖真实 DNS）；
- **对照组**：解析到公网地址的同形状主机名通过。少了这条，一个"永远拒绝"的实现也全绿；
- `169.254.169.254` 和它的 IPv6 形式各一条；
- 重定向到私有地址也要拒——断言重定向后的地址同样过闸，不只是首个 URL。

### PR-2：只读取用 MCP server

新建 `src/agent_workbench/apps/web_mcp/`，形状照抄 `apps/word_mcp/`：`contract.py`
（有界输入 schema）、`server.py`（MCP v2 surface）、`main.py`。

两个工具：

```
fetch_page(url, max_chars?)       -> 抽好的正文，不返回原始 HTML
download_document(url)            -> 字节，交给 MCP 结果映射落 artifact
```

- 两个都是 GET，**不带 `operation_key`**，server 声明 `retryable_effects = true`；
- 输入 schema 只用那 17 个关键字（否则自己的工具会被自己的闸门筛掉，很讽刺但会发生）；
- `fetch_page` 复用 `page_text`；
- `download_document` 有字节上限，超了返回结构化错误而不是截断——截断的文档是坏文档；
- 远端异常文本不透传（照 `word_mcp/server.py` 的既有处理）；
- 每次取用前调 PR-1 的闸门。

**测试的牙**：
- 私有地址被拒（对照组：公网地址通过）；
- 非 HTML 内容不走 `page_text`，而是原样成为字节（对照组：HTML 走正文抽取）；
- 超限返回错误（对照组：限内正常返回）；
- **协议层**：用官方 SDK 的内存 server 跑一次 `tools/list` + `tools/call`，断言两个工具
  都在目录里且 schema 过得了 `assert_schema_supported`。

### PR-3：`researcher_external` 拿到动态工具源

改 `workflows/agent_profiles.py`：给 `researcher_external` 加
`dynamic_tool_sources=frozenset({"mcp"})`，并让"哪个 server 的工具进哪个 profile"由
配置声明而不是硬编码——否则每加一个渲染器都要改 profile 代码。

`[[mcp.servers]]` 增加一个字段（建议 `audience`，取值 `research` / `synthesis`），
登记进 `config/ownership.yaml`，抬 `config_schema_version`。

**测试的牙**：
- `audience="research"` 的 server，其工具对 researcher_external 可见、对 writer 不可见；
- `audience="synthesis"` 反过来（这条保证 word 那套没被这次改动挪走）；
- **对照组（防回归主力）**：没有配置 MCP 时，六个 profile 的可见工具集合与今天逐字节相同；
- framer / planner / critic 在任何配置下都看不到工具。

### PR-4：本地 profile 与真实验收

照 `docs/word-mcp-local.md` 的形状写 `docs/web-mcp-local.md` 与
`config/config.web-local.toml`，加 `scripts/dev.sh web-server` / `web-check`。

真实验收不能只看任务成功，至少核对：

1. `researcher_external` 的 `RunStarted.tool_names` 含 `mcp_web_fetch_page`；
2. 同一 run 出现 `ToolProposed → PermissionResolved → ToolStarted → ToolCompleted`；
3. 下载的文件在 artifact store 里，`media_type` 正确，提交者能下载；
4. 去掉 `mcp:web` scope 后，同一工具在 Gateway 被拒。

## 2. 之后可以复制的形状

ADR-027 §3.4 说的"操控基本的软件"就是把 `apps/word_mcp` 再抄几遍：

| server | 工具 | 产出 |
|---|---|---|
| `xlsx_mcp` | `render_spreadsheet` | .xlsx |
| `pptx_mcp` | `render_deck` | .pptx |
| `pdf_mcp` | `render_pdf` | .pdf |

每一个都是纯函数、无路径、无租户字段、`retryable_effects=true`。它们不需要新的 ADR——
形状已经由 ADR-026 与本 ADR 确立；需要新 ADR 的是任何**会改变对面状态**的东西。

## 3. 明确不做

- 填表、点击、下单、任何 POST；
- 驱动桌面软件的界面；
- JS 渲染的页面与截图（需要浏览器内核，见 ADR-027 §3.5）。**SPA 页面在本版取不到正文，
  这是已知边界不是 bug**；
- 放宽 `retryable_effects`。

## 4. 验收命令

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/e2e
```

```bash
.venv/bin/python -m ruff check src/ tests/ scripts/ && .venv/bin/python -m ruff format --check src/ tests/ scripts/
```

```bash
.venv/bin/python -m pyright
```

带真实服务的全量（容器起在 5433 / 6333，跳过项应从 597 降到 11）：

```bash
AGENT_WORKBENCH_TEST_DSN="postgresql+asyncpg://agent:ci-only@127.0.0.1:5433/agent_workbench_test" AGENT_WORKBENCH_TEST_QDRANT_URL="http://127.0.0.1:6333" .venv/bin/python -m pytest tests/ -q --ignore=tests/e2e
```

`tests/vector/test_tied_score_order.py` 的 tie-break 用例在全量跑里偶发失败（单独跑稳定
通过），与本 WP 无关，不要顺手改它。

跑真实 Worker 需要 `embedding` extra；`uv sync` 不带 `--extra embedding` 会把它剪掉，
症状是 Worker 拒绝启动并要求安装该 extra。
