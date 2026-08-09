# WP14-01 MCP Adapter 实施方案

ADR：[ADR-025](./adr/0025-mcp-adapter.md)。本文只讲怎么落地和怎么证明它是对的，
决策理由不重复——但每条实现约束都注明了它出自 ADR 的哪一节，改之前先回去读那一节。

计划按项目既有规矩拆：**一个 PR 只做一件事**，每个 PR 自带"有牙"的测试——即先证明
测试在改动前是**红**的，再让它变绿。**没有对照组的测试不算数**：只断言"这个被拒绝"
的测试分不出一个正常工作的校验器和一个把什么都拒绝的校验器。

## 0. 进度

| PR | 内容 | 状态 |
|---|---|---|
| PR-1 | `[mcp]` 配置段与 schema 升版 | **已完成** |
| PR-2 | 名字重整（纯函数） | 待做 |
| PR-3 | schema 闸门 | 待做 |
| PR-4 | `tools/list` 客户端与 `ToolBinding` 装配 | 待做 |
| PR-5 | 授权信封 | 待做 |
| PR-6 | 结果映射与 artifact 落地 | 待做 |
| PR-7 | 端到端与 benchmark | 待做 |

## 1. 代码库事实速查

下面这些是实现时一定会撞上的既有约束。先读，不要边写边发现。

**工具契约**（`src/agent_workbench/domain/tools.py`）

- `ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]`；
- `ToolSpec.validate_risk_consistency`：`risk == "read"` ⇒ 必须 `idempotency == "safe"`；
  `write`/`external`/`destructive` ⇒ 必须 `concurrency == "exclusive"` **且**至少一个
  `permission_scopes`。所以 MCP 工具（一律 `external`）三个字段是被这条锁死的；
- `ToolBinding.__post_init__`（`ports/tools.py`）：`operation_key` 与
  `idempotency == "safe"` 互斥，同时出现直接 `ValueError`；
- `OperationKeyFor` 的注释明确反对从 `tool_call_id` 推 key——重试会 mint 新 id，
  于是每次重试都像新工作。PR-6 会用到这条。

**schema 校验**（`src/agent_workbench/runtime/schema_validation.py`）

- `SUPPORTED_KEYWORDS` 只有 17 个关键字，`assert_schema_supported` 对不认识的关键字
  抛 `UnsupportedToolSchema`；
- 它在 `ToolGateway.__init__`（`runtime/tool_gateway.py:149`）里对每个注册工具调用一次。
  **这意味着一个 schema 不合规的 binding 进了注册表，炸的是 gateway 装配，也就是进程启动。**
  PR-3 的闸门必须挡在 binding 造出来之前。

**注册表装配点**

- Task Worker：`src/agent_workbench/apps/task_worker/composition.py:385`
  ```python
  tool_registry = StaticToolRegistry((external_tool.binding(), export_tool.binding()))
  ```
  MCP 的 binding 加在这个元组里。同一行下面就是 `ToolGateway(...)`；
- API 侧另有 `apps/api/dependencies.py`，本 WP **不动它**（MCP 只进 Task）；
- `StaticToolRegistry` 对重名 binding 抛 `ValueError: duplicate tool registration`。

**授权信封**（`src/agent_workbench/bootstrap/projections.py:76`）

- `task_authorization_envelope(*, external_search: bool) -> AuthorizationEnvelope`；
- 已有两个常量：`TASK_V1_AUTHORIZATION_ENVELOPE` 和
  `TASK_V1_AUTHORIZATION_ENVELOPE_WITH_SEARCH`；
- 注释写明 `allowed_tools` 与 `max_tool_risk` **必须一起抬**，只抬一个会得到一个仍然
  拒绝该工具的信封（`risk_within` 把 external 排在 write 之上）。MCP 工具是 `external`，
  所以走 `max_tool_risk="external"` 那一档。

**结果与产物**（`domain/tools.py`、`domain/artifacts.py`）

- `ToolResult` 已有 `artifact: ArtifactRef | None` 字段，MCP 产出的文件走这条通道；
- `ArtifactRef.MediaType` 正则是 `^[a-z]+/[A-Za-z0-9][A-Za-z0-9.+_-]*$`，长度 3–128。
  OOXML 的 `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
  能过（71 字符）。

## 2. 三条硬约束

踩上去会让架构测试直接 fail，或者违反 ADR：

1. 新增配置字段必须**同时**登记进 `config/ownership.yaml`（唯一 owner + lifecycle），
   否则 `tests/architecture/test_config_ownership.py` 报 ownership drift。注意它会**递归**
   进嵌套模型：`tuple[MCPServerSettings, ...]` 展开成 `mcp.servers.alias` 等五个叶子字段，
   要逐个登记，`mcp.servers` 本身不是叶子；
2. `SUPPORTED_KEYWORDS`（`runtime/schema_validation.py:34`）**在整个 WP 里一个字都不加**。
   有 PR 动了它，说明走错路了，回去看 ADR-025 决策三；
3. 抬 `config_schema_version` 时，`tests/config/test_settings.py::test_the_configuration_schema_version_is_pinned`
   会失败。**那是机制不是障碍**——它的 docstring 自己写着 "this test failing *is* the
   mechanism"。更新断言并在它的 docstring 版本串里补一行理由，是决策的最后一步。

## 3. PR 序列

### PR-1：`[mcp]` 配置段与 schema 升版 —— 已完成

实际落地的内容（与最初计划有三处出入，以此处为准）：

- `MCPServerSettings`（`bootstrap/settings.py`）：
  - `alias`：`^[a-z][a-z0-9_]*$`，**≤24 字符**。它会变成 `mcp_<alias>_<tool>` 的中段，
    而 `ToolName` 上限 64，远端那半需要留空间；
  - `transport`：**单值 `Literal["http"]`**。stdio 意味着派生本地子进程，是另一套威胁
    模型，ADR-025 决策一没有决定它——要加得改代码，不是改配置文件；
  - `endpoint`：复用 `_validate_service_endpoint`，拒绝 userinfo 凭据、query、fragment；
  - `retryable_effects: bool`，**无默认值**（ADR-025 决策五）；
  - `timeout_seconds`：默认 30，1–600。
- `MCPSettings.servers: tuple[MCPServerSettings, ...] = ()`，带 `refuse_duplicate_aliases`；
- `Settings.validate_architecture_and_environment` 增加单向交叉校验：
  `mcp.servers` 非空而 `optional_labs.mcp_adapter` 为 false → 启动失败。
  **反向不检查**：开关开着没配 server 是合法状态（打开了 lab 还没指向任何地方）；
- `config_schema_version` `1.6` → `1.7`（`settings.py` 的 `Literal` 与
  `config/config.default.toml` 两处），`docs/configuration.md` 的版本串同步；
- `config/ownership.yaml` 五个叶子字段登记给 owner **`adapters.mcp.registry_source`**、
  lifecycle `startup`（不是最初写的 `bootstrap.adapter_factory`——真正读它的是 PR-4 那个模块）。

测试见 `tests/config/test_mcp_settings.py`，8 个，每个拒绝都配了对照组。

### PR-2：名字重整，纯函数先行

新建 `src/agent_workbench/adapters/mcp/naming.py`。**不碰网络、不碰注册表、不 import
任何 adapter**。

```python
@dataclass(frozen=True, slots=True)
class SkipReason:
    """为什么这个远端工具没能进来，一句能给运维看的话。"""
    remote_name: str
    reason: str

def tool_name_for(alias: str, remote_name: str) -> ToolName | SkipReason: ...
```

- 产出 `mcp_<alias>_<remote>`，重整后必须匹配 `^[a-z][a-z0-9_]{0,63}$`；
- 重整规则要**确定且可逆推**：大写转小写、`-`/`.`/空格 转 `_`、其余非法字符整体判失败。
  不要做"删掉非法字符"这种有损映射——两个远端名字塌缩成同一个本地名字，会在
  `StaticToolRegistry` 那里变成 `duplicate tool registration`，而错误信息指不回真正的原因；
- 超长、无法映射 → 返回 `SkipReason`，**不抛异常**。

**测试的牙**（`tests/adapters/test_mcp_naming.py`）：
- 一组真实形状的名字各自的归宿：`camelCase`、`kebab-case`、`with.dots`、`Ünicode`、
  64 字符以上、空字符串；
- 对照组：两个 server 各有一个 `search` → 必须得到两个**不同**的 `ToolName`；
- 确定性：同一对输入调用两次，结果逐字节相同（这个名字会进事件流和账本 key）。

### PR-3：schema 闸门

新建 `src/agent_workbench/adapters/mcp/schema_gate.py`。**复用 `assert_schema_supported`，
不重写一份**——重写等于让 MCP 路径和自研路径对"什么算合规"产生两种意见。

```python
def admit(remote_name: str, schema: JsonObject) -> JsonObject | SkipReason: ...
```

捕获 `UnsupportedToolSchema`，转成 `SkipReason`。**这是整个 WP 唯一允许捕获这个异常的
地方**，且只在 MCP 路径上。

同时要检查 `ToolSpec.require_object_input` 的前置条件：`schema.get("type") != "object"`
的远端工具直接 `SkipReason`，否则会在造 `ToolSpec` 时抛 `ValidationError`。

**测试的牙**（`tests/adapters/test_mcp_schema_gate.py`）：
- 含 `oneOf` 的 schema → 被跳过，且**进程不受影响**；
- 对照组：同一批里合规的那个 → 正常返回 schema 本身。要断言的是"跳掉坏的、留下好的"
  这个**差集**，不是单独一句"坏的被跳过"；
- 非 object 顶层类型 → 跳过（对照组：object 顶层 → 通过）；
- **反向断言**：自研工具路径上塞一个 `oneOf`，`ToolGateway` 装配**必须仍然抛**。
  这条防的是有人把 MCP 的宽容顺手改到了公共路径上。

### PR-4：`tools/list` 客户端与 `ToolBinding` 装配

新建 `src/agent_workbench/adapters/mcp/client.py` 与
`src/agent_workbench/adapters/mcp/registry_source.py`。

```python
async def discover(server: MCPServerSettings) -> tuple[ToolBinding, ...]: ...
```

- 启动时每个 server 调**一次** `tools/list`，超时用 `server.timeout_seconds`；
- 连不上 / 超时 / 协议错误 → 记结构化日志、返回空元组，**进程照常起**。
  写法对齐 `bootstrap/embedding_factory.py`：进程报告它没能建起什么，然后继续提供
  它能提供的东西；
- 每个远端工具依次过 PR-2 命名、PR-3 schema，然后按 ADR-025 决策四填死：
  `risk="external"`、`concurrency="exclusive"`、`permission_scopes=("mcp:<alias>",)`、
  `idempotency="unsafe"`、`timeout_seconds=server.timeout_seconds`；
- `retryable_effects=false` 的 server：其 binding **不带 `operation_key`**，且不进 Task
  注册表。ADR-025 决策五——没有账本的远端写工具，重试会产生第二次真实副作用；
- 装配点：`apps/task_worker/composition.py:385` 那个元组。

**测试的牙**（`tests/adapters/test_mcp_registry_source.py`）：
- 内存 fake server（`tests/support/` 已有 fake 的写法可循，见 `tests/support/pdf.py` 的
  组织方式）返回 3 个工具，其中 1 个名字非法、1 个 schema 非法 → 注册表里**正好剩 1 个**，
  且日志里**正好 2 条**跳过记录，各自带得出原因；
- 对照组一：3 个全合规 → 3 个都在；
- 对照组二：server 完全不可达 → 注册表 0 个，且**进程仍然启动成功**（这条是这个 PR 的
  核心承诺，不能只靠"没抛异常"来暗示）；
- `retryable_effects=false` 的 server → binding 的 `operation_key is None`。

### PR-5：授权信封

改 `bootstrap/projections.py`：

```python
def task_authorization_envelope(
    *, external_search: bool, mcp_tools: tuple[ToolName, ...] = ()
) -> AuthorizationEnvelope: ...
```

`allowed_tools` **逐个列出**解析后的 MCP 工具名，不用通配。ADR-025 决策六：信封随 Task
存盘、每次 resume 重放，通配符会让一次配置变更追溯性地改写历史 Task 的权限。

`mcp_tools` 非空时 `max_tool_risk` 必须是 `"external"`（见 §1 那条"两个一起抬"的注释）。

**测试的牙**（补进 `tests/config/test_settings.py` 或新建 `tests/bootstrap/test_mcp_envelope.py`）：
- 配了 2 个 MCP 工具 → 信封里正好是 `export_artifact` + 那 2 个；
- **对照组（防回归主力）**：`mcp_tools=()` → 信封与今天**逐字节相同**。
  它保证一个没开这功能的部署权限一点没变，这是整个 WP 最重要的一条断言；
- 信封里有名字但注册表里没有（模拟"Task 提交后运维改了 MCP 配置"）→ 得到一个
  `status="error"` 的 `ToolResult`，**不是异常**。`ports/tools.py` 已写明 "an unknown
  tool is not an exception"，这条既有语义正好兜住，不需要新机制。

### PR-6：结果映射与 artifact 落地

MCP 的 content block → `ToolResult`。文本进 `content`；resource 超过
`runtime.tool_result_artifact_threshold_bytes` 的写进 artifact store，返回 `ArtifactRef`。

- 落盘这一步带 `operation_key`，key 从 `task_id` + `argument_digest(arguments)` 推。
  **不要用 `tool_call_id`**——见 §1；
- `media_type` 用远端给的，但必须过 `ArtifactRef.MediaType` 正则；过不了就退回
  `application/octet-stream`，**不猜**；
- `kind` 用 `"tool_result"`。

**测试的牙**（`tests/adapters/test_mcp_result_mapping.py`）：
- 同一个调用重试两次 → artifact store 里**只有一个**对象，且账本报告"这个操作已经
  成功过"；
- 对照组：换一个参数再调 → 得到**第二个**对象。少了这条，上一条也可能是"什么都没写进去"；
- 畸形 media type → 落成 `application/octet-stream`（对照组：合法的 OOXML media type
  原样保留）。

### PR-7：端到端与 benchmark

`docs/implementation-plan.md` WP14 的规矩是"每项都必须有独立 Adapter、ADR 和 benchmark"。

- `tests/e2e/test_mcp_task_e2e.py`：起 fake MCP server，跑一个真 Task，断言事件流里
  `ToolProposed → PermissionResolved → ToolStarted → ToolSucceeded` 四条齐全，
  且 `tool_name` 是重整后的名字；
- benchmark：记录"多接一个 MCP server 让启动慢了多少"。这是这个功能唯一会被日常
  感知的开销，值得有个数落在 `docs/status.md`。

## 4. 验收命令

每个 PR 合并前，这四条都要过：

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/e2e
```

```bash
.venv/bin/python -m ruff check src/ tests/ && .venv/bin/python -m ruff format --check src/ tests/
```

```bash
.venv/bin/python -m pyright
```

带真实服务的全量（Postgres 与 Qdrant 容器起在 5433 / 6333，跳过项应从 597 降到 11）：

```bash
AGENT_WORKBENCH_TEST_DSN="postgresql+asyncpg://agent:ci-only@127.0.0.1:5433/agent_workbench_test" AGENT_WORKBENCH_TEST_QDRANT_URL="http://127.0.0.1:6333" .venv/bin/python -m pytest tests/ -q --ignore=tests/e2e
```

已知偶发失败：`tests/vector/test_tied_score_order.py::test_the_hybrid_and_dense_paths_agree_on_the_tie_break`
在全量跑里会间歇性挂（单独跑稳定通过），与本 WP 无关，不要在本 WP 的 PR 里顺手改它。

## 5. 不在本 WP 范围内

写清楚，免得下次接手时以为漏了：

- **让最终报告变成 .docx**。`adapters/tools/export_artifact.py` 的 `EXPORT_MEDIA_TYPE`
  与 `EXPORT_FILENAME` 是写死的 `text/markdown` / `report.md`，改它要单独的 ADR
  （见 ADR-025 后果最后一条）。**本 WP 只让 MCP 工具产出的中间文件能落进 artifact
  store，不改最终报告的格式。** 接了一个 docx MCP server 之后，任务中途可以生成 .docx，
  但那份"任务报告"仍然是 markdown；
- MCP OAuth、热更新、动态工具审批——`docs/architecture-baseline.md` 第 94 行明确划在
  v1 之外；
- `stdio` transport——见 PR-1，需要单独决定；
- 放宽 `SUPPORTED_KEYWORDS`。要提高工具覆盖率就去**实现某个关键字的语义**，那是一次
  独立的、有自己测试的改动，不是在 MCP 这一侧开旁路。

## 6. 建议的实现顺序

PR-2 和 PR-3 是纯本地逻辑，不需要任何外部进程，可以一口气做完并跑满测试。
PR-4 起才需要 fake server。真实 MCP server 的接入放在最后手动验一次即可：本机起服务
的坑（端口、容器、模型加载耗时）与被测逻辑无关，见 [running-locally.md](./running-locally.md)，
不要让它挡住前面几个 PR 的进度。
