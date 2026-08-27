# ADR-086：产物不该为「它落在哪一侧」负责

- 决策点：2026-08-27 的用户反馈，对 Code 控制台提了五件事——产物生成之后没有直接
  预览；没有「模型自己决定 / 人来决定」的权限设置；思考过程又乱又长；产物在文件夹里
  没有体现；文件夹导航栏没有会话的标志。参照 Codex 的 harness 与 Claude Desktop。
  这五件在代码里不是五处独立的缺失：其中三件同源——**项目目录那一侧是二等公民**，
  它没有查看器、没有结构化的写入事实、因而在树上也不会动
- 状态：**接受**。两件事落地：(1) `ProjectFileBody` 与工作区那一侧共用
  `previewKind` 分派表与查看器组件；(2) `ToolResult` / `ToolCompleted` 新增
  `project_writes`，由 `ProjectWriteTool` / `ProjectEditTool` 发布，控制台据此让目录树
  自己刷新并标出这段会话写过的行。
  第三件——权限轴——原本也在这一号里，已拆出为
  [ADR-087](./0087-a-session-may-be-stricter-than-its-deployment.md)：它收紧的是信封
  的另一半，是另一条边界。
  **明确不做**：不给项目文件加取字节的路由（因而图片／PDF 仍看不了）、不给项目写入做
  产物卡片、不动 `config_schema_version`
- 日期：2026-08-27
- 影响：`domain/project_files.py`（`ProjectRelativePath`）；`domain/tools.py`、
  `domain/events.py`、`runtime/tool_gateway.py`（`project_writes` 直通）；
  `adapters/tools/project_files.py`（两处发布）；前端 `ProjectFileTree` /
  `PreviewPanel` / `CodePage` / `turnBlocks`

---

## 1. 头条：五件事里有三件是同一件

用户提的五件事，读起来像五个独立的功能请求。逐条去看代码，其中三条落在同一处：

| 反馈 | 表面症状 | 实际位置 |
|---|---|---|
| 产物没有直接预览 | 点开 `report.html` 只有源码 | `ProjectFileBody` 是一个 `<pre>` |
| 产物没在文件夹里体现 | 刚写的文件树上不出现 | 没有结构化的写入事实可订阅 |
| 导航栏没有会话标志 | 树上看不出哪些是这次改的 | 同上 |

而工作区那一侧，这三件事**都是做过的**：`FilePreview` 有 html／image／pdf／text 四
种查看器和沙箱 iframe（ADR-062、ADR-065），`ToolCompleted.workspace_writes` 是
ADR-063 专门为「哪个文件是这一步产出的」加的结构化字段，`CodeTurn` 因此画得出产物
卡片。

所以缺的不是四个功能，是**一侧的补齐**。而这一侧偏偏是默认那一侧：ADR-072／074
之后，`config.demo-local.toml` 下每一段会话都有项目目录，`CODE_TOOLS` 与
`CODE_PROJECT_TOOLS` 互斥（`code_session.py` 明写为不变量），于是**握着项目工具的
回合写出来的每一个产物，都落在没有查看器、没有事实、树上不会动的那一侧**。

用户看到的那五句话，是这一条分界线在界面上的五个投影。

## 2. 为什么 `project_writes` 是新字段而不是更宽的 `workspace_writes`

显然的省事做法是让 `workspace_writes` 也装项目路径。**否掉，而且理由是类型不是口味。**

`WorkspaceName` 的正则明确排除 `/` 和 `\`，`domain/workspace.py` 写着为什么——
*a client-supplied path is exactly how path traversal and cross-tenant reads enter a
system*。扁平侧买到的那条性质，是靠「路径根本拼不出来」买的。为了让一个字段兼容两
种存储而把这个类型放宽，是**为所有调用方**删掉一条性质，去服务一个并不需要它的侧。

两个字段还有第二个好处，它在读的时候才显出来：`report.html` 在工作区里和
`docs/report.html` 在项目里，是两个不同的端点取、两个不同的组件画。一个合并的列表
会让「这是哪一个」变成从有没有分隔符去猜——而写在项目根下的文件同样没有分隔符。

## 3. 为什么发布的是 `entry.path` 而不是模型给的参数

`ProjectWriteTool` 拿到的 `path` 是模型写的，`entry.path` 是 store 归一化之后的。发布
后者：控制台拿这个值去 `["project-files", …]` 失效、去树上标记、（将来）去打开预览，
而这三件事都要它是目录里真实的那个名字。发布前者，等于让界面按一个目录不使用的拼法
去要一个文件。

同一句话的另一半在 `turnBlocks.ts` 里已经写过一遍，那是 ADR-063 的措辞：**一个解析
散文的界面，会在有人改进措辞的那天坏掉，而测试套件里没有任何东西会注意到。**
`ProjectWriteTool` 此前唯一说得出写了什么的地方，是
`f"Wrote {entry.path} ({entry.size_bytes} bytes)."` ——一句没有任何测试钉住的英文。

## 4. 明确不做

- **不给项目文件加取字节的路由。** 因而图片和 PDF 在项目侧仍然只有一句话，记进
  `known-gaps.md` F-27。
  **不做的理由不是「agent 放不进二进制」。** 这一句在初稿里写过，它是错的：
  `project_write` 的入参确实是 `str`，但 ADR-077 之后回合还握着 `project_run`，
  而一条命令能在项目目录里写出任何东西——两个 local profile 都打开了
  `policy.shell_tools_enabled`。所以项目侧的二进制**可以**是产物。真实的理由只是
  次序：文本产物（`.md` / `.html` / 源码）是绝大多数，它们一行后端代码都不用改就能
  看。这是一次排期，不是一次判定。

  > **2026-08-27 补记，两处更正。** 这一条**已在同一批里做掉**（F-27 已关闭）：
  > `ProjectFileStore.open_bytes` + `GET /v1/projects/{id}/file/bytes` +
  > 前端接 `BlobPreview`。
  >
  > 而上面这段原本还写着「要动 `tests/contracts/` 的参数化套件」——那也是错的。
  > `tests/contracts/test_projects.py` 是 `ProjectStore`（归属与成员关系）的套件；
  > `ProjectFileStore` 只有一个实现，测试在
  > `tests/adapters/test_project_file_store.py`。**这条排期是按一个比真实成本高的
  > 估计做的**，两句话都留在这里而不是改掉，因为「当时凭什么这么判断」和「后来发现
  > 判断依据不对」是两件都该看得见的事。
- **不给项目写入做产物卡片。** `FileCard` 的每一个能力都绑在 `WorkspaceEntryView`
  上（media type、大小、能不能运行），项目侧没有这个对象，而伪造一个只有名字的会让
  卡片上一半的东西说不出来。树上现在会动、会标记、点得开，这条路是完整的；卡片是
  第二条路，值得单独一次决定。
- **不动 `config_schema_version`。** 这次没有新配置字段：`project_writes` 是事件上的
  一个可选字段，默认空元组，旧事件读得出来，新读者读不到时也不会崩。
- **不宣称「目录树是活的」。** 它跟的是**记账过的**写入，而 `project_run` 能改根下
  任何东西且不经过任何记账工具（ADR-078 §3 明写这是代价不是缺陷，known-gaps F-25）；
  控制台自己的 `PUT /v1/projects/{id}/file`、以及读者自己的编辑器和 `git` 同样绕过。
  界面因此只说「这段会话写过它」——一句它答得出的话——而不说「这是目录当前的样子」。
