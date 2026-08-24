# ADR-070：权限是关于一个窗口的，不是关于一个应用的

- 决策点：给这个项目加 computer use。它此前一行相关代码都没有——没有 GUI、没有屏幕、
  没有任何可被治理的输入合成。要不要加；如果加，**门禁放在哪一层**、平台相关的那部分
  怎么隔离、以及一个「已批准」的许可在多长时间内还算数
- 状态：**接受**。新增第四个项目自有 MCP 服务端 `agent-computer-mcp`。门禁是
  四道检查，其中第三道——**在每一个动作即将发生前重新读一次当前前台应用**——是整个
  设计的支点。tier 表、截图 token 预算、焦点复查全部落在 `domain/computer.py`，
  不碰屏幕、可完整测试；碰屏幕的只有 `adapters/screen/darwin.py` 一个薄模块，
  pyobjc 放在 macOS-only 的可选 extra 后面，CI 不装
- 日期：2026-08-18
- 影响：新 `domain/computer.py`、新 `ports/screen.py`、新
  `adapters/screen/{__init__,darwin}.py`、新 `adapters/memory/screen.py`、新
  `apps/computer_mcp/{__init__,gate,server,main}.py`、新
  `config/config.computer-local.toml`、`pyproject.toml`（`computer-use` extra +
  `agent-computer-mcp` 入口）
- 依赖：[ADR-044](./0044-no-remote-no-production-identity.md)（先有远端部署才谈得上
  生产身份——本 ADR 的 loopback 绑定比它更硬：一个监听非回环的端口就是一台远程输入
  设备）、[ADR-025](./0025-mcp-adapter.md)（MCP 只是外层适配器——
  `retryable_effects = false` 那条声明由它的 §2.7 要求。写本 ADR 时，这六个工具
  够不着 Task 只是那条声明的副作用；[ADR-075](./0075-a-ledgered-effect-is-issued-not-proposed.md)
  收窄了 §2.7 的重开条件，把这个副作用本身立成决策：非可重放的 MCP 工具只能由
  自己发起调用的确定性节点带进 Task，不由模型提案）、
  [ADR-029](./0029-ephemeral-sandbox.md)（一次性沙箱——tier `click` 的拒绝文案把
  模型推回的正门就是它）

## 1. 背景：这个项目此前没有这件事，而且理由是成立的

`agent-api` 绑在回环上、是本地开发用的控制面（ADR-044）。它没有 GUI，没有屏幕，
也就不存在需要治理的 tier、截图预算或焦点竞争。

所以这不是「补一个缺口」，是**新加一种能力**，而且是这个仓库里权限含义最重的一种：
其它所有工具的作用域都是这个进程自己的工作区、自己的数据库、自己的沙箱容器；这一个
的作用域是**运行 Worker 的那台机器本身**。

## 2. 决策：门禁四道检查，顺序就是论证

```
1. 这个应用被批准过吗？        —— 会话级 allowlist，人一次性批准一整张名单
2. 这个动作被这个 tier 允许吗？ —— domain/computer.tier_for
3. 它现在还在最前面吗？        —— 动作发生前重读，从不缓存
4. 之后它还在最前面吗？        —— 只有打字需要，因为只有打字会被打断到一半
```

### 2.1 第三道是支点

一个「批准一次、然后照做」的门禁，授权的是**当时那个屏幕**；等到按键真的落下去时，
屏幕是**现在这个样子**。中间隔着一次 Command-Tab，一个弹窗，一次自动更新提示。

所以 tier 不是在批准时算一次存起来，而是在每个动作前**用当下的前台应用重新算**。
测试 `test_the_tier_is_re_read_against_whatever_is_frontmost_now` 钉的就是这条：
Notes 和 Terminal 都在 allowlist 里，只看 allowlist 的门禁会放行，而键入 Terminal
的那次必须被拒。

这一道最容易省掉，而且**省掉之后无法后补**——一旦上层结构假定「许可是一次性算出来
的」，把它改成每次重算就是把每个调用点都改一遍。

### 2.2 三级 tier，一个没有例外分支的函数

| 类别 | tier | 能做 | 不能做 |
| --- | --- | --- | --- |
| 浏览器、交易/钱包 | `read` | 出现在截图里，可以读 | 点击、打字，全部拒绝 |
| 终端、IDE | `click` | 可见 + 普通左键单击、滚动 | 打字、按键、右键 |
| 其它一切 | `full` | 无限制 | —— |

判定先查 bundle id（精确、不完整），再查名字子串（完整、可伪造），**两个都要**：
只认 bundle id 的话，上周发布的浏览器会落到 `full`——于是**这个项目没听说过的浏览器
反而是唯一会被输入密码的那些**。只认名字的话，一个终端把自己叫做 "Notes" 就够了。

名字子串按**长度从长到短**匹配，否则 "Chrome Remote Desktop" 会被 "chrome" 判成
浏览器。

`tier_for` 里没有任何例外分支，这是刻意的：每一个能加进去的「除非……」都是一条绕过
门禁的路，而门禁是模型和密码输入框之间唯一的东西。

### 2.3 终端被钉在 `click` 上，是因为正门已经存在

不许对终端打字，不是因为终端危险，是因为**这个项目已经有一条跑命令的正门**：
`sandbox_run` 走一次性容器、策略网关、审批闸门和事件流（ADR-029）。往终端窗口里敲
键盘的那条路，这四样一样都没有。

所以拒绝文案里那句「For shell commands, use the sandbox tool」不是安慰，是把模型
推回正门。文案有三段，第三段才是关键：

```
"Terminal" is granted at tier "click", so type is not available for it.
Keystrokes would go to this application's command line. For shell commands,
use the sandbox tool, which runs them with a policy gate and an audit trail.
Do not attempt to work around this restriction -- never use AppleScript,
System Events, shell commands, or any other method to send input to this
application.
```

没有第三段，前两段读起来像建议。而它禁掉的每一条路这台机器上都真的有。

### 2.4 打字之后还要再查一次

按键跟着键盘焦点走。一个窗口在字符串打到一半时抢到前台，**剩下的字符跟着它走**——
于是同一串字落进两个应用，其中只有一个被批准过。

适配器因此报告**送达了多少个字符**，门禁用这个数字拒绝，而不是用一句「denied」。
理由很具体：只被告知 denied 的模型会重打整串，于是前半段到两次。

## 3. 分层：可测的那部分与碰屏幕的那部分

```
domain/computer.py     tier 表 · 截图预算 · 拒绝文案     纯函数，30 条测试
ports/screen.py        契约（点坐标，不是像素）
adapters/memory/screen.py  可编程假实现——能让前台应用在两次调用之间变
adapters/screen/darwin.py  Quartz / CGEvent，全仓唯一一个 pyright 抑制的文件
```

**测试用不着屏幕，这件事本身就是设计在起作用。** 焦点复查这条规则之所以测得了，
正是因为假实现能做一件真屏幕没法配合的事：在 `type_text` 前后返回不同的前台应用。

`darwin.py` 顶部有六条逐条列出的 pyright 抑制而不是切成 basic 模式——pyobjc 没有
类型信息，严格模式在这个文件里报 186 个 unknown，一条都不是这个项目能处理的缺陷；
但严格模式**还能说的**（未定义的名字、不可达分支、对本模块自己函数的错误调用）
仍然要红。全仓库只有这一个文件带抑制，这正是把 FFI 压进一个薄模块的意义。

### 3.1 点，不是像素

端口只说**显示器自己的点坐标，原点左上**，永远不说像素。中间有两次换算会咬人：
retina 面板报 1470×956 点、实际 2940×1912 像素；截图又会被预算缩小一次。任何一次
换算错了，点击就落在两倍或一半的位置——而且是静默的，因为点空了也还是一次点击。

所以适配器在边界上换算一次，上面所有代码都只说点。

### 3.2 截图预算：两个上限，而且是不同的那个先咬

```
px_per_token = 28    max_edge_px = 1568    max_tokens = 1568
```

只夹长边的实现会按自己的规矩每次都「正确」，然后送出 3136 token 的图：1568×1568
在边长上限之内，是 token 上限的两倍。两个上限都要，而且在宽屏上先咬的是 token 那个。

二分搜索而不是解析解：两个上限在不同屏幕上分别生效，而取整到整像素让闭式解在边界上
是错的——算出来「刚好放得下」的尺寸，取整之后放不下。

**实测**（本机，真适配器）：1470×956 点 → 1375×894，正好 1568 token。

## 4. 被拒绝的替代方案

**把门禁放进 MCP 工具处理函数里。** 一个在处理函数里做的检查，是下一个处理函数会
忘掉的检查。六个工具全部走同一个 `ScreenGate`，四道检查只有一份。

**让 `request_access` 自己指定 tier。** 那样模型可以为 Chrome 申请 `full`，而正在
批准一张应用名单的人不会去读 tier 那一列。tier 从应用本身推出，不接受申请。

**用 `screencapture` + `osascript` 而不是 pyobjc。** 零新依赖，但每个动作 fork 一个
进程（几十到上百毫秒），拿不到 `SCContentFilter` 那种合成器级的窗口排除，按键合成也
更粗糙。使用者明确选了 pyobjc。

**默认装 pyobjc。** 它是 macOS-only，而 CI 在 Linux 上跑。放进 `computer-use`
extra，和 `embedding` 同样的处理；缺它时适配器在**启动时**抛错，而不是注册一批
「调用成功、什么也没发生」的工具——这正是 macOS 对没有辅助功能授权的进程做的事：
`CGEventPost` 返回成功，然后什么都不做。

**把批准做成持久化的。** 会话级、进程重启即清空。一个跨重启记住「可以点这个应用」
的授权，是一个没有人再看着屏幕时依然成立的授权。
