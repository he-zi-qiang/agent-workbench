# ADR-088：工作集里的文件本来就是一件 artifact

- 决策点：2026-08-28 的用户反馈——「没有看见生成后的产物，产物也没有预览」。查下来
  产物面板与内联预览**本身是好的**（一次成功任务验证过），看不见是因为任务
  `budget_exceeded` 死在 `export` 之前。但顺着查出了一条真的：一个委派任务真正的
  交付物是子代理写进工作集的 `failure-modes-comparison.md`，而侧栏把它列在
  「任务工作区里的文件」下面，并写着**「控制台打不开它们」**（F-14）
- 状态：**接受**。`ToolResult` / `ToolCompleted` 新增 `workspace_write_refs`，由两个
  工作区写入工具发布；控制台据此把工作集文件名变成可打开的产物，取字节走**既有的**
  `/v1/artifacts/{id}`。
  **明确不做**：不给 Task 新增任何工作区读取路由、不给工作集条目一个稳定公开地址、
  不碰 `/v1/code/sessions` 下的那三条路、不动 `config_schema_version`
- 日期：2026-08-28
- 影响：`domain/tools.py`、`domain/events.py`、`runtime/tool_gateway.py`
  （`workspace_write_refs` 直通）；`adapters/tools/workspace.py`（两处发布）；
  前端 `workTimeline.ts` / `WorkPage.tsx`
- 关联：[ADR-063](./0063-a-produced-name-is-a-fact-not-a-sentence.md)（名字是事实）、
  [ADR-086](./0086-a-produced-file-is-not-answerable-to-which-store-it-landed-in.md)
  （同一形状，另一个 store）、[ADR-028](./0028-task-workspace.md)

---

## 1. 头条：这条缺口的前提比它需要的强

`known-gaps.md` 的 F-14 写着不修的理由：

> 把它们做成**可打开的**要给 Task 开一条工作区读取面，而工作区的列举/读取/运行三条路
> 全挂在 `/v1/code/sessions` 下并做 `mode="code"` 检查。复制一份等于第二套授权与第二条
> 寻址。

这段推理没错，但它假设了一件事:**要让读者看到字节，就得有一条新的取字节的路。**
对工作集来说，这个假设不成立。

`WorkspaceManifest.entries` 的类型是 `dict[WorkspaceName, ArtifactRef]`
（`domain/workspace.py`）。一个工作集条目**存下来就是一件 artifact**，有 id、有
tenant、有 owner、有 sha256。而 `GET /v1/artifacts/{artifact_id}` 与它的
`/preview` 早就存在，并且按 owner 鉴权。

**所以字节的读取面不需要新建——它已经在那儿，而且已经是那一条。** 缺的从来不是一条路，
是**「名字绑到哪个 artifact」这件事没有离开服务端**。

## 2. 那条不该造的路，和它不该造的真正理由

F-14 的直觉是对的，只是理由该换一个更准的：

`Workspace.locate` 的 docstring 自己写着——

> A name is still resolved against the manifest at `version`, which is the whole
> reason a workspace entry has **no stable public address**: the same name is a
> different artifact at a different version, and only the session row knows
> which version this caller is at.

**`report.md` 不是一个地址。** 它在版本 A 是一份文件，在版本 B 是另一份。给
「`(task_id, name)` → 字节」造一条路，等于给一个会变的东西发一个看起来不会变的地址，
而这一条**恰恰是 Code 那三条路能成立、Task 这条不能的差别**：Code 的 session 行持有
当前版本，Task 的版本在 LangGraph 检查点里。

本 ADR 因此**不造那条路**。它发布的是**绑定**，不是字节，而且是**某一次写入当时**
那个绑定——那一刻是确定的，`artifact_id` 是内容寻址且不可变的。读者拿到的是一个
本来就稳定的地址，而不是一个被假装成稳定的名字。

## 3. 为什么这不是「第二套授权」

三层，逐层说：

| 问题 | 答 |
|---|---|
| 字节由谁授权？ | `/v1/artifacts/{id}`，按 owner。**没有新增第二个判定点** |
| 谁拥有这些 artifact？ | 写入时记的就是 Task 的 principal（`application/workspace.py` 头段：「Every write records the Task's principal as owner and every read passes it」）。所以取字节这一步本来就只有该 principal 过得去 |
| 公布 `artifact_id` 本身泄露了什么？ | 什么都没有。`tool_gateway.py` 为 `workspace_writes` 写下的论证原样适用：「the principal who made the call can already list the workspace, so recording the name reveals nothing」。一个 id 比一个名字更不敏感——它不可猜，且取它仍要过 owner 那一关 |

换句话说：**授权面的数量没有变（还是一个），寻址方案的数量也没有变（还是 artifact
id）。** 变的只是控制台知不知道那个 id。

## 4. 形状：跟着 ADR-063 与 ADR-086 走

ADR-063 把「这次调用写了哪个文件」从一句英文散文变成了结构化事实
（`ToolResult.workspace_writes`）。ADR-086 对项目那一侧做了同一件事
（`project_writes`），并且**同样明确不做取字节的路由**。

本 ADR 是第三次同一个动作，只是这一次那句「明确不做」可以取消——因为工作集的字节
路由不需要造，它已经存在。

```
ADR-063   名字是事实            workspace_writes:      tuple[WorkspaceName, ...]
ADR-086   另一个 store 同理      project_writes:        tuple[ProjectRelativePath, ...]
ADR-088   名字解析到了什么       workspace_write_refs:  tuple[ArtifactRef, ...]
```

**新增字段而不是加宽 `workspace_writes`**，理由与 ADR-086 当时相同并且更强：
`workspace_writes` 的元素类型是 `WorkspaceName`，把它加宽成「名字或引用」会让每一个
现有消费者都要先判断自己拿到的是哪一种。两个平行字段，各自类型干净。

`ArtifactRef` 自带 `filename`，所以两个字段**不靠下标对齐**——消费者按名字配对，
一个漏发或顺序不同都不会静默错配。

## 5. 代价，以及它落在哪里

每次工作区写入多一次 manifest 读取（`locate`）。manifest 是 JSON、只存引用不存内容
（`MANIFEST_MEDIA_TYPE` 那段注释写着它「small by construction」），而且刚刚才被同一次
写入产生，是热的。

**这是每次写入一次，不是每次渲染一次**——把它放在写入侧而不是读取侧，正是为了让
读取侧一个请求都不用发。

## 6. 被拒绝的方案

| 方案 | 拒绝理由 |
|---|---|
| 给 Task 加 `GET /v1/tasks/{id}/workspace/{name}` 取字节 | F-14 反对的那条，而且反对得对：给一个随版本变的名字发地址。字节的路已经有了 |
| 复用 `/v1/code/sessions/...` | `test_code_has_no_coordination_plane.py` 与 `test_code_premises_are_frozen.py` 把 Code 钉在「无协调面、API 进程内执行」上。把 Task 的产物塞进 Code 的前提里，是拿两个模块的耦合换一个字段 |
| 只加 `GET /v1/tasks/{id}/workspace` 返回绑定表（不返回字节） | 比上面两条都好，而且一度是本 ADR 的方案。放弃它是因为它仍然要新增一条路，而那条路要先知道 `workspace_version`——那个值在 LangGraph 检查点里，不在 task 行上。为一个字段把 API 接到检查点上，比在写入时多读一次 manifest 贵得多 |
| 让子代理把交付物 `export_artifact` 出去 | 那是模型行为，不是读取面。而且 export 要过审批门，把「想看看它写了什么」变成一次需要人批准的动作 |
| 把 `artifact_id` 塞进 `output_preview` 的散文里 | 正是 ADR-063 要终结的东西 |

## 7. 上线顺序：先 API，后 Worker

实测撞到一次，记在这里因为它不属于本 ADR 但由本 ADR 引发：

`ToolCompleted` 与本仓库所有领域模型一样**拒绝未知字段**
（`test_aggregate_rejects_unknown_fields` 钉着这条）。所以一个发布了
`workspace_write_refs` 的 Worker 配上一个还不认识它的 API，那些事件**整条解不开**——
不是少一个字段，是这个事件不进时间线。

好在这件事**不是静默的**：页面照实说了出来，`skippedSequences` 那条路把它渲染成

> 这段历史不完整：上面的步骤中缺了 8 个位置。
> #13：在「工具调用已开始：workspace_write」与「模型调用已开始」之间

八个缺口正好是八个 `ToolCompleted`。重启 API 后全部复原——**事件一直在库里，从来没丢**，
解不开的只是那一次读取。

**因此上线顺序是：先滚 API，再滚 Worker。** 反过来会让一段时间内的工具调用在控制台上
消失（并且明说自己消失了）。这不是本 ADR 新增的性质，是「领域模型拒绝未知字段」这条
既有选择的必然结果——它换来的是漂移会立刻暴露，代价就是这个顺序。

## 8. 不变量

1. **控制台不为工作集文件发第二次鉴权**：字节只经 `/v1/artifacts/{id}`。
2. **一个工作集名字仍然没有公开地址**：发布的是某次写入当时解析到的引用。
3. **`workspace_write_refs` 与 `workspace_writes` 同标准**，都在
   `record_step_inputs` 门之外——它们是事实，不是内容。
4. **失败的写入不发布引用**，与名字同理且同机制：两者都在 `session.version` 前进之后
   才产生，一次被拒的写入根本走不到那一行。
