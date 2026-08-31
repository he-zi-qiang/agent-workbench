# ADR-098：只有一个进程读的上限，不是部署级上限

- 决策点：`[runtime]` 的 `model_timeout_seconds` / `tool_timeout_seconds` /
  `max_parallel_read_tools` 与 `[policy]` 的 `max_tool_argument_bytes` 只被 Task Worker
  读到，API 进程五处运行时、五处网关一处都没接；是把它们接进 API 进程，
  还是把它们改名为「Worker 的上限」并承认 Code 会话没有部署级天花板
- 状态：**接受**，接进 API 进程，四个数从此对两个进程是同一个意思
- 日期：2026-08-31
- 影响：`bootstrap/projections.py` 的 `ApiRuntimeConfig` 新增四个带默认值的字段；
  `apps/api/dependencies.py` 新增模块级 `_api_gateway()`，五处运行时构造与五处网关构造
  全部经它装配。**配置 schema 一个字都没动**（见 §3.1），事件形状、
  `policy_fingerprint`、Worker 侧的任何一行**均不变**
- 依赖：[ADR-019](./0019-run-step-transparency.md)（`record_step_inputs`
  为什么是投影而不是运行时读设置）、[ADR-042](./0042-blocking-belongs-to-the-adapter.md)、
  [ADR-057](./0057-a-pure-function-is-not-a-shell.md)（Code 会话持有 `sandbox_run`）、
  [ADR-077](./0077-a-command-on-this-machine-is-shown-before-it-is-run.md)（Code 会话持有 `project_run`）、
  [ADR-097](./0097-a-funnel-nobody-reads-is-not-a-funnel.md)（同一种形状的上一条）

---

## 1. 背景：一个为了修事故而调大的数，从来没生效过

`config/config.code-local.toml` 把 `[runtime] model_timeout_seconds` 从 120 提到 300，
并在注释里写下了理由——一次真实的超时事故，以及「这个数和
`[model.main] timeout_seconds` 成对，短的那个先响，只提一个等于没提」。
`docs/status.md` 用一整节论证了这次「成对」改动。

**而 code-local 只被 `code-api` 加载，Code 会话跑在 `apps/api` 进程里。**
该进程的 `ApiRuntimeConfig` 从来只投影三个 runtime 字段
（`record_step_inputs`、`context_soft_limit_ratio`、`context_compaction_enabled`），
五处 `ClaudeLikeAgentRuntime` 构造一处都没传 `model_timeout_seconds`，
于是信封恒为 `DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0`。

也就是说：**那个 300 一次也没生效过，适配器那层的 240 永远轮不到先响，
事故原话会一字不差地重现。**

这条能长期隐身，有一个具体的原因：`status.md` 那节的表把「谁在用」填成了
`agent_runtime.py` 里读 `self._model_timeout_seconds` 的那一行——**那是读取点，不是注入点**。
一个字段有读取点而没有注入点，看起来和接好了完全一样。

同形的还有三个：

| 字段 | 只被谁读 | API 侧的实际行为 |
|---|---|---|
| `runtime.tool_timeout_seconds` | `task_worker/composition.py` 一处 | 五处网关都走 `ToolExecutor()` 默认，`deployment_ceiling_seconds=None` |
| `runtime.max_parallel_read_tools` | 同上 | 恒为 `DEFAULT_MAX_PARALLEL_READS` |
| `policy.max_tool_argument_bytes` | **谁都没有** | 恒为 `DEFAULT_MAX_ARGUMENT_BYTES = 65536`；改这个数唯一的实际副作用是 `policy_fingerprint` 变了 |

第二行最要紧：**Code 会话持有 `project_run` 与 `sandbox_run` 这两个 destructive 工具，
而它们恰恰跑在这个没有部署级天花板的进程里。** 一个部署把
`tool_timeout_seconds` 设成 60，本意是「这台机器上任何一次工具调用不许超过一分钟」，
拿到的是 Task Worker 遵守、Code 会话不遵守。

第四行说明这不是「整段没接」而是逐个漏掉的：兄弟字段 `policy.max_tool_result_bytes`
从一开始就投影了。

## 2. 真正的问题不是「API 少了几个旋钮」

配置说明与 `config.default.toml` 的 `[runtime]` 段读起来都像是**对整个部署生效**。
没有任何一行说「以下四个数只对 Task Worker 生效」。所以这不是能力缺失，是**口径不实**：
一个 operator 按文档调了数、按文档读了配置校验的绿灯，得到的是一半。

两条出路因此不对称：

- **改文档**（承认这四个数只属于 Worker）：诚实，但要同时承认 Code 会话
  ——本仓库唯一会在 API 进程里跑 destructive 工具的产品面——**没有部署级天花板**，
  而这不是一个可以写进文档就了事的状态。
- **接线**：让这四个数对两个进程是同一个意思。

**选接线，理由和 ADR-097 §2 一样：那句话本来就该是真的。**

## 3. 决定

### 3.1 四个字段进 `ApiRuntimeConfig`，配置 schema 不动

四个都带默认值，且默认值抄 `config.default.toml`（120.0 / 4 / `None` / 65536）。
这样测试里几十处手搭的 `ApiRuntimeConfig` 一行都不用改，而生产的唯一来源仍然是
`project_api()`。**没有新增任何配置项**——四个 TOML 键、它们的校验、
它们在 `ownership.yaml` 里的归属全部原样，本 ADR 只是让它们有第二个读者。

`tool_timeout_seconds` 保持 `float | None`，`None` 继续表示「只按每个工具自己声明的」。
这不是骑墙：它把「配置没说」和「配置说了一个数」分开，而前者正是出厂行为。

### 3.2 一个 `_api_gateway()`，而不是五处各传四个关键字

这是本 ADR 与「把四个参数补到五个调用点上」的差别，也是它唯一的结构性内容。

被修的缺陷从来不是「有人填错了一个数」，而是**五个构造点，以及没有任何东西会注意到
第六个构造点带着默认值出生**。五处各自复制四个关键字，是把同一个缺陷的复发条件
原封不动留在原地。

所以这个进程从此只有一个地方构造 `ToolGateway`。守门测试
（`tests/apps/test_api_runtime_ceilings.py`）按 AST 检查两件事：

- 文件里每一个 `ToolGateway(` 都在 `_api_gateway` 函数体内；
- 每一个 `ClaudeLikeAgentRuntime(` 都显式传了 `model_timeout_seconds` 与
  `max_parallel_read_tools`。

第二条没有做成同样的工厂，是因为运行时构造的其余参数（`policy_identity`、
模型标签、价格、上下文窗口）在五处**确实不同**，把它们塞进一个工厂只会得到一个
参数比构造点还多的函数。守门测试守的是会漂的那两个。

### 3.3 `_api_gateway` 不接受 `ledger`，这是检查而不是遗漏

一个持有「会记录外部效果」的工具的注册表，在没有账本时会拒绝装配。
API 侧不传账本，于是这条拒绝就是这个进程的检查：**一个 ledgered 工具哪天出现在
这一侧，进程会在装配时停住，而不是先派发效果再说。**

## 4. 后果

### 4.1 Code 会话的模型信封从 120 变成 300（在 code-local 上）

这是本 ADR 唯一一处改变用户可见行为的地方，也是它的全部意义：那次事故的修法
第一次真的生效。副作用是**一次卡住的模型调用现在会占用最多 300 秒而不是 120 秒**，
而这正是那条配置写下来时想要的。不想要的部署把数字改回去即可——
从今天起改它有用。

### 4.2 `max_tool_argument_bytes` 改动的含义变了

此前改它只改 `policy_fingerprint`（因而改 `policy_identity`，因而改每条决策记录里的那个串），
拦截阈值纹丝不动。此后它同时改两者。**已有部署若曾把这个数调大以「解决」某个被拒的调用，
在 Worker 上此前也没生效**（Worker 侧同样没传），所以这条对两个进程都是新行为。
出厂值不变，因此默认部署没有任何差别。

### 4.3 没有改 Worker，也没有统一两个进程的装配

`task_worker/composition.py` 保持它自己的单一网关构造。把两个进程的装配合并是另一件事，
而且它们的差异是真的（账本、MCP 绑定、子代理池）。**本 ADR 只让四个数的含义一致，
不让两条装配路径合一。**

### 4.4 顺手关掉的与顺手记下的

- 关闭「`policy.max_tool_argument_bytes` 零装配」（本次由已知缺口新登记后当场关闭）。
- `docs/status.md` 那节的「谁在用」表已就地更正为注入点，并注明原表填的是读取点。
- **本 ADR 没有触及 `runtime.cancellation_poll_seconds` 与
  `runtime.max_parallel_write_tools`**：前者仍无读者，后者的写并发由「独占工具永远是
  一组一个」在运行时实现而不读配置。两条都登记在已知缺口的「配置叶子零读者」条目里。

## 5. 重审条件

本 ADR 拿到的是**机制**证据（改配置能改变信封、每个构造点都被守门测试盯住），
不是**效果**证据。`model_timeout_seconds = 300` 是否足以覆盖那次事故的长尾，
要等下一次同形事故或一次刻意的长回合复现来说话。

若 300 仍不够，正确的动作是继续调这个数并同时调 `[model.main] timeout_seconds`
——它们成对这件事，从今天起才是真的。
