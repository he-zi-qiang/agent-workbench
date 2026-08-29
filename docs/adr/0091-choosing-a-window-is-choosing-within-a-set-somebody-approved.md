# ADR-091：模型可以挑窗口，但只能在人批准过的那一组里挑

- 决策点：再对一次 Claude Desktop 的 computer use 工具面（10 个）与本仓库的（6 个），
  发现三处 ADR-070 / ADR-076 / known-gaps 都没记过的缺口。最要紧的一条是
  **六个工具里没有一个能改变前台应用**，而 `ScreenGate._require_frontmost` 的第 3 道
  检查要求「被批准的应用此刻在最前面」——于是任何跨两个应用的任务都走不到第二步。
  要不要给模型这个能力；给了之后第 3 道检查还剩下什么意思；以及另外两处缺口
  （没有 `list_granted_applications`、权限表比工具表宽）怎么处理
- 状态：**接受**。新增 `activate_application` 与 `list_granted_applications` 两个工具，
  工具面 6 → 8。第 3 道检查的含义**确实变了**，本 ADR 就是为这个变化写的：它从
  「人选了这扇窗」变成「模型在人批准的集合里选了一扇」，而让这个变化可接受的是一条
  收窄——**当前前台应用不在 allowlist 里时，拒绝激活**。同时把 `_ALLOWED` 收窄到工具
  真的够得着的动作，并从 `ScreenPort` 删掉 `move`
- 日期：2026-08-28
- 影响：`domain/computer.py`（`_ALLOWED` 去掉 `mouse_move` 与 `drag`，新增四条激活
  拒绝文案）；`ports/screen.py`（`move` 删除，新增 `activate`）；
  `adapters/screen/darwin.py`（`move` → 私有 `_move`，新增 `activate`）；
  `adapters/memory/screen.py`（新增 `installed` 与 `activation_lands` 两个可编程维度）；
  `apps/computer_mcp/gate.py`（新增 `activate` 与 `frontmost_grant`）；
  `apps/computer_mcp/server.py`（两个新工具）；`apps/computer_mcp/consent.py`（对话框
  多说一句）；`config/config.computer-local.toml` 与 `scripts/dev.sh` 的工具清单；
  `web/src/features/computer/ComputerPage.tsx`。新增 known-gap **F-29**（不启动应用）
- 依赖：[ADR-090](./0090-a-coordinate-carries-the-screen-it-was-measured-on.md)（本 ADR
  的基线。它在本批写到一半时进了主线，改的是同一批文件：端口下面一律全局点、`Display`
  带原点、换算进 domain。两批不冲突——激活不带坐标，收窄的许可表里也没有坐标——唯一
  真正的交汇是 `move`，见 §2.4）、
  [ADR-070](./0070-a-permission-is-about-a-window-not-an-application.md)（四道检查，
  本 ADR 改的是第 3 道**的语义**，不改它的机制——重读、不缓存、每个动作之前，一个字没动）、
  [ADR-076](./0076-a-window-nobody-approved-is-not-in-the-picture.md)（对表 Claude Desktop
  的那一轮，它明确写了「本轮不碰工具面」，本 ADR 碰的正是它留下的那半；§3 里
  「不要把枚举正在运行的应用这个能力交给模型」的取舍，本 ADR 原样沿用）、
  [ADR-075](./0075-a-ledgered-effect-is-issued-not-proposed.md)（屏幕工具进不了 Task，
  本 ADR **不触碰**那条拒绝：新增的两个工具同样 `retryable_effects = false`）

## 1. 缺口是怎么被看见的

不是读代码读出来的，是把两份工具面并排放出来看出来的。Claude Desktop 有 10 个，
这里有 6 个，差的那几个里有一个不是「少一件方便的东西」：

```
Claude Desktop (10)          本仓库 (6)
request_access               request_access
list_granted_applications     ——                    ← 缺口 2
open_application              ——                    ← 缺口 1
computer_batch               screenshot / left_click / type / key / scroll
                             （它把这五件事收进一个数组；ADR-076 §4 拒绝了那个形状，
                               这一格不是缺口）
read_clipboard                ——
write_clipboard               ——
switch_display                ——
request_teach_access          ——
teach_step / teach_batch      ——   （以上四类不在本 ADR 范围）
```

**缺口 1 是一个死锁，不是一个短缺。** 第 3 道检查要求被批准的应用此刻在最前面，
而没有任何工具能让某个应用到最前面。唯一的现实办法是点 Dock——但点 Dock 需要
Finder 的 grant，而点它本身又要先满足第 3 道检查（Finder 得先在最前面）。于是：

> 一个人批准了 Notes 和 Word 两个应用，任务需要从前者抄到后者。第一步能做，
> 第二步做不了，而且**不是被拒绝**——是没有工具可调。

这一条此前从没被记下来过。ADR-070 定的是门禁，ADR-076 对的是抓屏和同意，
`known-gaps.md` 里 computer use 只有 F-19（批准是进程级的）和 F-21（进不了 Task）。
三份文档谁也没说过「这套工具面走不完一个跨应用的任务」。

## 2. 决定

### 2.1 加 `activate_application`，而第 3 道检查的意思因此变了

必须先把这句话说清楚，因为它是本 ADR 唯一真正的代价：

| | 第 3 道检查回答的问题 |
| --- | --- |
| ADR-070 起 | 这扇窗是**人**放到前面的吗？ |
| 本 ADR 起 | 这扇窗是**人批准过的那一组里的**一扇吗？ |

前一个更强。它成立的时候，前台是一件模型完全无法影响的事实，门禁读它就像读时钟。
加了激活之后，模型能写这块时钟了——所以「前台应用被批准」不再意味着「人此刻在看着
它并且选择了它」。

**这不是把第 3 道检查削弱成装饰。** 它守住的东西还在，只是范围小了一圈：模型仍然
不能对任何没被批准的窗口做任何事，仍然是每个动作前重读、从不缓存，仍然按当下的前台
应用重新推 tier。丢掉的那一圈是「人的当下选择」这一层额外信息。

**换来的是一件此前根本做不成的事**，而不是一件更方便的事。这个交换要成立，得配一条
收窄——就是下一节。

### 2.2 收窄：当前前台不在名单里时，拒绝激活

```
activate(target):
  1. target 在 allowlist 里吗？        —— 否则拒绝，指回 request_access
  2. 此刻最前面的那个在 allowlist 里吗？—— 否则拒绝，且不说它是谁
  3. 让端口去激活，按它回报的前台应用作答
```

第 2 条是本 ADR 的支点。没有它，`activate_application` 就是「模型可以随时把屏幕抢
过来」——一个人切到邮件去读一封信，任务把窗口从那个人手里拽走，而这个任务既不知道
那封信，也不知道有人切过去了。

**在人批准过的那一组里重排，是那个人委派出去的选择；把屏幕从正在被使用的那扇窗抢
回来，不是。** 这两件事看起来都只是「改变前台」，实际上分属两边：前者的每一个候选都
被逐个看过并点过同意，后者一个字都没被问过。

代价是实打实的：**人一碰别的窗口，任务就停住**。这不是副作用，是这条规则的正面
含义——它把「人正在用这台机器」当作比「任务要往下走」更高的优先级。想解开的人有一条
正当的路：让人把那个应用也批准进来。

拒绝文案里**不说最前面的是谁**。这一条拒绝恰好只在「最前面的应用没被批准」时触发，
所以一个会点名的拒绝，等于把每一次被拒的激活变成一次「此刻这个人在用什么」的读数
——那正是 allowlist 要挡的东西，而且是一个比被拒的那个更大的能力。同样的理由，
`list_granted_applications` 只说「有没有一个名单里的在最前面」，不说没有的时候是谁。

### 2.3 激活不由 tier 管，由名单管

`_ALLOWED` 里没有 `activate`，就像里面没有 `screenshot`。理由是同一个：

**激活不合成任何输入。** 它把一扇窗挪到前面，之后要对它做什么，会**再过一遍**完整的
门禁，按那时候的前台应用重算 tier。把浏览器切到前台买到的，恰好就是「可以看它」——
而看它本来就是批准它的目的。一个只能读的应用，被切到前台之后仍然只能读：
`test_activation_is_not_tier_gated_and_still_buys_nothing_extra` 钉的就是这一条。

反过来做（按 tier 卡激活）会得到一条说不通的规则：一个 tier `read` 的浏览器
可以出现在截图里，却不能被挪到最前面去截得更清楚。

### 2.4 `_ALLOWED` 收窄到工具够得着的动作

第三处缺口，形态和前两处不同：它不是少了东西，是**多写了东西**。

```
_ALLOWED["click"] 里有 mouse_move   ScreenGate 没有 move 方法，server 没有对应工具
_ALLOWED["full"] 里有 mouse_move、drag             drag 连 ScreenPort 方法都没有
ScreenPort.move                    唯一的调用者是 DarwinScreen.scroll 调它自己
```

**这不曾让任何东西出错**——一条没人执行的动作的许可，拒绝不了任何东西，所以它安安静静
活过了 ADR-070 和 ADR-076 两轮。它仍然是错的，而且错在两个方向上：

- **正着读是一句声称。** 一个来查「这个项目能对屏幕做什么」的人，在 `full` 那一行
  看见 `drag`，得出的结论是这个项目会拖拽。按 `known-gaps.md` 的四分类，这是
  **口径不实**——和 F-26（`write_tools_require_approval` 读起来像保证）同一种形状。
- **倒着读是一次预付。** 下一个想加拖拽工具的人，会发现它在 `full` 里**已经被允许了**，
  于是那场「该不该有拖拽」的讨论不会发生——表看起来已经替人做过了。

所以按 known-gaps 对「口径不实」的处理方式办：**立刻修，不排期**。二选一里选收窄
而不是实现，理由在 §4。

一起删掉的还有 `ScreenPort.move`：一个没有 port 之上调用者的 port 方法，是同一种
声称往下一层的复制。它现在是 `DarwinScreen._move`，私有，只被 `scroll` 调用——
CGEvent 的滚轮事件不带坐标，落在光标当前所在的位置，所以滚动之前确实要先定位一次。

**这一条不必靠推想，它在本 ADR 落地的同一周就发生了一次。**
[ADR-090](./0090-a-coordinate-carries-the-screen-it-was-measured-on.md) 重写
`ports/screen.py` 的模块注释，讲清两个坐标空间时写下的是：「`capture` 是某一块显示器
的……；`click`、**`move`** 和 `scroll` 把事件发进横跨所有显示器的**全局**空间」——一句
认真的、正确的、关于一个**从来没有任何调用者**的方法的坐标空间的规格。写它的人查了
这个方法、判断了它属于哪个空间、把它和另外两个并列写进了一份新的设计文档。

这正是本节说的「正着读是一句声称」被读到的样子，而读它的不是外人，是这个仓库里下一个
认真写文档的人。两批相隔几天、各自独立：**一批在删它，另一批在给它写规格。** 合并时
那段散文整段留下，只把它点名的方法改成 `click` 与 `scroll`。

两条测试从两侧钉住这件事，缺一条都不够：
`tests/domain/test_computer.py::test_no_tier_permits_something_this_project_cannot_do`
钉住那两个名字不再出现；
`tests/apps/test_computer_gate.py::test_every_permitted_action_is_one_the_gate_actually_performs`
把 `_ALLOWED` 的全部名字与真正驱动 gate 的调用对成一张表，并**逐个真的调用一遍**
（对着一个 tier `read` 的浏览器，因为 `refusal()` 会把它拒绝的动作名写进文案）。
前者防的是往表里加名字，后者防的是 gate 那侧不再产生某个名字。

### 2.5 `list_granted_applications`：`grants()` 早就在了，只是没有出口

`ScreenGate.grants()` 从 ADR-070 起就存在，没有任何工具暴露它。模型想知道自己有哪些
权限，只有一条路：再调一次 `request_access`——**而那会再弹一次对话框给人看**。

一个人被问第二次，第二次就不读了。所以这个缺口不只是不方便，它在消耗同意本身的
质量：ADR-076 §2 把「弹一次、逐个显示 tier」立成决定，然后留下一个让模型有理由
反复弹它的工具面。

这个工具**不产生任何副作用**：不显示给任何人看，不开对话框，不动屏幕。它多答一件事
——名单里有没有一个此刻在最前面——因为那正是其它每个工具会栽的那道检查，而这一件事
按 §2.2 的规则只答有无、不答是谁。

## 3. 分层：没有新的一层

```
domain/computer.py     _ALLOWED 收窄 · 四条激活拒绝文案      纯函数，测试不需要屏幕
ports/screen.py        activate(bundle_id) -> Identity|None  · move 删除
adapters/memory/screen.py  installed / activation_lands 两个新的可编程维度
adapters/screen/darwin.py  NSRunningApplication + 有界轮询
apps/computer_mcp/gate.py  activate（两道检查）· frontmost_grant
apps/computer_mcp/server.py  两个薄壳
```

### 3.1 端口返回「之后谁在最前面」，不返回「成功了没有」

`activateWithOptions:` 是**给窗口服务器发一个请求**，不是一次函数调用。它一发出去就
返回，重排由窗口服务器自己排期——所以紧接着读一次 `frontmost()` 可能仍然读到旧的那个，
而一个信了它的 gate 会拒绝一次它自己刚刚批准的请求。

所以端口方法在自己内部做**有界轮询**，然后把「之后到底谁在最前面」原样交出来。让调用
方自己再读一次是同一次读数做两遍，中间还夹着一个竞态。

`None` 是另一件事，而且窄得多：**没有任何在运行的应用带这个 bundle id**。它和「激活了、
但别的东西在最前面」分开，是因为这两件事要给模型的句子不同——一个的解决办法是请人打开
它，另一个的解决办法是去看一眼屏幕。

**这两个数字（2 秒上限、20 ms 轮询）是本文件里唯一没有实测的数字**，并在代码注释里
如此标注。`computer-use` extra 不在写这一批的 checkout 里，而这个测量也不是能悄悄做的：
它意味着反复把窗口拽到一个人正在看的屏幕最前面。所以它们取得宽松而不是精确——猜错了
只会让一个如实的回答慢一点，不会让它变成错的回答（到点仍然如实回报「别的东西在最前面」）。

### 3.2 假实现新增的两个维度，都是真屏幕不肯配合的事

`FakeScreen` 从 ADR-070 起就有一个真屏幕给不了的能力：让前台应用**在两次调用之间变**。
本 ADR 加的两个是同一类：

- `installed`：这台假机器上**跑着**哪些应用。空的就是「批准了但没打开」。
- `activation_lands`：激活到底有没有生效。`False` 站的是模态表单、全屏 Space、
  或者一个不肯到前面来的应用——真窗口服务器偶尔会给出、但没法点菜的那种情况。

`installed` 存的是完整 identity 而不是 bundle id 列表，因为 `activate` 得拿一个
identity 作答；如果假实现自己编一个名字，测试会在真适配器把模型自己给的字符串原样
回显的情况下照样通过。

## 4. 被拒绝的方案

**`open_application`：找不到就启动它。** Claude Desktop 那个工具会启动应用，本 ADR
**只激活，从不启动**，工具因此也不叫 `open_application`。

启动一个进程，和把窗口重新排个序，不是一个量级的行为：它会执行那个应用启动时做的
任何事（同步邮箱、恢复上次的文档、连服务器），而且撤不回。更要紧的是**人批准的那份
名单不是这个意思**：对话框问的是「可以在这次会话里控制下列应用」，一个人看着
「Notes、Word」点同意，同意的是控制它们，不是同意「在我机器上把它们开起来」。

代价如实记在 **known-gap F-29**：两个应用必须都已经开着，任务才跨得过去。
重开条件也写在那里——它需要的是对话框上一句人能读懂的话，而不是一次实现。

**批处理原语（`computer_batch`）。** ADR-076 §4 已经拒过，理由（把授权单位从一个动作
塌缩成一个数组、Policy Gateway 只看得见一次提案、审计线索被压平）一条没变。本 ADR
不重开它，而且新增的 `activate_application` 让那条拒绝更硬了一点：如果批处理存在，
「激活 + 点击 + 打字」会是一次提案，而**改变前台**和**在新前台上打字**恰恰是最需要
分开授权的两件事。

**按 tier 卡激活（比如 `read` 的应用不许被切到前台）。** §2.3 说过：那会得出一条
说不通的规则——可以截它的图，不可以把它挪到前面去截得更清楚——而它拦不住任何东西，
因为激活之后能做什么本来就要重新过一遍门禁。

**把「哪个应用在前台」做成可枚举的**（比如一个 `list_running_applications`）。
ADR-076 §3 与 §6 拒绝过同一件事的另一个形态，理由原样成立：那是把一个**比被拒的那个
更有意思的能力**交给模型。本 ADR 因此在两个地方都只答有无、不答是谁。

**实现 `mouse_move` 和 `drag`，而不是把 `_ALLOWED` 收窄。** 这是 §2.4 那个二选一的
另一半，拒绝的理由是**没人要**：`drag` 要新增一个 port 方法、一份 CI 装不了的 darwin
实现和一个新工具，`mouse_move` 单独存在时买到的只有 hover 态。真需要的时候按正常流程
加就是了——收窄之后那场讨论会真的发生，而这正是收窄的目的。

**把这条收窄（§2.2）做成配置项**，比如允许某个部署关掉它。ADR-076 §6 拒绝
`computer.auto_approve` 的那句话原样适用：**一份配置不知道现在最前面的是什么。**
「人正在用一扇不属于这个任务的窗口」是关于此刻的事实，写在部署时的一行开关表达不了它。

## 5. 后果

**工具面 6 → 8**，`config.computer-local.toml`、`scripts/dev.sh computer-check` 与
`tests/config/test_local_computer_profile.py` 三处的清单同步。那三处仍然逐个点名而不是
数个数：一个只上来七个工具的 server，是同一个别名下的另一个部署。

**ADR-075 那条拒绝原封不动。** 两个新工具同样在 `retryable_effects = false` 的 server
上，同样进不了 Task 授权信封，同样不进 Worker 的注册表。`activate_application` 尤其
不该进——重放一次激活，是在一个完全不同的时刻改变某人的屏幕。

**同意对话框多了一句话**，因为它要收的同意变了。批准三个应用现在还意味着「并且它可以
在这三个里挑一个放到前面」，而一份名字清单本身说不出这件事。那句话把边界一起带着说
（不在名单里的窗口在最前面时，包括切换在内的一切都会被拒绝）——安心和授权是同一件
事实的两半，只给前一半的对话框，收的是另一件事的同意。

**F-19 不受影响。** 批准仍然是进程级而不是 MCP 会话级的。

**这一批的证据只能是本机的，而且这一批连本机的都不完整。** CI 不装 `computer-use`
extra，而写这一批的 checkout 里也没有装——所以 `adapters/screen/darwin.py` 里新增的
`activate` **没有在真机上跑过**，`tests/apps/test_computer_darwin.py` 在这里是
skipped。按仓库的能力阶梯，激活这条路停在 **Tested**（假实现下的门禁行为全部有测试），
**没有到 Demonstrated**。这一句写进 `status.md`，不写成「已验证」。

## 6. 重审条件

- **有人为「任务停在人切走的窗口上」抱怨到值得改。** 那时要回答的不是「要不要放宽
  §2.2」，而是「人怎么在当下把这一次的授权给出来」——一个当场的提示，而不是一个开关。
- **`drag` 或 `mouse_move` 真的有用例。** 那时按正常流程加：port 方法、gate 方法、
  工具、`_ALLOWED` 一行，四样一起。§2.4 收窄的目的就是让这四样必须一起出现。
- **视觉通路做出来了**（ADR-076 §4 拒的那条，需要它自己的 ADR）。模型看得见截图之后，
  「哪扇窗在前面」会从一个要靠工具回报的事实变成一个看得见的事实，`frontmost_grant`
  多答的那一件事可能就不必再答了。
