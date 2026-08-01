# ADR-015：唯一写节点的授权上限，与人站在哪个边界

- 决策点：WP10-07 `export_artifact`
- 状态：**接受**
- 日期：2026-07-31

## 背景

架构基线 M4 写着「只增加一个确定性写入 node：`export_artifact`」。副作用协议在
PR #63 已经全部就位——`tool_executions` 表、稳定 operation key、两段提交、重算
授权、`needs_reconciliation`——但**没有任何工具带 operation key**，`export` 节点
在真实 handler 集合里根本不存在，落到 `_default_handler`（passthrough）。

也就是说：人批准之后，图走到 `export`，**什么都不做**然后结束。DoD 的
「审批后的副作用在恢复测试中只发生一次」没有任何东西可测——不是测不过，是没有副
作用。

本轮实现 `export_artifact`，随之必须回答一个此前没人需要回答的问题：**Task 的授权
上限该是什么。**

## 问题

实测（不是读代码）：

```text
AuthorizationEnvelope().permits(external_search SPEC)  -> False
AuthorizationEnvelope().requires_approval(SPEC)        -> True
max_tool_risk = "read"   allowed_tools = ()
```

`project_task` 给每个 Task 的 envelope 是 `AuthorizationEnvelope()`，空 allowlist。
`permits()` 的第二条就是「不在 allowlist 里就是 False」，所以**到今天为止，Task 里
的每一次工具调用都是 `policy_denied`**。external search 一直走的是那条被拒的路
（它本来也没有 provider，所以从没有人发现）。

于是有三个决定要做，且都必须显式做：

1. allowlist 要不要写 `export_artifact`；
2. `max_tool_risk` 要不要从 `read` 抬到 `write`；
3. `approval_required_risks` 默认含 `write`，要不要保留。

前两条是「不做就没有 export」，没有争议。**第三条是这份 ADR 真正的内容。**

## 决策

**envelope 点名 `export_artifact`，ceiling 抬到 `write`，`approval_required_risks`
置空。**

```python
TASK_V1_AUTHORIZATION_ENVELOPE = AuthorizationEnvelope(
    allowed_tools=(EXPORT_ARTIFACT_TOOL,),
    max_tool_risk="write",
    approval_required_risks=(),
)
```

置空的理由不是「审批不重要」，恰恰相反：**v1 的人已经站在图边界上了。**

基线 M4 的原文是「在 **Graph 边界**使用 `interrupt()` 实现报告导出审批」，而
「Tool 级人工审批和 resume」被明确列在 M7 Optional Lab。这两件事在本仓的实现里是
分开的两套机制：

| 边界 | 机制 | 状态 |
|---|---|---|
| Graph | `approval` 节点 `interrupt()` + approvals 账本 + `/v1/approvals` | 已实现，已实测 |
| Tool | `ToolGateway._await_approval` | **只会拒绝** |

`_await_approval` 的实现是发一条 `PermissionRequested` 然后
`refuse(code="approval_required")`——它的错误正文到今天还写着 "and no approval
facility exists yet"。它不能暂停、不能被恢复、没有人能回答它。

所以如果保留 `write` in `approval_required_risks`，实际后果是：人在图边界批准了导
出 → 图恢复 → 走到 export 节点 → gateway 说「这个工具需要人工审批」→ 拒绝 → Task
失败。**一个只会说不的门不是门。**有一条以此命名的测试钉住这一点
（`test_an_envelope_requiring_tool_approval_exports_nothing`）。

### 这不是把授权放宽了

三层仍然都在，被去掉的只有那层不可能被满足的：

- **envelope 只点名一个工具。** 不是抬 ceiling 放行同风险的一切——有一条测试断言
  同为 `write` 但换个名字的 spec 仍然 `permits() == False`；
- **principal scope 仍然要求。** `export_artifact` 声明 `artifact:export`，
  `EnvelopePolicyEngine` 要求调用方**持有**该 scope，否则
  `missing_permission_scope`。提交 Task 的人没有这个 scope，导出就跑不了。
  这也是**演示时必须带 `x-principal-scopes: artifact:export`** 的原因；
- **两段提交里的重算授权仍然在。** gateway 在记录 intent 之后、dispatch 之前再问
  一次 policy，所以「批准之后、导出之前 ACL 被收紧」仍然会挡住这次写。

换句话说：**graph 边界回答「这件事该不该做」，envelope + scope 回答「这个身份能不
能做」，ledger 回答「这件事是不是已经做过」。**去掉的那一层问的是「有没有人批
准」——那个问题 100 秒前刚被真人回答过。

## operation key 选 `export:{task_id}`

不是 `export:{task_id}:{draft_ref}`。key 只认 Task，draft 放进**参数**里。

因为 ledger 的规矩是「一个 key 一个 canonical request」：同 key 不同参数 →
`ToolOperationConflictError`。把 draft 放进 key，一份改过的 draft 会得到**一个新
key**，于是安静地导出第二份；放进参数，它**撞上同一个 key 然后被拒**。

破坏验证证实了这一点：把 draft 加进 key 之后 9 条测试失败。

## 后果

**gateway 的成功路径现在记录产物 id。** `_report` 原本在成功时写
`detail=None`。崩溃窗口「产物已写、ledger 已结算、checkpoint 未写」下，重跑会从
ledger 读到 `succeeded`，却拿不到它造了什么——只剩「再导一次」和「报告成功但指不
出东西」两个坏选择。现在成功时把 `result.artifact.artifact_id` 写进
`outcome_detail`，恢复路径读它。代价是这一列从「给人看的原因」变成了「有时是给机
器读的 id」，已写进注释。

**恢复必须核对 canonical hash。** 第一版没核对，破坏验证抓到了：同 key 不同 draft
时，恢复路径会把**上一次**导出的产物当成这次的答案返回——把一个冲突变成一次说谎的
成功。现已核对，并有一条对应测试。

**`export_ref` 进 `TaskState`，且它要求 `approval_decision == "approved"`。**
一个带着无人批准的导出的 checkpoint **加载不出来**。

**新增依赖方向：`bootstrap.projections` → `adapters.tools.export_artifact`。**
只为取一个工具名常量。备选是把字符串写两遍，那样 envelope 和工具可以在没人察觉的
情况下各说各话。

## 被否决的替代方案

**A. 让 `export` 保持 passthrough，等 tool 级审批做完再说。** 那意味着 M4 唯一的写
节点继续不存在，ledger 继续没有任何东西在用，DoD 那条继续无法演示。而 tool 级审批
按基线本来就不在 v1。

**B. 在 gateway 里让「图已批准」满足 tool 级审批要求。** 需要把图的审批状态传进
gateway，等于让 tool 边界去读 workflow 的事实——两个边界就此耦合，而且第一个真正
需要 tool 级审批的工具会发现这条路已经被一个特例占住了。

**C. 把 envelope 做成配置项。** 配置能关掉的授权上限，就是一个部署可以顺手关掉的
授权上限。ceiling 属于代码里的部署决定，要改它应当留下一次 diff 和一次 review。
