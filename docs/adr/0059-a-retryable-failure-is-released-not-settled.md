# ADR-059：可重试的失败要被放回队列，而不是被定案

- 决策点：`ErrorInfo.retryable` 到底管不管控制流；执行失败的 Task 能不能吃到
  `coordination.max_attempts`；两个从未被消费的 `workflow.node_*` 配置键怎么办
- 状态：**接受**。Worker 对"运行分类为可重试"的执行失败调 `release_for_retry`
  （退避与 reclaim 同公式），耗尽后落 `failed` 而非 `dead_letter`；
  `workflow.node_retry_max_attempts` / `node_timeout_seconds` **删除**，
  schema `1.16` → `1.17`
- 日期：2026-08-16
- 影响：`workers/task.py`（`_execute` 返回 `_ExecutionFailure{reason,retryable}`，
  运行环按 `attempt_count < max_attempts` 分支到 `_release_for_retry`）；
  `bootstrap/settings.py` 删两个死键并抬版；`config.default.toml` 同步，且把两处
  实测过的预算写回默认（`runtime.max_steps` 12 → 40、
  `multi_agent.max_tokens_per_agent_invocation` 16000 → 120000，
  `config.demo-local.toml` 不再重复覆盖）；ownership.yaml 同步。
  **Registry、`release_for_retry` 的实现、epoch 栅栏、reclaim 一律不动**——
  机械早就在了，缺的只是这一个调用点
- 依赖：known-gaps B-06（实测证据）、ADR-040（预算拒绝的走法）、
  ADR-041（心跳自检）

## 1. 背景：retryable 是一句话，不是一个分支

`ErrorInfo.retryable` 在 Worker 里只被读过一处：`_failure_detail` 把它拼进
`status_detail` 的展示字符串。B-06 记录了代价的实测形态：2026-08-13，demo
profile 对真实供应商，同一个 Task 连续三次死在 `RemoteProtocolError` /
`ConnectError` 上，三次都带着 `retryable: true`，三次都是终态失败——同时代理
测得 6/6 通、0.8 秒。瞬时抖动变成永久失败，而 `coordination.max_attempts=5`
只喂给租约过期的 reclaim。

## 2. 决定

失败从 `_execute` 出来时带上它的分类（`_ExecutionFailure`）。运行环：

- `retryable and attempt_count < max_attempts` → `release_for_retry`，延迟用
  reclaim 自己的公式 `min(retry_max, retry_base * 2**(attempts-1))`——租约过期
  和执行失败是"稍后再试"的两个生产者，两套公式会漂移；
- 否则 `mark_failed`，可重试但耗尽的在 reason 里写明
  `gave up after attempt N of M`。

**分类是保守的**：只有 `AgentNodeFailedError` / `TaskNodeRunFailedError` 且
`outcome.error.retryable is True` 才算——这正是 B-06 实测的传输抖动落点。
证据错误、没有 `ErrorInfo` 的节点失败、图自己抛的异常一律不重试：确定性的
失败重试五次是同一个答案付五倍价钱。

**图内主动失败天然在边界外**：`position.failed` 的 checkpoint（人拒绝了审批）
走 reconcile → `settle_failed`，从不经过异常路径——重试拦的是异常，不是决定。
`tests/workers/test_task_worker_retry.py` 把这条边界钉死。

**耗尽落 `failed` 而非 `dead_letter`**：端口文档定义 dead_letter 为"下一次也
一样"，对传输抖动不成立；reaper 保留它处理租约耗尽的场景。

## 3. 顺手清掉的两个谎言和两个陷阱

`workflow.node_retry_max_attempts = 2` / `node_timeout_seconds = 600` 在
settings 里被校验、在任何地方都不被消费——LangGraph 适配器没有 RetryPolicy
也没有每节点时钟。读到配置的人以为节点会重试，而它们不会。删除；非增量改动，
schema 抬到 1.17。重试现在住在 Task 层，一个旋钮（`coordination.max_attempts`）
回答一个问题。

`runtime.max_steps = 12` 与 `multi_agent.max_tokens_per_agent_invocation =
16000` 是每个真实跑过任务的 profile 都必须调高的默认值（demo-local 里有两段
实测注释）。一个人人都得改的默认是陷阱不是默认：实测值（40 / 120000）写回
default，demo-local 撤掉重复的覆盖。

## 4. 代价

重试花真 token。上界是 `max_attempts`（attempt_count 在 claim 时递增，与租约
重试共享同一个计数），且分类保守——不会有确定性失败在烧预算。C-05（v1 critic
拿到合法 JSON 仍死）不受本 ADR 掩盖：那类失败没有 retryable 分类，照旧当场
失败，known-gaps 里保持开放。

## 5. 证据

- `tests/workers/test_task_worker_retry.py`：五条——可重试→放回且退避正确、
  不可重试→定案、耗尽→定案且写明次数、未分类异常→不重试、图内决定→零重试。
- `tests/persistence/test_task_registry.py` 既有的 `release_for_retry` 契约
  覆盖不变。
- `tests/config/test_local_console_profile.py`：发货默认即实测值，console 继承
  而非再覆盖。
