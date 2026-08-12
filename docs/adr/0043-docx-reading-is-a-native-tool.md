# ADR-043：读 Word 的那半边是本地工具，不是第二个 MCP server

- 决策点：`apps/word_mcp` 把「生成」做完了，「读」没有；补「读」的时候，读取器
  是复制 ADR-027 §3.4 的渲染器形状再起一个 MCP server，还是一个 native 工具；
  唯一那份 docx→Markdown 实现该放在哪一层才不让 Worker 反向依赖 API 应用；给
  模型的文本上限跟谁对齐；ADR-026 §2.4 要求的「保留来源关系」今天存在哪里
- 状态：**接受**。只定形状与第一刀，**不决定入口，也不决定编辑形状**，见 §12、§13
- 日期：2026-08-11
- 影响：`apps/api/docx_preview.py` 整份搬到 `adapters/documents/`，
  `apps/api/routes/artifacts.py:98` 的 preview route 改成从新位置 import，
  **行为逐字节不变并配对照测试**；`tests/api/test_docx_preview.py` 整份跟着搬。
  同批补一批**不是本项目生成的** `.docx` 样本。**零新增配置叶子、零
  `config/ownership.yaml` 变动、配置 schema 保持 `1.14`、零迁移、零新依赖。**
  能力表四处仍然写 Planned，一个字都不改——**第一刀落地之后，仍然没有任何 agent
  能读一份 Word**，理由写在 §10。
- 依赖：[ADR-026](./0026-word-docx-is-an-mcp-artifact.md) §2.2（MCP Server 不接收
  路径 / 租户 / 所有者 / artifact id：本条沿用）与 §2.4（读取必须从已授权
  Artifact 获取、编辑必须读旧写新并「保留来源关系」：本条**订正**后半句，见
  §9）、[ADR-027](./0027-read-outward-write-inward.md) §3.4（渲染器形状可复制：
  本条明确它**不覆盖读取器**）、[ADR-028](./0028-task-workspace.md)（可变名字压
  在不可变字节上，同名再写产生新版本：将来的编辑要用的版本语义）、
  [ADR-036](./0036-triage-decides-the-shape.md) §2.3（往 `TaskInput` 加字段会推翻
  每份已存输入的 fingerprint：入口方案 A3 被挡住的原因，见 §13）

## 1. 背景：这一半不存在，而两个最显眼的默认答案都是错的

### 1.1 生成这一半是完整的，并且真跑通过

`src/agent_workbench/apps/word_mcp/` 的 `contract.py` 是一份封闭有界的结构化
schema，不接受任何路径、租户、所有者字段；`server.py:96` 成功时只返回一个
`EmbeddedResource(BlobResourceContents)`；`adapters/mcp/result_mapping.py:258`
认得 Word 的 media type 并给它 `.docx` 后缀，`:202-210` 用 Worker 自己的
`PrincipalContext` 决定 tenant/owner 后写进 ArtifactStore。这条链上没有缺口。

### 1.2 唯一那份「读」的实现，在 Worker 够不到的地方

全仓只有一处 docx→Markdown：`src/agent_workbench/apps/api/docx_preview.py:198`
的 `extract_docx_preview`，走 body 的 XML 子元素把段落与表格**按文档顺序**抽出来；
`:69` 的 `preflight_docx` 是三道 zip 炸弹闸（条目数 512、展开 100 MiB、压缩比
200x）；`:214` 的 `Document(io.BytesIO(content))` 是全仓唯一一处 docx 解析。

它唯一的调用者是 `apps/api/routes/artifacts.py:98` 的
`GET /v1/artifacts/{id}/preview`。**它是为控制台阅读列写的，不是工具，agent 够
不到。**

### 1.3 真要补「读」的时候，两个错误答案摆在最显眼的位置

- **错误一：照渲染器模板再起一个 MCP server。** ADR-027 §3.4 明写
  「xlsx、pptx、pdf 按同一形状各写一个 server」——它为**渲染器**背书，从没为
  **读取器**背书任何形状。一个只读代码的人会先想到抄它。
- **错误二：让 Worker 直接 `import agent_workbench.apps.api.docx_preview`。**
  机械上能过架构测试（`apps` 与 `adapters` 都是 outer boundary，
  `apps/api/__init__.py` 只有 docstring、不会拖进 FastAPI，且没有 apps→apps 的
  规则），但那是 **Worker 依赖 API 应用，方向是反的**。

这两个错误都只在有人动手那一刻才发生，事后再改就要动已经写好的工具。**所以形状
必须先定，先于入口、先于编辑。**

## 2. 决策：读取器是 native 工具，不是 MCP 工具

判决不是新的，是 `adapters/tools/workspace.py:10-14` 已经给过的那一条，逐字
适用第二次：

> These are native tools rather than an MCP server, and that is a departure from
> ADR-025/026/027 with a specific cause: they touch the artifact store and the
> Task's state, which is Worker-internal authority. ADR-026's rule is that an MCP
> server receives no path and no owner; handing one the artifact store would
> invert it.

推理照搬：**要读一份既有文档，参数里必须出现某种定位符**（artifact id 或工作区
名字）。而给 MCP server 定位符，就是把 artifact store 的权限交出去，正面撞
ADR-026 §2.2（「它不接收输入路径、输出路径、tenant、owner、artifact id 或
shell 参数」）。

工作区工具当年为这件事做过一次判断，读取器是**同一条推理的第二次适用**，不需要
改 ADR-026，也不需要新的理由。

## 3. 被否决的方案

### 3.1 复制渲染器形状，起第二个 MCP server

被否决的理由就是 §2：读取需要定位符，渲染不需要。ADR-027 §3.4 之所以成立，正
因为渲染器**没有输入**——它把结构化 JSON 变成字节，对面不需要知道任何本地事实。
读取器的对面必须知道「读哪一份」，这一点不能靠形状复制绕过去。

### 3.2 规避定位符：把整份 docx 字节当 MCP 入参传过去

这条路在物理上就不成立，写下来免得下一个人再想一遍：

- **MCP 工具的 `arguments` 只能来自模型输出。**Worker 没有往一次 MCP 调用的参数
  里注入字节的通道；要有，那本身就是一条新的授权面。
- **`apps/word_mcp/server.py:31` 的 `MAX_MCP_REQUEST_BYTES` 是 262144。**一份带
  图的真实 Word 文档常常比这个大，而这个上限不是随手设的。

两条一起，把「用字节代替定位符」这条捷径关死。

## 4. 实现搬到 `adapters/documents/`，行为逐字节不变

落点是 `adapters/documents/`：`adapters` 是 outer boundary，允许 import `docx`
（`tests/architecture/test_dependency_boundaries.py` 的 `FORBIDDEN_CORE_IMPORTS`
只管核心层）。Worker 与 API 都能到得了它，而谁都不依赖谁的应用。

- `apps/api/routes/artifacts.py:98` 的 preview route 改成从新位置 import。
  **行为要求逐字节不变**：同一份输入产出同一份 `DocumentPreview`，非 docx 仍然
  415、超 20 MiB 仍然 413、解析不了仍然 422，鉴权仍然 `head` 先判。要**配对照
  测试**，不是靠「只改了 import 所以不会变」这句话。
- `tests/api/test_docx_preview.py`（309 行）整份跟着搬。测试跟着实现走，不留一份
  在 `tests/api/` 下测一个已经不在 `apps/api/` 的东西。

## 5. 全仓保持只有一条 docx 解析路径

`preflight_docx` 的三道闸对**每一个**调用者强制。它今天的 docstring 已经写明这
是为了「so that the ceiling holds for every caller」，搬家不得削弱它。

**任何新入口都不得自己写 `Document(BytesIO(...))`。**今天全仓只有
`docx_preview.py:214` 一处（另有一份未跟踪的 Finder 副本
`apps/api/docx_preview 2.py:129`，被 `.gitignore` 与 conftest 双重挡住，不属于
代码路径）。新开第二条解析路径等于新开一个洞，而症状离原因很远。

## 6. 给模型的文本另立上限，并且超限要说出来

`MAX_PREVIEW_CHARS = 40_000`（`docx_preview.py:37`）是**面板**的数——它的注释
自己写着「this is one panel beside a run」。同类量是
`adapters/tools/workspace.py:55` 的 `MAX_INLINE_READ_CHARS = 48_000`，那才是给
agent 循环定的数。

两者不是一回事，读取器要用自己的那一个。更重要的是**超限的形状**：照
`workspace_read`（`workspace.py:190-198`）的样板——

> `{name} holds {len(content)} bytes, which is too large to show in full. First
> {MAX_INLINE_READ_CHARS} characters:` + 截断内容

——**说出来**，而不是静默截断。一个静默截断的读取器会让模型基于半份文档作答，而
「少了后半段」这件事不在任何一处记录里。

## 7. 读取器必须数出它表达不了的东西

`DocxPreview.table_count`（`docx_preview.py:133-136`）的注释已经是这个思路的
雏形：

> Counted rather than rendered. A reader who sees "3 张表格" knows to open …

**把它扩展**：图片、页眉页脚、编号、脚注各自的计数，进入读取结果。

这一条不是锦上添花。将来若走「重生成」路线（读旧 docx → 文本 → 模型改 → 再调
`render_document`），`contract.py` 的 schema 表达不了的一切都会被丢掉，而这份
计数是唯一能挡住下面这个形状的东西：

> 使用者拿回一份少了一半东西的文档，**而丢了什么不在事件流里、不在 `ToolResult`
> 里、也不在 artifact 元数据里**。

顺带一处已知的表达边界要一并写进结果，而不是留在代码注释里：
`docx_preview.py:118-122` 的 heading 识别只认内置 `Heading 1`..`Heading 9`，
`:155` 另认一个 `Title`；**其余样式一律降级成普通段落**，包括本项目自己的渲染器
为中文毕业论文格式所加的自定义样式。

## 8. 第一刀只做两件

1. **搬家，零行为变化。**§4、§5。
2. **补一批不是本项目生成的 `.docx` 样本。**

第二件是真正的新证据。`tests/api/test_docx_preview.py:1-8` 的 docstring 自己
写着：

> Every fixture here is produced by `render_document` rather than hand-built or
> checked in as bytes.

也就是说，**今天全部 docx 读取证据都是自产自读的闭环**。而 §7 提到的
heading 识别只认两种内置样式——真实 Microsoft Word / WPS / Google Docs 导出的
文件在样式名、编号、run 切分上都不一样。

## 9. 一处对 ADR-026 §2.4 的订正，留痕但本刀不修

ADR-026 §2.4 写着：

> 编辑必须读取旧 Artifact、生成新版本并**保留来源关系**

对着代码核，**这句承诺今天没有地方存**：

- `domain/artifacts.py:49` 的 `ArtifactRef` 有 `artifact_id` / `tenant_id` /
  `kind` / `media_type` / `size_bytes` / `sha256` / `filename`，**没有
  `derived_from`**；
- `domain/workspace.py:253` 的 `WorkspaceManifest` 只有
  `entries: dict[WorkspaceName, ArtifactRef]`，**manifest 之间也没有父指针**。

本 ADR 只记下这件事，并写明结论：**在有人给它一个存放处之前，来源关系由工作区
版本链承担，离开 Task 之后不可追。**这是一句关于今天的诚实陈述，不是一句「未来
会补」。

本条**不给 `ArtifactRef` 加 `derived_from`、不给 `WorkspaceManifest` 加父指针**
——那属于 domain schema 变更，要它自己的一刀。

## 10. 后果

### 10.1 得到的

一条被钉死的形状：**不会再有第二个 MCP server 抄渲染器模板，也不会有 Worker
反向依赖 API 应用。**加上一条 Worker 与 API 共用、只有一个入口、闸门对所有调用
者强制的 docx 解析路径。

### 10.2 代价，写在正面

**第一刀落地之后，仍然没有任何 agent 能读一份 Word。**入口没定，而四条路今天
全是断的：

- `TaskInput`（`domain/task_inputs.py`）没有附件通道——`apps/api/routes/tasks.py:197`
  的 `attachment_names` 注释明写 "Names only... context for the classifier, not
  an upload channel"；
- 摄取路径不认识 docx——`adapters/ingestion/parser.py:50` 的
  `SUPPORTED_MEDIA_TYPES` 只有 `text/plain`、`text/markdown`、`text/x-markdown`、
  `application/pdf`；
- `workspace_read` 对二进制是 `errors="replace"`（`workspace.py:190`）的**静默
  乱码**，不是拒绝；
- `application/workspace.py:137` 的 `write_ref`（「把已经存好的字节绑一个名字」）
  **至今零调用者**。

所以**能力表四处仍然写 Planned，一个字都不改**：`docs/architecture-baseline.md:1549`
（表格行）与 `:1572-1575`（逐条说明现有两样都不是它）、`docs/status.md:481`。
这些地方今天的口径是准的，本条不让它变得不准。

这是 [ADR-040](./0040-a-task-pays-before-it-calls.md) 第一刀（计数器落地但没有
人读）已经用过的形状：**先把东西放到对的位置，再让它有人用**，两件事分两刀。

### 10.3 外部来源样本可能一上来就失败——那是证据不是回归

补进来的 `.docx` 样本如果第一次就有解析不出来的，**不要按回归处理**。它第一次
回答「能读 Word」这句话到底成不成立，而这个答案会改变入口三条路的估值——一个
连真实 Word 导出都读不干净的读取器，配什么入口都没有意义。

## 11. 配置影响：零

- **不新增配置叶子**，不动 `config/ownership.yaml`；
- **不抬 schema 版本**，仍 `1.14`。[ADR-042](./0042-blocking-belongs-to-the-adapter.md)
  §13.2 的判据根本用不上——本条一个叶子都不加，也不动任何 `Literal` 取值域；
- **零数据库迁移**（当前 head 是 `migrations/versions/0025_agent_invocation_count.py`，
  本条不新增 0026）；
- **不引入新依赖层。**`python-docx>=1.2,<2` 已经是主依赖（`pyproject.toml:78`，
  **不在 extra 里**），所以不受「CI 不装 extra」那条规矩影响。这一条写进 ADR，
  免得被下一个人当成新风险重估一次。

## 12. 一处顺带记下但本刀不修

`adapters/tools/workspace.py:538-545` 的 `_SUFFIX_MEDIA_TYPES` 只认识
`.md` / `.txt` / `.json` / `.csv` / `.py` / `.html`——**不认识 `.docx`**。模型
写 `report.docx` 会落成 `text/plain`。

等入口定了、真有 docx 进工作区那天，这条会让**「刚写进去的读不出来」**，而症状
离原因很远。本刀不修，因为今天没有 docx 进得了工作区，修它是为一个不存在的路径
改代码。

## 13. 明确不做

- **不决定入口。**A1（摄取加 docx）、A2（给 `write_ref` 接上调用者）、A3（给
  `TaskInput` 加输入文件字段）三条服务的是**三个不同的用户故事**，工作量差
  5-10 倍。且 A3 被 `application/task_inputs.py:163-167` 引 ADR-036 §2.3 挡着：

  > the artifact's canonical bytes include defaults, so any field added there
  > changes the recomputed fingerprint of every stored input and fails every
  > existing Task's load-time check

  也就是说 A3 不是「加个可选字段」，是额外背一次数据迁移。选哪条见 §14。
- **不决定编辑形状**（C1 重生成 / C2 就地改）。
- **不给 `ArtifactRef` 加 `derived_from`、不给 `WorkspaceManifest` 加父指针**
  ——只记下它们不存在（§9）。
- **不碰 `adapters/ingestion/parser.py:50` 的 `SUPPORTED_MEDIA_TYPES`**，不动
  前端 `accept`（`web/src/features/knowledge/KnowledgePage.tsx:324`），不改
  uploads 的 media type 校验。
- **不新增任何工具绑定进 `TASK_V1_AUTHORIZATION_ENVELOPE`**
  （`bootstrap/projections.py:77`）。搬家不产生新工具，所以信封没有理由变。
- **不修 `_SUFFIX_MEDIA_TYPES`**（§12）。

## 14. 待定：必须由用户拍板，本 ADR 不给答案

以下问题原样记在这里。它们不是实现细节，选法不同会得到不同的东西，**本 ADR
不替任何一个作答**。

1. **本轮的性质。**「开始第 7 条新建能力」要的是（a）截止前多出一件能演示的新
   能力，还是（b）把该关的门关严、把形状定死？本 ADR 偏 (b)：形状 + 一次搬家。
   如果要的是 (a)，四件里唯一能在截止前真正落地、且能诚实写进材料的是入口 A1
   （`parser.py:50` 加一项 + 前端 `accept` 加两项），效果是「上传 Word → 切块 →
   检索 → 带引用被答案引用」，代价是引用只能退回字符偏移——docx 没有稳定页概念，
   `ParsedDocument.page_starts` 对它无意义，**不能假装和 PDF 同级**。要不要把
   本 ADR 改写成直接决定 A1？
2. **入口选哪条。**(A1)「我上传的 Word 资料能被检索和被答案引用」——小，一个
   PR；(A2)「Task 能把下载或工具产出的文档收进工作区继续处理」——中，要定名字
   从哪来、谁授权、失败时版本怎么办（`application/workspace.py:137` 的
   `write_ref` 是现成钩子但零调用者）；(A3)「我提交任务时附一份文档」——大，且
   被 ADR-036 §2.3 挡着（§13）。这三条**没有正确答案，只有想要哪个**。
3. **编辑形状。**能接受「照着旧文档重做一份」吗？走重生成（C1）的话，
   `contract.py` 的 schema 表达不了的一切会被丢掉：图片、页眉页脚、样式、编号、
   脚注、超过 12 节或 30000 字符、超过 20 行的表。如果不能接受，就得做就地改
   （C2，native 工具用 python-docx 只改命中的 run），代价是要重新定义
   「匹配数必须恰好 1」这类判据在 run 被 Word 任意切分的情况下怎么算。

## 15. 什么会让这条决定重来

**有人给来源关系找到了一个存放处。**§9 说「离开 Task 之后不可追」是对今天的
陈述。一旦 `ArtifactRef` 或 manifest 长出父指针，那句话就要重写，而 ADR-026 §2.4
的原话也就重新成立，不再需要本条的订正。

**MCP 侧出现一条 Worker 能注入参数的通道。**§3.2 否掉「字节当入参」靠的是两件
事实：参数只能来自模型输出，以及 262144 的上限。前一件如果变了，那条推理要重
新做一遍——但那本身是一次授权面的扩张，要它自己的 ADR。

**入口定下来之后。**本条只定形状。入口一旦选定（§14 第 2 条），
「读取器读什么、定位符长什么样、要不要走一遍知识库授权」就都有了确定含义，
那时该有一份新 ADR 把它们一起写下来——**特别是**：一个接受 `artifact_id` 的
读取工具能读到「提交者名下的任何 artifact」，`ports/artifact_store.py` 的
`get` 只按 tenant + owner 判，知识库的 `granted_principals` 在这条路上完全不
参与。那是入口 ADR 必须正面回答的问题，不是本条能替它回答的。
