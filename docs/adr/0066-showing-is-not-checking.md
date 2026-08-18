# ADR-066：展示不是验收

- 决策点：控制台判断一个产物该怎么显示，用的是 `previewKind` 这一个五值词表；
  但界面真正需要回答的还有第二个问题——**读者要花多大代价才能确认这个产物是对的**。
  这两个问题该合在一个词表里、该拆成两个正交的词表、还是该由每个界面各自推导
- 状态：**接受**。`previewKind` 一个字不动，只回答「用哪个 DOM 元素显示这批字节」；
  新增正交的 `checkCost(mediaType, name, {canRun, canConvert, showsPdfInline})`，
  五值 `free / reader / one-action / elsewhere / unchecked`。
  ADR-062 的「打开即渲染」与 ADR-065 的「跑是点出来的」不是两套机制的不一致，
  是**同一条代价轴上的两档**：客户端代价可以替读者花，服务端代价留给一次点击。
  「这里验不了」成为一等分支，有两个发现时机，且第二个刻意不进纯函数
- 日期：2026-08-18
- 影响：`web/src/components/media.ts`（`CheckCost` / `checkCost` /
  `SurfaceAbilities` / `effectiveMediaType`）、新
  `web/src/features/code/FileCard.tsx`（从 `CodeTurn` 抽出，preview 用 render prop）、
  `PythonPreview`（`renderWritten` / 退出码那行 / `containerLimitNote` 四类 /
  `remedyFor` 三分支）、`FilePreview`（`producedCards` / 两句拆分）、
  `HtmlPreview`（提示移到 iframe 之上）、`CodePage`（`displayable`）、
  `WorkPage`（`RunFailure` 抽出、文本臂分流、步骤流闸门删除）、
  `workTimeline.ts`（`findDeliverable` 三判据、`collectArtifacts` 读 `output_ref`）、
  `ChatPage`（完整 chunk_id、删空 quote 分支、`CopyAnswer`）；
  后端新 `adapters/tools/media_guess.py`（合并两张猜型表），
  `adapters/tools/{workspace,sandbox}.py`、`apps/api/routes/code.py`、
  `workflows/task_handlers.py`
- 依赖：[ADR-062](./0062-a-produced-page-runs-in-an-empty-origin.md)（产出的页面在空 origin
  里运行——本 ADR 把它的「自动」与 ADR-065 的「手动」统一成一条规则）、
  [ADR-065](./0065-a-file-a-person-can-see-is-a-file-they-can-run.md)（§4 拒绝了第六个
  `PreviewKind`——本 ADR 不推翻它，把它推广成一条规则）、
  [ADR-029](./0029-ephemeral-sandbox.md)（沙箱是纯函数：`--network=none`、
  无 tty、一次性容器——`containerLimitNote` 的四类全部由它推出）、
  [ADR-063](./0063-a-produced-name-is-a-fact-not-a-sentence.md)（产出的文件名是结构化
  事实——本 ADR 的运行产出卡片建立在它之上）、
  [ADR-045](./0045-a-layout-is-a-conversion-not-a-third-parser.md)（版面只按 artifact
  寻址——`elsewhere` 这个值存在的原因）

## 1. 背景：一个词表在回答两个问题

`previewKind`（`web/src/components/media.ts`）的 docstring 记着它消灭的东西：一对
布尔值 `readable` / `isDocument`，它们的 false 相乘，让每个显示文件的界面各自推导
「那图片怎么办」，而各自得出的答案都是「静默下载」——于是点开一张产出的 `.png`
存下了一个文件而不是显示一张图。用一个封闭词表换掉那对布尔，是对的。

问题是**同一个错误在更高一层又长了出来**。今天有四处各自持有一个私有布尔，回答的
都是「读者能不能确认这个文件是对的」，而四个 false 各指一件事：

| 位置 | 它私下回答的 | false 意味着 |
| --- | --- | --- |
| `isRunnablePython`（`media.ts:66`） | 一个 `.py` 靠跑来验 | 不是脚本 |
| `HtmlPreview` 的 渲染/源码 | 一个页面靠画来验 | —— |
| `WorkPage` 的 `textFor` | 一个 `.docx` 靠转版面来验 | 读者切回了文字 |
| `browserShowsPdfInline`（`media.ts:124`） | 这个浏览器画不画 PDF | **根本验不了** |

四个布尔，没有共同词表，于是**没有任何界面说得出那句最该说的话：这里没有任何动作
能验证这个文件。** 一个 `.xlsx` 的卡片停在它的名字上——没有折叠块，没有句子，
什么都没有——而「没有查看器」和「卡片还在加载」在读者眼里长得一模一样。

### 这是两个问题的证据，是一次实测的失败

一个脚本退出码 0，stdout 打印「已生成」，它画出来的图里每一个中文标注都是空心
方框——matplotlib 默认的 DejaVu Sans 没有汉字字形。

退出码说成功。stdout 说成功。stderr 是空的。`previewKind` 说 `image`。
**只有看那张图**才说得出别的。在此之前，控制台里没有任何东西区分「显示了」和
「验收了」。

### 而「展示」这一步本身也是断的

顺着这条线往下查，发现产出到达读者的那条路在两处被切断，且两处都不是 UI 的问题：

**一、一次运行写出的文件不会出现在视野里。** `FilePreview` 的 `onRan` 只做了
`refreshWorkspace` + 缓存失效——脚本画出来的 `chart.png` 只是让面板底部那个折叠的
「工作区全部文件」多一行。**点一下运行，要再点两下才能看到这一下的产出。**

**二、一个文件能不能被看见，取决于是谁写的它。** 后端有两张互不相识的后缀猜型表：
`adapters/tools/workspace.py` 七条、兜底 `text/plain`；`adapters/tools/sandbox.py`
九条、兜底 `application/octet-stream`。两张表**都没有** `.jpg` / `.jpeg` / `.gif` /
`.webp`。所以：

```
savefig("chart.png")   →  image/png                 →  控制台显示
savefig("chart.jpg")   →  application/octet-stream  →  只能下载
```

同一张图，同一个脚本，差别只有点号后面那三个字母。加上上传走浏览器的
`content-type`（`routes/code.py`）、MCP 产物走对面声明的类型，一共**四个来源**——
而 media type 是**写入时冻结进 manifest 的**（`ArtifactRef` 不可变，`ArtifactStore`
端口既无 update 也无 delete），补表不追溯。

## 2. 决定：两个正交的词表，不是六个值

### 2.1 `previewKind` 不动

五值、判定顺序（`html` 必须排在 readable 之前）、`isPreviewable`、`isReadableMedia`
全部不变，所有既有调用点行为不变。它从此**只**回答一个问题：用哪个 DOM 元素显示
这批字节。

### 2.2 `checkCost` 是第二个词表

```ts
checkCost(mediaType, name, { canRun, canConvert, showsPdfInline? }): CheckCost
```

| 值 | 含义 |
| --- | --- |
| `free` | 一次绘制或一次解码就是答案。展示即验收 |
| `reader` | 字节已经全在屏幕上，对不对只有人知道 |
| `one-action` | 再花一次动作能验，且这个界面提供得了 |
| `elsewhere` | 再花一次动作能验，但不在这里——该告诉读者去哪 |
| `unchecked` | 这个控制台没有能验它的动作。说出来 |

### 2.3 第三个参数是能力对象，不是界面名字

这是本 ADR 唯一必须逐字讲清的类型选择。ADR-065 §4 拒绝第六个 `PreviewKind` 的
理由是：那个枚举被**没有工作区的界面**共用，加进去等于逼每个界面回答一个只有其中
一个能回答的问题。这条论证今天完全成立。

把第三参数写成 `"code" | "work" | "chat"` 这样的界面联合类型，是把同一个耦合搬到
一个模块之外：共享层重新知道一共有几个界面、各自叫什么，加第四个界面就要改这个
文件。三个布尔不会。

三个界面各传什么：

- Code：`{ canRun: true, canConvert: false }`
- Work：`{ canRun: false, canConvert: true }`
- Chat：`{ canRun: false, canConvert: false }`

**副产品**：F-11（Code 的 `.docx` 没版面）与 ADR-065 留下的不对称（Work 的 `.py`
不能跑），从两处互不相识的散落注释，变成同一张表里对称的两个 `false`。

`checkCost` **不上服务端**：它依赖界面能力，而服务端不知道请求来自哪个界面，知道了
也不该按界面分叉。服务端那一半（`routes/code.py` 对 `.py` 的两条独立判据）继续自己
判一次——客户端的判断从来是交互提示，不是授权。

### 2.4 时机规则：ADR-062 与 ADR-065 是同一条规则的两档

> 服务端代价在「不花它就什么都看不见」时随打开一起花掉；在「不花它也看得见东西」
> 时留给一次明确的点击。客户端代价（一次绘制、一次解码、上限内的一次传输）永远
> 可以替读者花掉。

- `.docx` 不转换就什么都看不见 → 打开即转换（`WorkPage` 今天就这么做，现在它有了理由）
- `.py` 不运行也看得见源码 → 留给一次点击。ADR-065 §4 的「跑是点出来的」从此是这条
  规则的**推论**，不是一条特例
- html / image / text 的代价在客户端 → 自动

**机制永远不会统一，这一点写死**：HTML 帧不需要新鉴权（字节已经在客户端），沙箱运行
需要 `sandbox:run` 加会话所有权。任何把两者做成一个 `run(artifact)` 端点的方案都要把
这两个授权面焊在一起。**统一的是时机规则，不是机制。**

### 2.5 折叠不由 `checkCost` 决定

`CodeTurn` 的 `lastPreviewable` 继续用 `previewKind` + 字节上限判定，**不**改用
`checkCost`。这一条被写下来，因为它看起来像本 ADR 的自然推论，而它错两次：

- 一个 `.py` 是 `one-action`，改用代价判定会让控制台不再自动展示编码会话最常产出的
  那类文件的源码——把「代码也是产出，它该在对话里」这个决定在一个版本之后撤销掉；
- `free` 若因此不受字节上限约束，一个 8 MB 的页面会被自动拉取，理由是「画它便宜」。

折叠回答的是「展示这件事贵不贵」，那是关于查看器和传输的问题；代价词表回答的是
「展示之后事情有没有定」。两者不该互相决定。

### 2.6 「验不了」有两个发现时机，第二个不进纯函数

- **点击前**，从媒体类型知道：`checkCost` 返回 `unchecked`
- **运行后**，从 CPython 自己的字符串知道：`containerLimitNote`

第二个时机**刻意不做成 `checkCost` 的返回值**。`checkCost` 是媒体类型与界面能力的
纯函数，而「跑了，但失败于容器的形状而不是代码」只有一次运行的 stderr 才知道；
把它塞进返回值，结果只能是在每个调用点被第二次推导出来——正是 `previewKind` 的
docstring 记着的那个病。

`containerLimitNote` 从两类扩到四类，四类全部由 ADR-029 的容器形状推出：

| 类 | 判据来源 | 为什么必须说 |
| --- | --- | --- |
| 终端 | `_curses.error` / `setupterm` / `termios.error` / `Inappropriate ioctl` | 一个写得完全正确的 curses 程序在这里必然失败 |
| 键盘 | `EOFError: EOF when reading a line` | traceback 指着读者自己那一行，看起来像他的 bug |
| 网络 | `socket.gaierror` / `URLError` / `Name or service not known` | 最容易被读成「网络不稳」，而读者会一直重试 |
| 原生窗口 | `_tkinter.TclError` / `no display name` / `pygame.error` | 容器没有显示器，面板是浏览器，两边都放不下 |

**匹配的是 CPython 标准库的字符串，不是本项目写的句子。** 这条界线是 ADR-063 的
镜像面：那份 ADR 拒绝解析 `output_preview`，因为那是本项目写的、没有测试钉住措辞的
三句英文散文；这里匹配的是标准库和 C 库的 errno 文本。漏判也是安全的——traceback
照样渲染，未识别的失败退化成改动前的行为。

### 2.7 退出码不是判决

`PythonPreview` 首行改成「运行结束，退出码 N · 写出 M 个文件」，**任何情况下不出现
「成功」「验收通过」**。产出卡片下面跟一句：

> 退出码只说明它跑完了，没说明它写对了——上面这些文件要自己看过才算数。

这是 §1 那个字体 bug 的直接产物。运行面板能说的最强的真话是否定式的。

### 2.8 验收不是一个被记录的状态

**刻意不做**：任何形式的「已验收」标记，无论是否持久化。三条理由：

1. 它是逐读者逐会话的状态，事件流里没有任何东西能作为它的事实源；
2. 持久化要一份 ownership 变更 + 一次 schema 变动 + 一次迁移；
3. 最要命的是它会把「有人点过」写成「已验收」，而那正是 `docs/status.md` 那条能力
   阶梯禁止的无证据声明。

更根本的一条：一旦要记录，就得回答「验收的是哪一份字节」——而工作区是名字可变、
字节不断被覆盖的（F-13），「那一轮的字节」等价于一个工作区版本 id，等价于
`tests/architecture/test_a_workspace_version_is_never_asked_for.py` 禁止的东西。

**验收是读者做的一件事，不是系统存的一个状态。** 这份重设计能做的全部，是让不花
代价的自动发生、让花代价的落在一次点击旁边、并且在验不了的时候把话说出来。

## 3. 到达：产出必须进入视野

### 3.1 运行写出的文件变成卡片

`PythonPreview` 新增 `renderWritten` render prop，`FilePreview` 用它把
`RunFileResponse.written` 渲染成与轮次同构的 `FileCard`。图片是 `free`，卡片自己
展开——**跑出一张 `plot.png` 从两次点击降到零次**。

两道闸门：

- **时序退化**：运行的响应与工作区 listing 是两条异步路径，所以只有当 listing 能
  解析出**每一个**名字时才渲染卡片，否则退回今天的纯文本句子。全有或全无，因为
  一个列表里两个是卡片、一个不是，读者会读成第三个失败了。
- **循环闸门**：运行结果里的卡片传 `{ canRun: false }`，一个脚本刚写出的 `.py` 不会
  在这次运行的输出里再长出一个「运行」按钮。一次点击必须等于一个容器。

`FileCard` 为此从 `CodeTurn` 抽成独立模块，preview 主体用 render prop 传入——
`FilePreview` 要渲染卡片、卡片要渲染 `FilePreview`，直接互相 import 是模块环。
这与 `PythonPreview` 早就在用的 `source` 是同一个缝。

### 3.2 一张表，一个兜底函数

后端两张后缀表合并进 `adapters/tools/media_guess.py`，补上 `.jpg` `.jpeg` `.gif`
`.webp` 等，并把兜底改成**字节的函数而不是调用方的函数**：前 8 KiB 无 NUL 且能按
UTF-8 解码 → `text/plain`，否则 → `application/octet-stream`。

这是合并唯一可接受的形状。两个旧兜底**各自对自己的调用方都是对的**：
`workspace_write` 的 content 来自 JSON 字符串、必然是 UTF-8 文本；沙箱输出是 base64
解出来的任意字节。问字节，两个调用方都拿回自己原来的答案，而谁都不必知道自己是谁。

**一刀切成 `text/plain` 被拒绝**：一个脚本用 openpyxl 写的 `.xlsx`、用 zipfile 写的
`.zip`，会从「只能下载」（诚实）变成被截成 512 KiB 塞进 `<pre>` 显示成乱码——把
「我不知道」改成一句确信的假话，而这个值是要写进 manifest 且改不了的。

### 3.3 界面侧对 octet-stream 按名字再问一次

`effectiveMediaType(mediaType, name)`：只有 `application/octet-stream` 和空串——
两个「没人知道」的值——才查名字。声明过 `text/plain` 的文件保持 `text/plain`，
哪怕名字以 `.png` 结尾：一个写入方说了话，一张后缀表没有资格推翻它。

修的是这个：读者往编码会话上传一个 `notes.md`，浏览器对不认识的后缀发空的
`file.type`，上传路由诚实记下 `application/octet-stream`，于是读者自己的笔记在
控制台里成了一个打不开的二进制块。

**刻意不改上传路由去猜后缀**：`routes/code.py` 的立场（上传用请求头、缺省诚实地
不知道）与 `workspace_write` 拒二进制包格式的不对称是被写下来过的有意设计。同样的
读者收益由界面侧拿到，且不动那条边界。

## 4. 被拒绝的方案

| 方案 | 为什么否 |
| --- | --- |
| 给 `previewKind` 加第六个值 | ADR-065 §4 拒过一次，理由今天依然成立。本 ADR 不绕过它，是把它推广成一条规则：凡是与界面能力相关的问题都去 `checkCost` |
| 第三参数写成界面名字的联合类型 | 把 ADR-065 §4 反对的耦合原样搬进新词表（见 §2.3） |
| 折叠改由 `checkCost` 判定 | 两处都错（见 §2.5） |
| 运行时的「验不了」做成 `checkCost` 的返回值 | 纯函数不知道容器说了什么，结果只能在调用点被二次推导（见 §2.6） |
| 统一的 `POST /v1/run`，或把 artifact 当脚本送进沙箱 | ADR-029 §3 + ADR-065 §5。读者选「哪个文件跑」而不是「跑什么代码」，攻击面必须继续与「读者能不能往自己工作区写文件」重合 |
| 让前端用运行前后的 listing 差集推断这次写了什么 | 把一个事实变成一个推断：并发的另一个标签页写的文件会被算进这次运行，而这类错误没有任何测试会响。与 ADR-063 拒绝解析散文同源 |
| 给读者发起的运行开事件流 / 新事件类型 | ADR-065 §6 拒过。更根本的是归属：轮次是 Agent 执行的记录，读者点一下按钮往里塞一条 `ToolCompleted`，是让读者的动作伪装成 Agent 的动作 |
| 把读者跑出来的文件挂进当前最后一轮 | 那个列表的无障碍名是「这一轮产出的文件」，而这些块刷新之后就消失——同一轮在刷新前后产出列表不一样，读者会以为 Agent 做过一件它没做过的事 |
| 合并猜型表后兜底一刀切成 `text/plain` | 把「诚实地不知道」改成一句假话（见 §3.2） |
| 把猜型表做成配置的一个叶子 | `tests/architecture/test_config_ownership.py`：一个叶子要一份 ownership 变更 + 一次 schema 抬版 + 一次迁移。一张后缀表是代码里的事实，不是部署要调的旋钮 |
| 给上传路由加后缀兜底 | 推翻 `routes/code.py` 在案的立场，而同样的收益界面侧拿得到（见 §3.3） |
| HTML 卡片默认开在源码视图 | 纯回退：今天 0 点击的 HTML 预览会变成 2 次点击，且与「`free` 可以自动」自相矛盾。改的是提示的位置，不是渲染的时机 |
| 让 `.py` 的卡片滚进视野时自动运行 | 新模型最诱人的误用。时机规则就是为拦它写的：一次绘制可以替读者花，一次服务端容器启动不行 |
| 持久化「已验收」标记 | 见 §2.8 |
| 给 Chat 一个下载按钮或 artifact 容器 | 会把 Chat 推成第三套产物机制（容器、寻址、生命周期、GC），而它真正缺的东西比那小得多。「复制答案与引用」拿到了读者要的那件事且不产生需要归属的字节 |
| `findDeliverable` 只认 `export`/`render` 节点的白名单 | `v2_general` 的渲染发生在 `work` 节点，白名单会把 Word Task 的 `.docx` 排除出头条——正是 `findDeliverable` 当初修掉的那个 bug。改用排除资料获取节点的黑名单 |
| 给 MCP 结果换新的 `ArtifactKind` 来分辨自产与取回 | 所有 MCP 工具共用一行 `put(kind="tool_result")`，word 渲染的 `.docx` 与 web 抓的 PDF 走同一行；换它会把 Word Task 的头条一起打掉。而且 `ArtifactKind` 是封闭 Literal，加值是一次 domain 变更 |

## 5. 代价与刻意不做的

1. **F-13 一步没动，而且更显眼。** 卡片预览的仍然是名字此刻的字节。新模型只是逼
   「第 N 轮又改过，预览的是最新内容」这句话出现在卡片上，而不是只躺在 known-gaps 里。
2. **F-11 只是从沉默变成一句话。** Code 的 `.docx` 依然既看不了版面也看不了文字，
   读者现在只是知道去哪能看。
3. **F-12 原样不动，并新增一条要写进正文的残余**：保留 HTML 卡片自动展开，意味着
   出网风险在读者滚到那一轮时自动付掉，不是点出来的。提示上移只是让它在读者读那
   一段**之前**被读到，不是撤回一次已经发生的加载。
4. **运行时的「验不了」是字符串匹配，必然漏。** 四类之外的容器限制会退化成一整块
   红色 traceback。这是可接受的失败方向——漏判等于回到改动前——但它是漏判而不是
   完备判断，不该被说成别的。
5. **运行产出的卡片对 listing 时序有可见的不一致。** 名字有时是卡片、有时是纯文本，
   取决于 listing 刷新回来没有。全有或全无兜住了误导，但不一致是真实的。
6. **兜底改成字节嗅探是一次行为变更。** 一个真正的二进制产物如果碰巧前 8 KiB 无 NUL
   且是合法 UTF-8，会被当文本显示成乱码。今天的失败方向相反（ADR-062 §2 记的那次
   真实故障），且这一条有测试。
7. **`.xlsx` / `.pptx` 仍然没有查看器。** 它们只是从「顶到头条然后说自己只能下载」
   退回侧栏。`DOCUMENT_MEDIA_TYPES` 那句「为部署新增渲染器预留」的注释一并作废——
   预留一个位置给一个不存在的能力，恰恰是这次要修的病。这改变了一条为未来预留的行为。
8. **有意的契约变更，各自记账**：Code 的「这个类型只能下载。」拆成两句；步骤流的
   「打开产物」不再静默下载；`findDeliverable` 的三判据。三条都有测试跟着改。
9. **运行路径仍然没有自动化的端到端证据。** CI 的 `quality` job 离线跑（known-gaps
   E-03），ADR-065 §6 记过这个缺口不是理论上的。任何改这条路径的东西要对着真容器
   再跑一遍。

### 未做，各自需要自己的 ADR

- `ToolFailed.workspace_writes`（部分成功的运行落了哪些盘）——ADR-063 §4 明确划在
  范围外，且有测试钉住
- `RunFileResponse.written` 换成结构化条目——前端在同一时刻已能从 listing 拿到同样
  的信息，代价倒挂
- Task 侧列出 `ToolCompleted.workspace_writes` 的名字（本次未做，见任务清单）
- Chat 的引用原文读取端点——它是本设计里单位收益最高的一项，但需要一个新端点 +
  `ChatTurnStore` 一个按 id 的读方法 + 两套契约测试 + 一次带真库的本地跑。
  它有自己的 ADR-0067，不与前端一起提交

## 6. 证据

- `web/src/components/media.test.ts`：`checkCost` 的三界面矩阵；一条法律式断言
  ——一个既不能跑也不能转的界面**永远不会**被告知点一下有用；一条
  「`previewKind` 只回答它自己那个问题」的对照（`.py` 与 `.md` 同 kind，异代价）
- `web/src/features/code/CodePage.test.tsx`：跑一个画图脚本，图在**零次点击**后出现
  （断言收窄在 `aria-label="这次运行写出的文件"` 那一组里，否则会命中目录列表里的
  同名按钮）；listing 落后时不画死按钮
- `web/src/components/PythonPreview.test.tsx`：`remedyFor` 的 503/403/422 三分支，
  以及 409 与非 `ApiError` **不给处方**；`containerLimitNote` 四类各两三条真实 stderr
- `web/src/components/HtmlPreview.test.tsx`：提示节点在 iframe **之前**
  （`compareDocumentPosition`）
- `web/src/features/work/workTimeline.test.ts`：抓回来的 PDF 不当头条、同样的字节由
  `work` 节点产出则当头条；`.xlsx` 不当头条；HTML 可以当头条；`output_ref` 被收集
- `web/src/features/work/WorkPage.test.tsx`：失败的 Task 即使产出了文件也说自己失败
- `web/src/features/chat/ChatPage.test.tsx`：完整 chunk_id 在 title 里而短的在屏幕上；
  剪贴板写入成功与被拒各一条
- `tests/adapters/test_media_guess.py`：四个「两张表都没有」的图片后缀；未知名字由
  字节决定；跨 8 KiB 边界的多字节字符不算二进制
- `tests/adapters/test_workspace_tools.py::test_an_edit_keeps_the_media_type_the_write_declared`
- `tests/api/test_code_api.py::test_an_upload_whose_header_is_upper_case_is_stored_not_rejected`
  ——`Content-Type: TEXT/PLAIN` 从 500 变成 200 且落库已规范化
