# ADR-048：导出闸门默认关闭，控制台不再有跨任务收件箱

- 决策点：[ADR-038](./0038-the-export-gate-guards-a-list-not-a-boundary.md) §4 要求的那份
  ADR——把 `workflow.export_requires_approval` 的**仓库默认值**改成 `false`；以及
  「待我确认」这一页要不要留
- 状态：**接受**；ADR-038 §4 的第二个重来条件由本 ADR 应答，第一个（export 外发）
  原样继承
- 日期：2026-08-15
- 影响：`settings.py` 的 `export_requires_approval` 默认 `True → False`；
  `config.default.toml` 同步；删除 `web/src/features/approvals/` 与 `/approvals` 路由。
  **配置字段、审批 API、审批事件、`TaskApprovalGate`、`PostgresApprovalStore`、
  migration 0018、`policy.write_tools_require_approval` 一字不改。**
- 依赖：ADR-015（导出授权）、ADR-031（第二张图）、ADR-038（闸门守的是清单）

## 1. 决策

仓库默认值变成 `false`，控制台移除「待我确认」页。

## 2. 为什么是这个仓库

ADR-038 §1 已经把论证写完了：export 把字节写进**这个租户自己的** artifact store，
kind 是 `report`，owner 是提交者本人。写完之后文件没有离开这个部署，谁也收不到它，
要它到人手上还需要一次**独立的、已经鉴权的**下载。所以那道闸门拦住的事情是
「一个文件出现在提交者自己的附件列表里」。

ADR-038 §2.1 把这件事定为一个部署自己的选择，并在 §4 说：把 `false` 变成仓库默认
需要一份新 ADR，因为那是替所有部署回答问题，而多人部署里「作者之外的人先看一眼」
是有意义的。

**这个仓库就是那台单人机器。** `config.local.toml`、`config.demo-local.toml`、
`config.word-local.toml` 三个 profile 早就写着 `false`，因为在这里那道闸门问的是
「你批准把文件交给你自己吗」。默认值留着 `true` 的效果不是更安全，而是让唯一还在
用默认值的路径——一个刚 clone 下来什么都没配的部署——去问一个它自己也会无条件
点通过的问题。

ADR-038 §1 那句话在这里成立：**一道人人都会无条件批准的闸门，比没有闸门更坏。**
它在审计记录里留下「有人批准过」的痕迹，而那句话已经不再意味着有人看过。

## 3. 什么没有变

- **配置字段还在。** 一个多人部署写 `export_requires_approval = true`，得到的东西
  和 ADR-038 之前逐字相同：图路由到 approval 节点、开审批行、`approval_id` 进报告
  头部。
- **审批 API、事件、闸门机器全部保留**——`routes/approvals.py`、
  `application/approvals.py`、`PostgresApprovalStore`、`TaskApprovalGate`、
  `TaskApprovalRequested` / `TaskApprovalDecided`、migration `0018_approvals`，
  连同 `tests/api/test_approval_api.py` 一起。删掉机器等于替不是这台的部署回答，
  而这份 ADR 只回答默认值。
- **`policy.write_tools_require_approval` 一个字都没动。** 它是 `[policy]` 里被单值
  `Literal` 钉死的授权不变量之一，而且没有任何代码消费它——ADR-038 §2.1 特意把这个
  可关的开关放进 `[workflow]` 而不是那张表，就是为了让「这张表里的东西关不掉」
  这句话没有例外。本 ADR 不碰那张表。
- **`TaskState.export_requires_approval` 的默认值仍是 `True`**，这不是不一致：
  settings 的默认回答「什么都没说的部署想要什么」，state 的默认回答「一个丢了这个
  字段的 TaskState 意味着什么」，两者的安全答案本来就不同。

## 4. 删掉「待我确认」丢了什么，没丢什么

**没丢的是回答审批的能力。** `WorkPage.tsx` 的 `ApprovalSection` 就在等待的那个
Task 的详情里渲染决定，它自己调 `getApproval` 和 `decideApproval`——那才是权威位置，
和 Code 把审批内嵌在会话里是同一个形状。被删掉的是一份**重复的跨任务列表**。

**丢的是那个收件箱。** 一个重新开启闸门的部署，现在得逐个 Task 去看，或者直接用
HTTP。这在只有一种确认（导出报告）的今天不构成困难；等到有第二种、第三种确认的
那天，收件箱要重新长出来，而那时它该是「所有待我处理的事」而不是「所有审批」。
记在 `docs/known-gaps.md`。

## 5. 真正的风险是沉默

一道不再触发的闸门，是没人会注意到它消失的闸门。三样东西对抗这一点：

1. `tests/api/test_approval_api.py` 原样保留且必须绿——它红了就说明删除动作伸进了
   API 而不是控制台；
2. `WorkPage` 的 `ApprovalSection` 保留，所以 `true` 的部署仍然点得到；
3. `tests/config/test_local_console_profile.py` 里那条断言现在钉住 `False`。那条
   测试原本钉的是 `True`，并且在 docstring 里写明「一个移动了默认值的改动会静默
   满足前半句」——它确实抓住了本 ADR 的改动。现在它反过来钉住新的默认值，任何
   想悄悄改回去的人都得编辑那一行并解释为什么。

## 6. 什么会让这条决定重来

**export 一旦外发**——推到对象存储的公开前缀、发邮件、调 webhook。§2 的推理立刻
失效，因为「没人收得到」不再成立。这条逐字继承自 ADR-038 §4，而且答案也一样：
那时该做的不是把默认值改回来，而是把**那个外发动作**放到闸门后面。闸门要守的
一直是「东西离开了这里」，而不是「文件被写出来了」。

**这个仓库不再是单人机器。** 本 ADR 的全部依据是 §2 那句「这个仓库就是那台单人
机器」。它一旦不成立，默认值该跟着回去。
