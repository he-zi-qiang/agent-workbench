# ADR-092：能改变屏幕最前面那扇窗的服务器，自己必须是一个应用

- 决策点：[ADR-091](./0091-choosing-a-window-is-choosing-within-a-set-somebody-approved.md)
  加的 `activate_application` 在真机上**一次都没成功过**（known-gap F-30）。查下来不是
  ADR-091 的门禁写错了，是**这个进程的形态**在 macOS 上根本没有资格改变前台应用。
  要不要为这一个能力改变服务器的进程形态；改了之后 ADR-076 §2 拒绝 `NSApplication`
  的那条理由还成不成立
- 状态：**接受**。computer-use MCP 服务器从「一个 python 进程」变成「一个签名的
  `.app`」：**主线程交给 `NSApplication` 的 run loop，uvicorn 挪到后台线程**。
  这**推翻** ADR-076 §2 对 `NSApplication` 的拒绝——但推翻的是结论，不是理由：它当年的
  理由是「模态框会卡住那条正在服务 HTTP 的线程」，而现在服务 HTTP 的已经不是那条线程了。
  **关闭 F-30**
- 日期：2026-08-29
- 影响：新 `scripts/build_computer_app.sh`（产出签名 bundle）；
  `apps/computer_mcp/main.py`（uvicorn 进后台线程）；
  `adapters/screen/darwin.py`（新增 `give_main_thread_to_appkit`、`can_change_frontmost`，
  并新增第八条 pyright 抑制 `reportUntypedBaseClass`）；`scripts/dev.sh computer-server`
  改为构建并启动 .app；`config/config.computer-local.toml` 的前置条件从三条变四条；
  `tests/config/test_local_computer_profile.py` 与 `tests/apps/test_computer_darwin.py`
- 依赖：[ADR-091](./0091-choosing-a-window-is-choosing-within-a-set-somebody-approved.md)
  （本 ADR 让它的 `activate_application` 第一次真的能工作。它的**门禁逻辑一个字没改**
  ——两道检查、不点名的拒绝、tier 不管激活，全部照旧且实测有效，见 §5）、
  [ADR-076](./0076-a-window-nobody-approved-is-not-in-the-picture.md) §2（**被本 ADR
  推翻的那条**，见 §3）、
  [ADR-070](./0070-a-permission-is-about-a-window-not-an-application.md) §3、§4
  （pyobjc 只压在一个文件里——§3 原话说的是「全仓只有这一个文件带 pyright 抑制」，
  那句从来没成立过，2026-08-29 已在该 ADR 就地更正；本 ADR 依据的是收窄之后的那句。
  以及「宁可启动时抛错，也不要注册一批调用成功却什么都没发生的工具」——本 ADR 把这条
  原则又用了一次，见 §4）

## 1. 缺口：四个条件，缺一个就静默失败

F-30 记的是「activate 不工作」，而这一批把它拆到了根。macOS 允许一个进程改变前台
应用，需要**四个条件同时成立**，而其中三个是**进程怎么被启动的**属性，不是代码写得
对不对。

2026-08-29 在这台机器上（macOS 26.5.2 / arm64），固定其余三条、每次只去掉一条实测：

| 缺哪一条 | 激活成功次数 |
| --- | --- |
| bundle 身份（经 LaunchServices 启动） | **0/20**（裸解释器，`bundleIdentifier()` 为 `None`） |
| 代码签名 | **0/10**（人把开关打开了，`AXIsProcessTrusted()` 仍然是 `False`） |
| 辅助功能授权 | **0/10** |
| **主线程活着的 run loop** | **0/15** |
| 四条都在 | **15/15**，包括从人正在打字的那个窗口手里抢焦点 |

**每一种失败都是静默的。** `activateWithOptions_` 返回 `true` 而屏幕不动；
`AXUIElementSetAttributeValue(kAXFrontmostAttribute)` 返回 `0` 而屏幕不动；
`AXUIElementPerformAction(kAXRaiseAction)`、`NSWorkspace.openApplicationAtURL(activates:)`、
合成点击 Dock 图标、合成点击后台窗口标题栏（含移动+停顿+按住三种形状）——全部一样。
而**同一条路径上光标确实会移动**，所以事件真的投递出去了，只是「谁在最前面」这件事
不归它管。

这正是 ADR-070 §4 用一整段拒绝过的形态，只是那一段说的是 `CGEventPost`：
**「调用成功、什么也没发生」**。

### 1.1 诊断过程本身值得记一笔

查到第四条之前，有三个假设被提出来又被数据否掉：**激活请求在进程退出时才落地**、
**`NSApplication.sharedApplication()` 就是解药**、**成功与否取决于当时谁在前台**。
第二个假设一度看起来成立（一次 5 ms 的成功），实际是**上一个进程排队的请求恰好在那时
落地**——把「焦点确实动了」当成了「我这次调用让它动了」。

写下来是因为这条缺口的正确诊断需要**对照组**：不做任何调用观察 20 次，前台自发变化
0 次。没有那个对照，上面每一次「成功」都会被读成证据。

## 2. 决定：把服务器做成 .app，把主线程还给 AppKit

```
scripts/build_computer_app.sh   →  ~/Applications/AgentComputerMCP.app
                                    · CFBundleIdentifier 固定
                                    · codesign --sign -（ad-hoc）
                                    · LSUIElement = true
main.py                          →  uvicorn 在后台线程
darwin.py give_main_thread_to_appkit()  →  NSApplication.run() 占主线程
```

### 2.1 为什么是 Accessory 而不是 Regular

`LSUIElement = true` → activation policy 1（Accessory）：**注册进窗口服务器**（激活需要
的正是这个），但**不占 Dock 图标、不占菜单栏**。一个在别人 Dock 里放图标的服务器，是在
宣称自己是一个可以被切换过去的应用，而它不是。

实测中 policy 0（Regular）反而失败——这一条没有深究，因为 Accessory 才是想要的形态。

### 2.2 构建在 checkout 之外，而且必须签名

两条都是实测撞出来的，不是洁癖：

- **LaunchServices 不启动隐藏目录下的 bundle。** 这个仓库的 worktree 全在
  `.claude/worktrees/…` 下面，而仓库路径本身还带中文。实测 `open -a` 一个建在那里的
  bundle：**没有进程，没有报错，什么都没有。**
- **未签名的 bundle 拿不到授权。** 人可以把它加进辅助功能列表、可以把开关打开，
  而 `AXIsProcessTrusted()` 依然返回 `False`。授权挂在**代码身份**上，所以必须有一个。

因此 bundle id 与签名标识都**固定**：TCC 用这两样认人，任何一个变了，人就得再去一趟
系统设置，而且**撤销是不可见的**——直到某次激活悄悄不工作了才会发现。`build_computer_app.sh`
重建不改变这两样。

### 2.3 launcher 写日志，因为 LaunchServices 不给它地方写

从 .app 启动的进程没有终端、没有 journal。一个在启动时退出的服务器（缺授权、端口被占）
会**什么都不留下**。所以 launcher 把 stdout/stderr 重定向到
`~/Library/Logs/AgentComputerMCP.log`。这条不是调试便利，是 ADR-070 §4 那句「一个启动
就退出的服务器要给人留下一句能读的话」在新的启动方式下的兑现——实测第一次跑起来时，
缺屏幕录制授权的那条消息正是从这个文件里读到的。

## 3. 推翻 ADR-076 §2 的哪一句，以及为什么可以推翻

ADR-076 §2 写的是：

> **用子进程弹 osascript，不用 NSAlert。** `NSAlert` 要 `NSApplication` 和主线程 run
> loop，而那条线程是 uvicorn 的。把一个模态框架在那儿，服务器就什么都不答了，包括运维
> 用来查「为什么卡住」的健康探针。

**那条理由当时是对的，现在仍然是对的——它只是不再适用。** 它成立的前提是「主线程是
uvicorn 的」，而本 ADR 把这个前提换掉了：主线程是 AppKit 的，uvicorn 在后台线程。
**一个模态框现在会卡住的那条 run loop，已经不是在服务 HTTP 的那条了。**

实测（2026-08-29）：服务器在 bundle 里跑起来之后，健康探针照常应答——

```
$ curl -s http://127.0.0.1:8768/health
{"status":"ok","service":"agent-workbench-computer","transport":"streamable-http",
 "displays":1,"capabilities":["exclude_native"]}
```

**本 ADR 不改同意机制。** `consent.py` 继续用 osascript 子进程。ADR-076 §2 另外两条
理由（模型的字符串永远不进脚本正文；任何说不清的答案都是拒绝）与线程无关，原样成立。
换成 `NSAlert` 现在**变得可能**了，但「可能」不是「应该」——那要它自己的一次决定，
不在本 ADR 范围。

## 4. 检查放在动手之前，而不是让它超时

`can_change_frontmost()` 在每次激活之前问一次：有没有 bundle 身份、有没有活着的 run
loop。缺了就**立刻拒绝并说明是哪一条**，而不是等两秒超时。

理由是 §1 那张表：每一种失败都是静默的，所以一句「它没生效」虽然是真话，却
**分不清「那扇窗不肯来」和「这个服务器没被打包」**——而这两件事的解决办法完全不同，
只有一个值得重试。

两道 TCC 授权仍然在**构造函数**里预检（录屏那道自 ADR-070 起就有，辅助功能那道是
2026-08-29 补的，见 status.md 第四十六批）。run loop 不能在那里检查：适配器被组装出来
的时候它还不存在，所以它是在**使用时**问的。

## 5. ADR-091 的门禁逻辑，一个字没改，而且实测有效

本 ADR 只换了进程形态。端到端跑完（真 MCP 客户端 → 真服务器 → 真屏幕）之后，
ADR-091 的每一条都照旧成立：

- **§2.2 的收窄真的会拦。** 第一次端到端时 Claude Desktop 在最前面而它不在名单里，
  三次激活**全部被拒**，而且拒绝文案**没有点名 Claude**——正是 §2.2 要的行为。
  把一个已批准的应用放到前面之后，同样三次调用全部成功。
- **§2.3 成立。** 切到 Safari（tier `read`）之后，工具如实回报
  `Safari is frontmost, at tier read`，而 Safari 依然不可点击、不可打字。
- **第一次切换必须由人发起。** 这是 §2.2 的直接推论，之前没有被写出来：模型只能在
  「已批准的应用已经在前台」时重排，所以会话的第一扇窗是人放上去的。

## 6. 后果

**多一步安装，而且这一步只有人能做。** 新 bundle 要人在系统设置里给**两个**授权
（辅助功能、屏幕录制）。`config.computer-local.toml` 的前置条件因此从三条变四条。

**F-30 关闭。** 按仓库规矩条目从 `known-gaps.md` 删除，关闭记进 `status.md`。

**ADR-075 那条拒绝不受影响。** 屏幕工具仍然进不了 Task。

**这条路的证据全部是本机的。** CI 不装 `computer-use` extra，也没有屏幕、没有 TCC，
`tests/apps/test_computer_darwin.py` 在那边整份 skipped。本 ADR 里每一个数字都按此标注。

**没有解决的一件事**：`can_change_frontmost()` 判断 run loop 用的是
`NSApplication.isRunning()`。这是一个**够用但不精确**的代理——它回答的是「AppKit 的
loop 起来了吗」，而不是「窗口服务器认不认这个进程」。四条件同时成立时它是对的，
将来若出现第五个条件，它不会知道。

## 7. 被拒绝的方案

**AppleScript / System Events。** 这个项目**自己的拒绝文案**逐字禁止它——`refusal()`
第三段、`activation_needs_a_grant`、`activation_would_take_the_screen` 都写着
never use AppleScript。不能一边这样告诉模型，一边自己用它实现这个工具。

**合成点击 Dock 图标。** 看起来最省事：不加依赖、不改形态、复用已有的输入通路。
两个理由拒掉——**它不工作**（实测 0/N，含移动+停顿+按住三种形状），以及**即使它工作
也会拆掉 ADR-091 §2.3**：那一节说「激活不合成任何输入，所以不由 tier 管」，而用点击
实现的激活让这句话变成假的，于是「为什么 tier `read` 的浏览器可以被激活」就要重新论证。

**把服务器做成 Regular 应用（有 Dock 图标）。** 见 §2.1：那是在宣称自己是一个可以被
切换过去的应用。而且实测 Regular 反而失败。

**不做，把 `activate_application` 删掉。** 这是 F-30 认真列过的一条，也是使用者当时
面对的三个选项之一。使用者选了「我想让它有这个能力」，本 ADR 是那个决定的落地。
它的代价（多一次安装、多两个授权、推翻一条既有决定）都写在上面，没有藏。
