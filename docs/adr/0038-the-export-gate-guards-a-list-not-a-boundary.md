# ADR-038：导出闸门守的是一份清单，不是一条边界

- 决策点：Task 的导出必须经过人工审批吗；ADR-031 §2.4 那句"v2 不放宽任何边界"里，
  导出审批算不算一条边界；一个部署能不能自己决定不要它
- 状态：**接受**，收窄 [ADR-031](./0031-a-second-graph.md) §2.4；并移走
  [ADR-015](./0015-export-authorization.md) 推理所依赖的一个前提，见 §3
- 日期：2026-08-11
- 影响：新增 `workflow.export_requires_approval`（默认 `true`，配置 schema 不变）；
  `TaskState` 增加同名字段并在提交时冻结；`route_review` 在无闸门时直接路由到
  `export`；`TaskState` 的 `export_ref` 不变量与 export 节点的前置条件都改为按该字段
  判断；`ReportExportPort.export` 的 `approval_id` 变为 `Identifier | None`。
  **授权信封、scope 检查、artifact 的租户与所有者边界、`export_artifact` 的
  operation key 一字不改。**
- 依赖：ADR-015（导出授权）、ADR-027（只读外、只写内）、ADR-031（第二张图）

## 1. 背景：这道闸门在一台单人机器上问的是"你批准把文件交给你自己吗"

ADR-031 §2.4 的原话是"同一个 Tool Gateway、同一个授权信封、同一条 artifact 通道、
同一个导出审批、同一套预算"。这句话把导出审批和另外四样并列，而另外四样都是**授权
边界**：它们决定一个 principal 能不能碰到某个东西。

导出审批不是。把它和那四样并列，是把"一次人工确认"错当成了"一道权限检查"。

区别在 export 节点实际做了什么：它把草稿的字节写进**这个租户自己的 artifact
store**，kind 为 `report`，owner 是提交这个 Task 的 principal。写完之后：

- 这份文件**没有离开这个部署**。artifact 的读取仍然逐 principal 鉴权，和写之前
  完全一样；
- 谁也收不到它。要它到人手上，得有人在控制台上点下载，而那次下载是一个**独立的、
  已经鉴权的**读请求；
- 它不覆盖任何东西。ADR-028 的工作区是版本化的，artifact 是内容寻址的。

所以这道闸门拦住的事情是：**一个文件出现在提交者自己的附件列表里**。

在多人部署里这仍然可能有意义——一份要交出去的报告，作者之外的人先看一眼。在一台
单人机器上它退化成一句"你批准把文件交给你自己吗"，而每个 Task 都问一次的确认，
教会的是闭着眼睛点通过。一道人人都会无条件批准的闸门，比没有闸门更坏：它在审计
记录里留下"有人批准过"的痕迹，而那句话已经不再意味着有人看过。

## 2. 决策

### 2.1 可配置，默认不变

新增 `workflow.export_requires_approval`，默认 `true`。什么都不写的部署得到的是
ADR-031 原本的形状，一步不差。

放在 `[workflow]` 而不是 `[policy]`，因为 `[policy]` 里那些字段是 ADR-022 起
就用单值 `Literal` 钉死的授权不变量（`write_tools_require_approval`、
`tenant_filter_required`、`default_effect`）。把一个能关的开关放进那张表，会让
"这张表里的东西关不掉"这句话不再为真——而那句话的价值恰恰在于它没有例外。

### 2.2 跳过闸门，不是自动批准

无闸门时 `route_review` 从 `review` 直接路由到 `export`：**不开审批行、不写
`approval_id`、不写 `approval_decision`**。

自动批准是更省事的实现——保留整条路径，让 approval 节点自己回答"approved"。本
ADR 拒绝它，理由和 ADR-034 §2 拒绝"从消息里抠出 JSON 对象"是同一个：那会让记录
说一件没有发生过的事。一条 `TaskApprovalDecided{decision: approved}` 的含义是
"某个 principal 在某个时刻做了决定"，而系统自己填进去的那条，事后无法与真的区分。

`route_approval` 保持原样，仍然在没有决定时拒绝导出。所以"绕过闸门"只有 §2.1
这一条路，且它在图的声明里可见。

### 2.3 冻结在 Task 上，不是每跳去读配置

`TaskState.export_requires_approval` 在 Task 载入时从配置抄一次，然后跟着
checkpoint 走，理由和 `wants_report` 一样：路由是状态的纯函数。一个在闸门前
暂停的 Task，如果部署在这期间改了配置，恢复时会走进一张与它暂停时不同的图——
而它暂停的那个节点正等着一个人的答案。

它也必须是一个**图通道**（`GraphState`）。不是的话每跳都会退回默认 `true`，于是
关掉闸门的部署照样暂停，而且暂停在一个没有任何 approval 行可以回答的节点上。
这一条有测试钉着：`test_every_task_state_field_is_a_graph_channel`。

### 2.4 不变量跟着条件走，而不是被删掉

`TaskState` 原有一条：`export_ref` 存在则 `approval_decision` 必须是 `approved`。
export 节点也有一条对应的前置检查。

两条都改成**按 Task 自己的 `export_requires_approval` 判断**，而不是取消。有闸门
的 Task 拿到的约束和以前逐字相同；无闸门的 Task 如果还要求一个 approval id，它的
导出状态将无法表示——图会路由到 export，然后 checkpoint 写不进去。

### 2.5 `approval_id` 是溯源，不是凭证

追到底，`approval_id` 从图一路传到 `render_report`，用途是报告头部的一行：

```
- Approved by: apr_0000…
```

没有任何地方拿它做权限判断。所以它变成 `Identifier | None`，而无闸门时头部写：

```
- Approved by: not required by this deployment
```

三个可选项里只有这一个是诚实的。编一个 id 会把一次不存在的审批写进交付出去的
文档；把这一行删掉，则让报告读起来和这个字段存在之前的版本一样，而provenance
头部的全部职责就是让读者知道手里的东西是什么。

`export_artifact` 的 input schema 里 `approval_id` 随之变为可选。它仍然参与
ledger 的 canonical request hash——无闸门时这个键**缺席**而不是取值 `null`，
所以哈希是对 draft 单独取的。同一份草稿的第二次审批在无闸门的部署里不存在，
这个区分因此没有东西要表达。

## 3. 后果，包括一个必须说清楚的

### 3.1 它移走了 ADR-015 的一个前提

ADR-015 把 `approval_required_risks` 置空，理由是一句原话：**「置空的理由不是
『审批不重要』，恰恰相反：v1 的人已经站在图边界上了。」** 它的论证是两层人工确认里
tool 那层只会拒绝（`_await_approval` 不能暂停也无人可答），所以留着它等于让导出
必然失败——而图那层的人还在。

关掉本 ADR 的开关，**图那层的人也不在了**。所以 ADR-015 那句"这不是把授权放宽了"
在这种部署下不再由它自己的理由支撑。诚实的表述是：

- **没有变的**：`artifact:export` scope 检查、授权信封对 `export_artifact` 的点名、
  `write` 风险上限、artifact 的租户与所有者边界、ledger 的一次性保证。这些都是
  ADR-015 真正在管的东西，一个都没动；
- **变了的**：这条路径上**不再有任何人工确认**。一个持有 `artifact:export` 的
  principal 提交的 Task，会自己把报告写进它自己的 artifact store。

之所以认为这可以接受，是 §1 那段推理——那份 artifact 谁也收不到，要它到人手上还
需要一次独立的、已鉴权的下载。**但这个判断依赖 export 不外发**，所以它写在 §4 的
重来条件里。

### 3.2 其余

- 一台单人机器（`config.local.toml`、`config.word-local.toml`）现在能把一个
  要产出文件的 Task 从提交跑到 `succeeded`，中间不停。实测
  `task_3ae4d5a0…`：`review → export → succeeded`，时间线里没有
  `TaskApprovalRequested`，报告头部写着"not required by this deployment"。
- 多人部署什么都不用做。默认值、事件形状、审批 API、控制台的审批区都没有变。
- **代价**：现在有两条通往 export 的路径，而不是一条。两条都在
  `add_conditional_edges` 的目标表里，也都有编译后跑一遍图的测试——这不是多余的
  谨慎：第一版只改了路由函数、漏了那张表，所有路由单测照过，而第一个真实 Task
  死在 `KeyError: 'export'`。路由函数和边表是两个必须同时对的东西，只有把图编译
  出来跑才检查得到。

## 4. 什么会让这条决定重来

**export 一旦外发。** 推到对象存储的公开前缀、发邮件、调 webhook——§1 的推理立刻
失效，因为"没人收得到"不再成立，§3.1 里那个"可以接受"的判断也随之失效。那时该做
的不是把这个开关改回去，而是把**那个外发动作**放到闸门后面：闸门要守的一直是
"东西离开了这里"，而不是"文件被写出来了"。

**默认值改成 `false`。** 本 ADR 只允许一个部署自己选择，没有说哪个更好。要把
`false` 变成仓库默认，需要一份新的 ADR，因为那是在替所有部署回答 §3.1 里那个
问题——而多人部署里"作者之外的人先看一眼"是有意义的。
