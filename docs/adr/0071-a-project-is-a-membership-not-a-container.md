# ADR-071：Project 是一层归属，不是一个容器

- 决策点：侧栏现在按**产品**分组——对话、任务、编码、知识库。人做事不按产品分，
  按**一件事**分：同一个季度复盘会同时有三段对话、两个任务和一个编码会话。要不要
  引入 Project 这个实体；如果要，它**拥有**这些东西，还是只**标注**它们
- 状态：**接受**。Project 是一层可空的归属标注，不是容器。
  `conversation_sessions.project_id` 与 `task_runs.project_id` 都是 nullable 且
  `ON DELETE SET NULL`；知识库走关联表而不是外键列。第一版只做 owner-private，
  不引入团队共享，也不新增任何 ACL 概念
- 日期：2026-08-20
- 影响：新 `domain/projects.py`、新 `ports/projects.py`、新
  `adapters/persistence/projects.py`、新 `adapters/memory/projects.py`、新
  `application/projects.py`、新 `apps/api/routes/projects.py`、新迁移
  `0030_projects`；`conversation_sessions` 与 `task_runs` 各加一列
- 依赖：[ADR-047](./0047-a-session-is-named-by-its-first-instruction.md)（会话由
  第一句指令命名——Project 不改这条，它只是多一层归属）、
  [ADR-044](./0044-no-remote-no-production-identity.md)（没有远端就没有生产身份——
  owner-private 的边界靠它成立，本 ADR 不新增身份概念）

## 1. 背景：侧栏按产品分，而人不按产品想事情

上一轮把三根互相重复的导航收敛成一根侧栏，里面直接放当前产品的真实事务。那次改动
解决的是「导航有几层」，没有解决「这些事务之间有没有关系」。

现在一个人要接着上周的季度复盘往下做，他得记住：那段对话在对话列表的第四行，那个
导出任务在任务列表里，那次改数据的编码会话在编码列表里。三个列表各自按时间排，而
把它们联系起来的东西——**这是同一件事**——在这套界面里没有任何表示。

这不是「再加一层分组」。三层导航之所以要被砍掉，正是因为它们是同一个维度（产品）
被重复表达了三遍。Project 是**另一个维度**：产品回答「这是什么工具」，Project
回答「这是为哪件事做的」。

## 2. 决策：归属，不是容器

```
projects                         一件事，有名字，属于一个人
  ├─ conversation_sessions.project_id   nullable, ON DELETE SET NULL
  ├─ task_runs.project_id               nullable, ON DELETE SET NULL
  └─ project_knowledge_bases            关联表，多对多
```

### 2.1 为什么是 nullable，而且必须是

数据库里已经有对话、任务和知识库。给它们加一个 NOT NULL 的 `project_id`，意味着
迁移要替每个人凭空造出一个 Project，把他所有历史塞进去——一个他没有起过名、没有
决定过边界、却从此出现在每一屏上的分组。

那个分组会撒两次谎：它声称这些东西属于同一件事（它们不属于），而且它声称这是**用户
的判断**（不是）。

所以 `project_id` 可空，界面上没有归属的东西就照实显示成没有归属。**不替用户创建
虚假的项目**，这条比任何数据模型上的整洁都重要。

### 2.2 为什么删除 Project 不删除里面的东西

`ON DELETE SET NULL`，不是 `CASCADE`。

一个 Project 是**一层标注**。删掉标注不该删掉被标注的东西——那段对话里有你问过的
问题和得到的回答，那个任务产出过一份文件。让「整理一下我的项目列表」这个动作可以
连带删掉三个月的工作记录，是把一个整理动作做成了一个破坏动作。

反过来说，如果哪天真的需要「删项目连同内容」，那是一个**明确的、要单独确认的**动作，
不是删除的默认语义。

### 2.3 为什么知识库走关联表

对话和任务各自属于一件事：那段对话是为季度复盘问的，不会同时也是为招聘做的。

知识库不是。同一份《产品手册》会被复盘用到、被招聘用到、被客服用到。给它一个
`project_id` 列，等于强迫人在三件事里选一件，或者把同一份资料上传三遍。

所以：`conversation_sessions` 和 `task_runs` 用外键列，`project_knowledge_bases`
用关联表。这不是为了对称好看，是因为这两组的基数本来就不一样。

### 2.4 第一版只做 owner-private

`projects` 有 `tenant_id` 和 `owner_id`，读写都按 `(tenant_id, owner_id)` 限定。
没有共享，没有成员表，没有权限位。

这不是"以后再补"的托词，是范围的一部分：团队共享会带来「谁能把别人的对话挪进我的
项目」这个问题，而那个问题的答案取决于一套这个仓库还没有的协作模型。把它和
「让一个人能把自己的三件东西归到一起」混在一起做，会让后者也拖到前者想清楚为止。

### 2.5 `archived_at`，不是删除标记

归档的项目不出现在侧栏，但仍然可读、可通过深链打开、里面的东西仍然带着它的
`project_id`。这和删除是两件事：删除放开归属，归档只是收起来。

## 3. 被拒绝的方案

**用 localStorage 在前端分组。** 上一轮明确没有这么做，理由在这里写清楚：一个只
存在于一台浏览器里的分组，换台机器就没了，换个身份也没了，而且服务端对它一无所知
——于是「这个任务属于哪个项目」这个问题，后端永远答不出来。它看起来是同一个功能的
便宜版本，实际上是一个**会撒谎的**版本。

**Project 拥有会话（`CASCADE`）。** 见 §2.2。

**给知识库加 `project_id` 列。** 见 §2.3。

**用标签（多对多、无名实体）代替 Project。** 标签是好东西，但它回答不了「打开这
件事，让我看到它的全部」——那需要一个有身份、有名字、能被打开的东西。标签可以后加，
而且它和 Project 不冲突。

## 4. 接口

```
GET    /v1/projects                 owner 的项目，未归档在前
POST   /v1/projects                 名字必填，服务端规范化
GET    /v1/projects/{id}
PATCH  /v1/projects/{id}            改名 / 归档 / 取消归档
DELETE /v1/projects/{id}            放开归属，不删内容（§2.2）
GET    /v1/projects/{id}/items      这件事底下的对话、任务、知识库
PATCH  /v1/chat/sessions/{id}/project
PATCH  /v1/tasks/{id}/project
```

`PATCH …/project` 的 body 是 `{"project_id": "..." | null}`。`null` 是**取消归属**，
不是「没传这个字段」——这两件事在一个 PATCH 里必须能被区分，否则「把它从项目里拿
出来」就没有表达方式。

## 5. 不变量

1. **一个 Project 只属于一个人。** 所有读写按 `(tenant_id, owner_id)` 限定，
   没有第二条路径。
2. **归属不影响可见性。** 把一段对话放进项目不会让它对别人可见，拿出来也不会让它
   对自己不可见。Project 不是 ACL，一个字都不是。
3. **归属可空，且空是正常状态。** 没有迁移会替用户创建项目，没有界面会强迫他先
   建一个项目才能提问。
4. **删除项目不删除内容。** `ON DELETE SET NULL`。

## 6. 怎么验证

- 契约测试对 in-memory 和 PostgreSQL 两个实现跑同一套（本仓库既有做法）：
  归属可空、跨 owner 读不到、删除后内容仍在且 `project_id` 变成 `NULL`、
  归档不影响深链可读。
- API 测试钉住 `{"project_id": null}` 与「不传该字段」的区别。
- 已有数据的迁移测试：加列之后所有历史行的 `project_id` 都是 `NULL`，
  且没有任何一行被写进任何项目。
