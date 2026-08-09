# ADR-027：只读取外部世界，写只写进自己的 artifact

- 决策点：Task 能不能自己上网取页面、下载文件、生成 Office 文档；这些能力的共同边界是什么
- 状态：**接受**
- 日期：2026-08-09
- 影响：`researcher_external` 获得动态 MCP 工具源；新增 `fetch_page` / `download_document`
  两个只读工具；渲染器类 MCP server 确立为一种可复制的形状；SSRF 防护从字面地址
  升级为解析后校验。`SUPPORTED_KEYWORDS`、`CANONICAL_V1_NODE_IDS`、ADR-009 **均不变**

## 1. 背景

ADR-025 落地后，第一个真实问题是"能不能再多给 Task 一点手脚"。提出来的清单是三件：
上网查东西、下载文件、生成文档并操作常见软件。

它们看起来是三个功能，实际上只有一条分界线，而这条线决定了它们值不值得做：

| | 改变对面的状态吗 | 能否安全重放 |
|---|---|---|
| 取一个页面的文本 | 否 | 能 |
| 下载一个文件 | 否 | 能，且 artifact store 是内容寻址的 |
| 把结构化内容渲染成 .docx | 否，纯函数 | 能 |
| **填表、点提交、下单** | **是** | **不能** |

前三行在一条规则下：**只读取外部世界，写只写进自己的 artifact store**。它们全部满足
ADR-025 §2.7 对 `retryable_effects=true` 的要求，因此一条既有不变量都不用动——不需要
mid-loop resume，不需要 `agent_run_snapshots`，不需要第二种执行形态，ADR-009 不必重审。

第四行不在本 ADR 内。它需要的东西在别处论证过，代价也大得多；把它和前三行混在一个
决定里，会让本来免费的三件事背上它的包袱。

## 2. 已经有的部分，比看上去多

`external_search`（ADR-020）不是"拿搜索结果的标题"。它自己发 HTTP GET 抓页面、用
`page_text` 抽正文、再让模型压缩它**真正被展示过**的文本。适配器的注释记录了为什么：
不这么做时，同一个天气问题三次跑出 9-20°C、3°C 和 36°C——"从标题编出来的数字不是证据"。

所以"上网查资料"这件事已经成立。缺的是三处具体的：

1. **按名字取任意 URL**。今天能抓的 URL 只来自搜索结果；模型自己想读的一篇文档读不了；
2. **下载非 HTML 的字节**。PDF、xlsx 现在会被当成 HTML 抽成一团乱码文本，而不是存成 artifact；
3. **JS 渲染的页面**。`httpx` 拿到的是原始 HTML，SPA 的内容不在里面。

## 3. 决策

### 3.1 两个只读工具，走 MCP，不新增原生工具

`fetch_page(url)` 与 `download_document(url)` 作为一个项目自有的 MCP server 提供，
形状与 `apps/word_mcp` 相同：无状态、不接受路径或所有者字段、返回内容或字节。

不做成原生工具，理由是 MCP 适配器已经把命名、schema 校验、授权信封、事件流和 artifact
落地全部走通了（ADR-025），再开一条原生路径等于维护两套。

- `fetch_page` 返回抽好的正文，复用现有 `page_text`，不返回原始 HTML；
- `download_document` 把字节交给 MCP 结果映射落进 artifact store，`media_type` 走
  ADR-025 §2.8 既有的合法性校验与回退；
- 两者都是 GET，都不带 `operation_key`，`retryable_effects=true`。

### 3.2 SSRF 防护必须从字面地址升级为解析后校验

这是本 ADR 唯一一处**必须先修才能开工**的地方。

今天的 `_LOCAL_HOSTS` 只比对字面主机名，它自己的注释写明了缺口：

> Literal-address checking only -- a name that *resolves* to a private address is
> not caught here, and closing that needs resolution before connect.

在今天这个缺口是可以接受的：URL 来自搜索引擎的结果，攻击者要先污染索引。**一旦允许
模型自己命名 URL，这个前提就没了**——而模型的输入里有检索到的网页文本，那是不受信任
的内容，prompt injection 可以直接让它去取一个指向内网的地址。

所以 `fetch_page` / `download_document` 落地前，取页面这条路径必须：解析主机名，拒绝
解析结果落在回环、链路本地、私有网段或云元数据地址的请求，并且**在连接前**完成判断。
既有的 `external_search` 抓取路径共用同一份检查。

### 3.3 只给 `researcher_external`，不给 writer

ADR-025 把动态 MCP 源给了 `writer/synthesize`，因为当时接的是文档渲染——那属于写作。
上网取材属于研究，所以本 ADR 把 `dynamic_tool_sources={"mcp"}` 加给
`researcher_external`。

两个 researcher 仍然互不可见对方的发现（`admits` 不变），fan-out 的独立性不受影响。
`framer`、`planner`、`critic` 依旧没有任何工具。

工具名到 profile 的分配由部署配置决定而非硬编码：一个 server 的工具进哪个 profile，
读它在 `[[mcp.servers]]` 里声明的用途。这样再加渲染器时不必改 profile 代码。

### 3.4 "操控基本的软件" 的准确含义是渲染器形状的 MCP server

`apps/word_mcp` 已经确立了这个形状，并且实测跑通：无路径、无租户字段，接受有界结构化
内容，返回一个内嵌文档。**它不驱动桌面版 Microsoft Word 的界面**，而是直接产出 OOXML。

这就是本 ADR 认可的"操控软件"：xlsx、pptx、pdf 按同一形状各写一个 server，共用同一套
契约、同一个闸门、同一条 artifact 通道。

区别不是文字游戏。驱动一个 GUI 会立刻越过 §1 那条线——窗口状态、剪贴板、文件系统都是
对面的状态，且不可重放。那是另一个决定。

### 3.5 JS 渲染与截图不在 v1

需要真实浏览器内核，带来新的依赖、新的进程生命周期和新的威胁模型。本 ADR 不引入，
理由与 ADR-025 拒绝 stdio 相同：它不该伪装成一个配置项顺手打开。

后果要写明：**SPA 页面在本版取不到正文**，这不是 bug，是已知边界。

## 4. 后果

- Task 能读它自己指定的页面、把文件收进 artifact store、产出 Office 文档，全部在无人
  值守下运行，且崩溃恢复语义与今天完全一致——因为没有一件事改变了对面的状态；
- 未配置 MCP 的部署行为与信封逐字节不变；
- SSRF 检查加强会让 `external_search` 也更严：一个解析到私有地址的搜索结果会被拒绝抓取。
  这是收紧，可能让极少数结果取不到，接受；
- 模型能命名 URL 意味着**检索到的网页文本可以影响它去取什么**。§3.2 的网络层防护限制
  的是"能到达哪里"，不解决"被诱导去读什么"。后者由授权信封与最终 HITL 兜底，本版
  不声称能判断远端文本的语义真实性——这一条与 ADR-025 §4 的口径一致；
- **不解决**：填表、点击、下单、驱动桌面软件、JS 页面、截图。

## 5. 备选方案

**直接接一个通用浏览器 MCP server（Playwright 之类）。** 它同时带来读和写两半，而写的
那半按 ADR-025 §2.7 根本进不了 Task 图（`retryable_effects=false` 的 server 被整个跳过）。
结果是引入了一个大依赖，却只用得上它的只读部分。

**把 `fetch_page` 做成原生工具而不是 MCP。** 省一个进程，代价是绕开刚刚建好的适配器，
让"工具从哪来"有两个答案。

**放宽 `retryable_effects` 让写操作也进来。** 那是另一条路线的第一步，不该由一个"想加
个取网页功能"的 ADR 顺手带过。
