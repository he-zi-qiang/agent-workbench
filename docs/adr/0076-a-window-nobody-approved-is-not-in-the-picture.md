# ADR-076：没人批准过的窗口，不在那张图里

- 决策点：ADR-070 定下「一次权限是关于一个窗口的」，并写了四道检查。落地一年后
  去对照 Claude Desktop 的做法，发现两件事：它用 **ScreenCaptureKit** 在**合成器**
  这一层过滤，而我们是「先画整屏、再涂黑矩形」；以及——更要紧的——ADR-070 §2 那句
  「人一次性批准一整份名单」，**这个仓库里从来没有实现过**。要不要照抄它的其余部分，
  以及这两件事怎么补
- 状态：**接受**。合成器过滤照抄（这是对的），**批处理原语拒绝**，**视觉通路这一轮
  拒绝**。同时补上那道从未存在的人工批准，并把过滤方向从黑名单翻成白名单
- 日期：2026-08-24
- 影响：新增 `apps/computer_mcp/consent.py`；`ScreenGate.grant` 变成 `async` 且会
  阻塞等人；`ScreenPort.capture` 的 `exclude_bundle_ids` 改为 `include_bundle_ids`；
  `adapters/screen/darwin.py` 换用 ScreenCaptureKit，`capabilities()` 从
  `exclude_mask` 变成 `exclude_native`；`pyproject.toml` 的 `computer-use` extra 增加
  `pyobjc-framework-ScreenCaptureKit`。**关闭 F-18**（按仓库规矩，条目从
  `known-gaps.md` 删除，关闭记在 `status.md`）
- 依赖：[ADR-070](./0070-a-permission-is-about-a-window-not-an-application.md)（本 ADR
  **兑现**它，不推翻它——它的模型一直是对的，缺的是实现。顺带更正一句**不在** ADR-070
  里、而在 known-gap F-18 里的话：「pyobjc 对它的覆盖不完整」。那句当时为真、现在为假，
  更正写在 ADR-070 §4 是因为读者会从那儿找过来）、
  [ADR-075](./0075-a-ledgered-effect-is-issued-not-proposed.md)（不可重放的工具进不了
  Task，本 ADR **不触碰**那条拒绝，见 §5）、
  [ADR-025](./0025-mcp-adapter.md) §2.6（MCP 绑定被钉死的字段，§4 的批处理讨论要用）

## 1. 背景：照着一个能跑的东西对表

Claude Desktop 1.34493.1 装在这台机器上，它的 computer use 是一个 Swift 原生插件
（`@ant/claude-swift/.../computer_use.node`），链接 AppKit、ApplicationServices、
CoreGraphics、QuartzCore 和 **ScreenCaptureKit**。工具面从它暴露的 schema 直接读到。

对表的结果是**我们的模型是对的、实现是缺的**——这比「模型错了」好，但比「都对」差得远，
因为一份写清了四道检查的设计文档，会让读它的人以为四道都在。

**它有而我们没有的，五条**：两段同意（应用名单一张卡，接管屏幕另一张）、合成器过滤、
批处理作为唯一交互原语、坐标参照系的明文纪律、background-app 与 display 两种作用域。

本 ADR 只处理其中一条（合成器过滤），明确拒绝两条（批处理、视觉通路），其余留在
`known-gaps`。**不照抄的理由和照抄的理由一样重要**，写在 §4。

**这一轮不碰工具面，而那半边比这份对表看上去的更要紧。**
[ADR-091](./0091-choosing-a-window-is-choosing-within-a-set-somebody-approved.md)
回头去补，发现差的不只是方便：六个工具里没有一个能改变前台应用，而每一个都要求被批准
的应用此刻在最前面——**任何跨两个应用的任务都走不到第二步，而且不是被拒绝，是没有工具
可调**。同一轮还发现 `list_granted_applications` 缺席让模型只能靠重弹对话框来问自己有
什么权限，正好抵消本 ADR §2 刚立下的那条同意。

## 2. 那道从来不存在的检查

对表过程中查到的第一件事，和 ScreenCaptureKit 无关，也比它严重。

`ScreenGate.grant` 收下**模型自己报的**应用名单，写进 `_granted`，返回
「Approved for this session」。没有对话框，没有阻塞，没有任何东西问过人。

ADR-070 §2 写的是「人一次性批准一整份名单」；工具描述里写的是
「The person approves the whole list once」。两句话都在，**实现一处也没有**。

其余三道检查都是真的——tier 表是真的，动作前重读最前面应用是真的，allowlist 从空开始
也是真的。它们守的是一份**没人给过的同意**。一个模型能自己写的 allowlist 只有一条：
它刚才要的那条。

补法在 `apps/computer_mcp/consent.py`，三个决定值得写下来：

**用子进程弹 osascript，不用 NSAlert。** `NSAlert` 要 `NSApplication` 和主线程 run
loop，而那条线程是 uvicorn 的。把一个模态框架在那儿，服务器就什么都不答了，包括运维
用来查「为什么卡住」的健康探针。子进程可以挂、可以被杀，服务器不跟着走。

> **这条的结论已被
> [ADR-092](./0092-a-server-that-changes-the-front-of-the-screen-is-an-application.md)
> 推翻，理由没有。** 那一批发现 macOS 只允许「主线程跑着 run loop」的进程改变前台
> 应用，于是主线程给了 `NSApplication`、uvicorn 挪到后台线程——**一个模态框现在会卡住
> 的那条 run loop，已经不是在服务 HTTP 的那条了**（实测：服务器在 .app 里跑起来后
> 健康探针照常应答）。上面这段的前提「那条线程是 uvicorn 的」不再成立，所以结论
> 不再适用。**同意机制本身没有改**：`consent.py` 继续用 osascript 子进程，本节另外
> 两条理由（模型的字符串永远不进脚本正文、任何说不清的答案都是拒绝）与线程无关，
> 原样成立。换成 `NSAlert` 现在变得**可能**，但那要它自己的一次决定。

**模型的字符串永远不进脚本正文。** 应用名是模型给的，把它插进 AppleScript 源码就是
代码注入——一个带引号和 `do shell script` 的名字会被执行。所以脚本是常量，所有可变文本
走 `argv`，AppleScript 在那儿只当数据。2026-08-24 用一个带 `do shell script "touch
/tmp/pwned` 的名字实测：被当作文字显示，`/tmp/pwned` 没有被创建。

**任何说不清的答案都是拒绝。** 超时（`gave up:true`）、Esc（osascript 非零退出）、机器上
没有 `osascript`、输出对不上——没有一个是人在说同意，而「我们不知道」的唯一安全读法是
「不」。唯一能放行的，是允许按钮把自己的名字报回来。默认按钮是**拒绝**，所以顺手敲回车
的人拒绝了。

对话框里逐个应用**显示 tier**，因为那是人推不出来的部分：批准 Terminal 给出的权限
严格小于批准 Notes，一个把这件事藏起来的对话框，收的是另一件事的同意。

## 3. 把方向翻过来，问题就没了

第二件事：**空 allowlist 会截到整个桌面**。改动前实测，一个没有任何 grant 的 gate 返回
完整的 1375×894 全屏图。而 `gate.py` 自己的 docstring 写着
「everything **not** granted is excluded from the frame」——`_to_exclude` 恒返回 `()`
让这句话是假的。这是 F-18 活着的那一半。

它一直没被修，是因为旧代码里写下的那个理由**在当时是对的**：

> 按 bundle id 排除，需要知道什么在**运行**，而这个 port 不暴露——给它加上「枚举所有
> 正在运行的应用」，等于把一个比被拒的那个更有意思的能力交给模型。

把问题**反过来问**，这个理由就没有了。gate 不需要知道什么在运行，它需要说**什么被批准了**
——那正是它唯一知道的事。适配器在自己内部把 bundle id 解析成窗口，模型永远不知道屏幕上
还有别的什么。所以 `exclude_bundle_ids` 变成 `include_bundle_ids`。

**方向本身是安全决定，不是口味问题。** 黑名单是 fail-open 的：漏掉一个就泄露一个。而且
它**根本写不全**——这台机器上实测，一个标题为 `underbelly`、由 WindowServer 拥有的窗口
一直在屏幕上，它的 bundle id 是**空字符串**，黑名单连它的名字都点不出来。白名单只需要
点出被批准的那几个，点不出的一律不在图里。

## 4. 照抄什么，不照抄什么

**ScreenCaptureKit —— 抄。** `SCShareableContent` → `SCContentFilter` 的
`initWithDisplay:includingWindows:` → `SCScreenshotManager.captureImage...`。两个完成
回调都在工作线程上用 `threading.Event().wait()` 等——SCK 在内部 dispatch queue 上回答，
不是主 run loop，所以这个从不启动 `NSApplication` 的服务器能用它。

`SCStreamConfiguration` 的宽高是**像素**且被精确遵守，所以 ADR-070 §3.2 二分出来的那个
预算值直接喂进去，没有第二次缩放来跟它打架。

实测（macOS 26.5.2 / arm64）：列窗口 46 ms，过滤截图 43 ms，合计约 70 ms，对比它替掉的
CoreGraphics 抓屏 22 ms。慢了三倍，仍比一次模型往返小一个数量级。

过滤是**真的**而不是好看：同一块屏、同样 400×260，整屏 PNG 96,147 字节，只含一个应用的
34,957 字节——别的窗口不在图里，所以压不出那些字节。

顺带一条**收窄**：gate 现在**只**接受 `exclude_native`。它以前也接受 `exclude_mask`，
而那是 F-18 后半句（「抓屏是遮盖不是合成器过滤」）一直开着的原因。遮盖是先画完整帧、再
按另外读来的几何涂黑：像素在缓冲区里存在过，而且那份几何在快门落下时可能已经过期。
两者是不同的承诺，只有前一个经得起窗口在读几何和按快门之间移动。

**批处理原语 —— 拒。** Claude Desktop 只暴露 `computer_batch`，单个 `left_click`／`type`
根本不给。它的理由是延迟：每个工具调用一次模型往返。

这个理由**在我们这儿不成立**，因为付那个往返的循环不归这个系统所有——computer server 的
六个工具经由 MCP 被别人的循环调用。而代价是实打实的：它把**授权单位**从「一个动作」
塌缩成「一个数组」。Policy Gateway 会只看见一个 `mcp_computer_batch` 提案，逐动作的判断
全掉进 `ScreenGate` 内部，没有任何策略规则够得着。这个仓库的网关建立在「一次调用、校验
过的参数、一个决定，且紧贴效果」之上——`_invoke_ledgered` 在派发前一行**再取一次**授权，
就是为这个。批处理是这条原则倒着跑。

它还会压平审计线索：computer profile 开着 `record_step_inputs`，整个动作数组会被预览进
**一条** `ToolProposed`，而前端刚学会渲染的那条「提议→授权→开始→完成」会变成一条。

**坐标参照系纪律 —— 抄可强制的那一半。** Claude Desktop 明写「本批坐标一律指向调用前那张
全屏图，zoom 图永不成为参照系」。我们把单位钉死了（点、左上原点、从不说像素），**参照系
没钉**。这不只是文档缺口，是个活 bug：`capture` 按显示器返回该显示器的点尺寸，而 `click`
发的是**全局坐标**的 CGEvent——第二块显示器上照着 `screenshot(display_id=N)` 读的坐标会
点错地方。本 ADR **不修它**（这一轮范围已经够大），记进 known-gaps。

**视觉通路 —— 这一轮拒。** Claude Desktop 整个循环建立在模型**看得见**截图之上。我们的
模型层收不了图片：`ContentBlock` 是 `TextBlock | ToolUseBlock | ToolResultBlock`，没有
image 成员。domain 那半很便宜（4 处调用点）。**墙在 provider**：
`provider: Literal["deepseek", "fake"]` 是 `docs/configuration.md` §3 的不变量，DeepSeek
适配器把三个 role 的 content 全序列化成纯字符串，没有 `image_url`、没有 content 数组。
而且运行时**没有上下文窗口管理**——对话只增不减，约十张截图撑爆 128K。

要做，得先有它自己的 ADR。

## 5. 后果

**ADR-075 那条拒绝原封不动。** 屏幕工具仍然进不了 Task，理由完全没变（键推导不出来，
且模型看不见）。本 ADR 修的是**另一条路**上的东西：用 MCP 客户端直连这台 server 的那条路
——那条路上现在真的会有人被问、未批准的窗口真的不在图里。两件事不要混。

**`grant` 会阻塞等人，最长 120 秒。** 这是一个模型看得见的行为变化：调用 `request_access`
不再立刻返回。工具描述照实写了。没人在机器前的时候，它超时并拒绝，而不是超时并放行。

**`ConsentUnavailableError` 和「被拒绝」是两回事。** 前者是「这台机器上没法问人」，后者是
「问了，人说不」。它们要修的东西不同，只有一个值得重试。

**代价：多两个 wheel。** `pyobjc-framework-ScreenCaptureKit` 与 `pyobjc-framework-coremedia`，
都是 12.2.2，与已经钉住的 pyobjc-core／Cocoa 同版本，`uv lock` 无冲突。CI **不**装
`computer-use` extra，所以这条路的证据只能是本机的，本 ADR 里的数字都按此标注。

**F-18 关闭。** 按仓库规矩，条目从 `known-gaps.md` 删除，关闭记进 `status.md`。
F-19（批准是进程级而非 MCP 会话级）**不受影响**，仍然成立。

## 6. 被拒绝的方案

**继续用遮盖，只是把矩形算准一点。** 算不准不是实现质量问题：窗口会在「读几何」和
「按快门」之间移动，而那两件事之间必然有时间。合成器过滤没有这个窗口期。

**在 gate 里枚举正在运行的应用，好把黑名单填全。** 旧注释拒绝它的理由仍然成立——那是把
一个更有意思的能力交给模型。白名单不需要枚举，所以这个取舍不必再做。

**把批准做成配置项**（比如 `computer.auto_approve = ["com.apple.Notes"]`）。它把「人当场
看着屏幕做的决定」换成「部署时写下的一句话」，而 ADR-070 第三道检查的全部意义就是权限要
对着**此刻**的屏幕算。一份配置不知道现在最前面的是什么。

**等做完视觉通路再一起做。** 那会把两件独立的事绑在一起，其中一件（同意）是**已经在生产
里错着的**，另一件（视觉）需要动一条 §3 不变量。先修错的那件。
