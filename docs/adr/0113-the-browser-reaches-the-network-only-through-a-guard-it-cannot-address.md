# ADR-0113：浏览器只经一个它绕不开的守卫出网

- 状态：Proposed
- 日期：2026-09-12
- 关联：ADR-027 §3.2（`address_guard` 的 resolve-then-judge——本 ADR 把同一个判断挪到
  它第一次真正管用的位置）、ADR-0107（沙箱 broker：用拓扑而不是挂载来给边界定形——本
  ADR 用同一个手法，只是把「不挂 socket」换成「够不着网络」）、ADR-095（computer 页
  的只读反代 + 轮询——画面回流照抄这个形状，不发明第二种）、ADR-029（沙箱是纯函数，
  `--network=none`——本 ADR 说明浏览器为什么不能住进那里）

## 1. 背景

Code 会话的模型现在手里有 `workspace_read/write/edit/grep/list`、`sandbox_run`、两个
search 和 `delegate`。没有 shell。唯一的执行能力是 `sandbox_run`，而它是
`python:3.12-slim`、`--network=none`、tmpfs `noexec`、nobody、一次性。

于是模型可以写出一个 `mario.html`，可以把它落到工作区，**然后什么也做不了**。它无法
执行一行 JavaScript，无法知道那个页面加载时报没报错，无法知道跳跃弧线对不对。判断
「能跑」和「对不对」的差别，只能由人在 `PreviewPanel` 里用眼睛完成。

这不是疏漏，是设计的直接后果。而它的代价随着 Code 会话产出的东西越来越像真实 web
应用而变大：表单、路由、第三方 SDK、登录态——这些东西的「不对」天然长在浏览器里，
截图和可访问性树才有答案。

沙箱不是它该去的地方。tmpfs `noexec` 装不下一个 Chromium，`--network=none` 打不开
任何 URL，`python:3.12-slim` 里没有它。这三条任一都够呛，而它们每一条都是 ADR-029
的隔离锚点，不该为了塞进一个浏览器而松动。

## 2. 决定

**新增一个只跑浏览器的容器；它在拓扑上够不着网络，唯一的出口是一个我们自己写的、
逐请求过 `address_guard` 的本地代理；画面经只读反代回流到 `PreviewPanel`。**

具体是六件事：

1. `apps/browser_mcp/`：第五个项目自有 MCP server，`agent-browser-mcp`，回环绑定，
   六个工具（§3.4）。Chromium 由 Playwright 驱动，进新的 `browser` extra。
2. `apps/browser_mcp/proxy.py`：一个 HTTP/HTTPS 转发代理，**每一个请求与每一个
   CONNECT 都过 `adapters/research/address_guard`**。Chromium 用
   `--proxy-server=127.0.0.1:<port>` 指向它，无 bypass 列表。
3. `compose.yaml` 新增**两个**服务（§3.3）：`browser`（Chromium + MCP server，只接
   `internal: true` 的网络，**没有默认路由**，工作区卷只读挂入以支持 `file://`，
   `security_opt` 用 `docker/chromium-seccomp.json` 让 Chromium 保留自沙箱、
   **不加 `--no-sandbox`**）与 `browser-egress`（只跑守卫代理，同时接 `internal`
   与默认网络，是这套拓扑里唯一能出网的一环）。两者都不挂 key 卷、不读配置、
   不连数据库、不挂 socket。
4. `docker/loopback_proxy.py` 沿用「向内」用法：API 与 Worker 容器里各起一条
   `127.0.0.1:8773 → browser:8773` 的隧道。`--host` 的 choice 列表、MCP SDK 的 Host
   校验、settings 校验器三样一个字不改，理由与 ADR-0107 §3.4 完全相同。
5. `routes/browser.py`：照 ADR-095 的 `routes/computer.py` 写一个**只读**反代，
   `PreviewPanel` 多一张「浏览器」标签页，按秒轮询最新帧与状态。
6. 新 profile `config/config.browser-local.toml`，并入 `demo-local` 与 `compose-local`；
   `dev.sh` 新增 `browser-server`，进 `up` 的启动序，`demo-worker` 启动前照 `sandbox`
   的样子探它一次。

## 3. 为什么是这些做法而不是别的

### 3.1 守卫在工具层等于没有守卫

最先想到的做法是在 `browser_open(url)` 里调一次 `assert_permitted_name` / 
`is_public_address`，判完再交给 Chromium。**这个做法是错的，而且错得不明显。**

模型批准的是第一个 URL。页面加载之后，发出请求的不再是模型，是页面：子资源、
`fetch`/XHR、WebSocket、`<img src>`、meta 刷新、301 跳转、`window.location` 赋值。
一个通过了守卫的 `https://good.example` 里，一行

```js
fetch("http://169.254.169.254/latest/meta-data/iam/security-credentials/")
```

就把守卫绕干净了——它从来没被问过这个 URL。

而这正是 `address_guard` 开头那段话描述的威胁模型：模型的输入里含有检索到的网页
文本，网页文本不可信，一句话就足以把请求瞄准一个内部地址。浏览器没有引入这个威胁，
它把这个威胁的执行者从「模型」换成了「页面」，而页面不需要说服任何人。

所以判断必须下沉到**每一个请求都必然经过的那一层**，那一层是网络。

### 3.2 代理，而且是我们自己写的代理

Chromium 的 `--proxy-server` 让每一个出站请求（含 WebSocket 的握手、含子资源、含
页面自己发起的一切）变成对本地代理的一次 `CONNECT` 或一次明文请求。代理逐个判，
判不过就拒。页面里的 JS 没有任何办法绕过它——它不是一层检查，它是唯一的路。

这里有一句 `address_guard` 自己写下的话，第一次在这个方向上闭合：

> 在代理分支里，enforcement boundary 是代理，不是这个进程。

在 `web_mcp` 里那句话是在**削弱**保证：开发机上挂着一个第三方代理，我们的解析不是
权威，只能退化成判名字。在这里它是在**描述**保证：代理就是我们自己，判断和连接
发生在同一个进程里。

**而且这一版比 `web_mcp` 那一版更强，不是更弱。** `address_guard` 明写了自己关不掉
DNS rebinding：名字在守卫里解析一次，在 HTTP 客户端连接时又解析一次，两次之间答案
可以变。关掉它需要「连到已经检查过的那个地址，并把 hostname 放进 Host 头」——而这
恰好是一个代理天然在做的事：代理解析一次、判一次、然后自己连那个地址；浏览器只跟
代理说话，自己不做解析。所以这条路上 rebinding 是关着的。

代价说清楚：**HTTPS 的 CONNECT 只能按名字和解析结果判，代理看不见隧道里的内容。**
这不是要解决的问题（解决它意味着 MITM 整个 TLS），而是边界的形状：我们判的是
「能不能连到这个地方」，从来不是「传了什么」。

### 3.2b 开发机上还有第三种「谁解析」，实测撞上了

§3.2 说代理解析一次、判一次、连那个地址，并因此把 DNS rebinding 关掉了。那在容器里成立。
**在写代码的人那台机器上不成立，而且不是理论问题：** 2026-09-12 实测，这台 Mac 挂着
fake-IP/TUN 代理，`example.com` 解析到 `198.18.0.70`——一个**正确地**不可全局路由的地址，
于是守卫拒绝了它，浏览器一个站点也打不开。

这正是 `address_guard` 开头那段话描述过的情形，而本 ADR 的实现一开始没有用它给出的分支。
补上之后规则和那个模块一字不差：**先问谁解析这个连接**。

* 这个进程解析（容器路径，没有上游代理）——resolve-then-judge，§3.2 原样，rebinding 关着。
* 上游代理解析（`--upstream-proxy`）——**判名字**，然后把请求原样交给上游。

第二条分支买到的是「能用」，卖掉的要说清楚：**在这条分支上，enforcement boundary 是上游
代理，不是这个进程。** 判名字挡得住 `localhost`、单标签名和私有用途后缀（实测
`http://localhost:8000` 在这条分支上照样 403，理由是「这个名字从不离开本机」），挡不住一个
公网名字被上游解析到内网，rebinding 也不再关着。地址字面量仍走地址规则——这是让
`http://169.254.169.254/` 在有代理时依然被拒的那一行，有测试钉住。

**容器路径永远不走这条分支**：Compose 里没有任何一个代理变量，`browser-egress` 自己出网。
所以 §3.2 的强保证正好落在它被声称的地方，这一条是开发机上较弱的兄弟——与沙箱、与浏览器
自沙箱在两条路径上的差别，是同一种差别。

### 3.3 出口在另一个容器里，因为同一个容器没有两种路由

只给 Chromium 一个 `--proxy-server` 是不够的。那是一个命令行参数，一个能在容器里执行
代码的东西可以不用它；`--proxy-server` 也不覆盖每一种协议。所以要让「走守卫」不是一条
约定，而是拓扑上唯一的可能。

**本节初稿说的做法行不通，原因很具体：**它写的是「`browser` 服务接在 `internal: true`
的网络上，代理进程绑在同一容器的回环上，代理自己接一条有出口的网络」。同一个容器里的
两个进程共享同一个 network namespace——不存在一个有默认路由、另一个没有。那句话描述的
是一个做不到的容器。

改成两个容器：

* **`browser`**：Chromium + MCP server，**只**接 `internal: true` 的网络。它没有默认
  路由，`curl` 一个公网地址在这里是超时而不是 403。
* **`browser-egress`**：只跑守卫代理，同时接 `internal` 与默认网络。它是这套拓扑里
  唯一一个既能被浏览器看见、又能出网的东西。

Chromium 的 `--proxy-server` 指向 `browser-egress`。这样「浏览器只能经守卫出网」不是
配置出来的，是**画出来的**：把代理关掉，浏览器不是绕过它，而是什么也到不了。

多出来的一笔账要记清楚：`browser_diagnostics` 要报告被拒的目标，而判断现在发生在另一个
进程里。代理因此在 `internal` 网络上开一个只读的 `/decisions`，浏览器容器去取。这**不是**
一条新的信任边界——能到那个端点的正是本来就能把请求交给这个代理的同一批容器——但它确实
是一次跨进程读取，所以它有自己的失败模式：取不到时诊断说「取不到」，而不是说「没有被拒
的目标」。两者对模型意味着完全相反的事。

**原生路径（`scripts/dev.sh browser-server`）没有这个隔离，也不假装有。** 那里代理与
浏览器同进程，`--proxy-endpoint` 不给就用内建的那一个。守卫本身一字不差地照跑，少掉的
只是「绕不过去」那一层——与沙箱在原生路径上以使用者本人账号运行、而在容器路径上只剩
socket，是同一种差异，并且理由相同：原生路径是给写代码的人用的，容器路径是给部署用的。

### 3.4 六个工具，不是二十个

参照实现（Claude Desktop 的浏览器面板，Electron `WebContentsView` + 内置
`webContents.debugger` 打 CDP）暴露了二十来个工具。那是给一个通用浏览代理用的。
这里要的是验证，工具面按「验证一次需要什么」来定：

| 工具 | 做什么 | 底下是什么 |
|---|---|---|
| `browser_open` | 打开一个 URL 或工作区里的一个文件 | `page.goto`，返回标题、状态、加载期 console 摘要 |
| `browser_snapshot` | 可访问性树，元素带 `ref` | `Accessibility.getFullAXTree`，比截图省 token 且可断言 |
| `browser_eval` | 跑一段 JS，拿回 JSON | `Runtime.evaluate`——**验证的主力** |
| `browser_interact` | 一批点击/输入/按键 | `Input.dispatch*Event`，批量一次往返 |
| `browser_screenshot` | 一张图 | `Page.captureScreenshot`，只在像素才有答案时用 |
| `browser_diagnostics` | console + network + 丢弃计数 | 订阅 `Log.entryAdded`、`Runtime.consoleAPICalled`、`Runtime.exceptionThrown`、`Network.*` |

`browser_eval` 是这里唯一真正新的能力，也是回答「对不对」的那一个：它把运行中的
状态变成数字，数字能跟规格对账。截图只能证明「画了东西」。

**`retryable_effects = false`。** 与 `word` / `web` 不同：那两个是读和渲染，重放一次
得到等价结果；点一次按钮不是。图节点重放不得重放浏览器交互。

### 3.5 Chromium 保留自沙箱；挡路的是 seccomp，不是 capability

**这一节的初稿是错的，错法值得留下来。** 它写的是：Chromium 的 namespace sandbox 要
`CLONE_NEWUSER`，与 `cap_drop: ALL` 冲突，所以只能 `--no-sandbox`，代价是一个 RCE 直接
等于拿到这个容器。

`CLONE_NEWUSER` **不需要任何 capability**——对非特权进程可用正是 unprivileged user
namespace 的全部意义。真正拒绝它的是 Docker 的**默认 seccomp profile**。两者都以
"Operation not permitted" 出现，于是看起来像同一件事。

实测（Docker 29.4.0 / LinuxKit 6.12.76，硬化三样全程不动）：

| 配置 | `CLONE_NEWUSER` | 真 Chromium（**不加** `--no-sandbox`） |
|---|---|---|
| `cap_drop: ALL` + `no-new-privileges` + 默认 seccomp | DENIED | `Aborted`，exit=1 |
| 同上 + `seccomp=unconfined` | OK | 正常渲染 |
| 同上 + `docker/chromium-seccomp.json` | OK | 正常渲染，exit=0 |

内核侧 `user.max_user_namespaces` 是 15665，从来不是它拦的。而 `--cap-add=SYS_ADMIN`
之所以"看起来能修好"，是因为默认 profile 的那条规则本身就写着
`excludes: {caps: [CAP_SYS_ADMIN]}`——加 SYS_ADMIN 绕过的是 seccomp，不是解决了
capability 问题。那条路要付一整个 `CAP_SYS_ADMIN`，而它什么也没多买。

所以决定改成：**保留 Chromium 自沙箱，换一份只比默认多三个洞的 seccomp profile，
`cap_drop: ALL`、`no-new-privileges`、`read_only` 一个不动。**

三个洞由 `docker/make_chromium_seccomp.py` 生成，每一个都是让下一个系统调用通过的最
窄形式（详见该文件头部）：`clone` 的 namespace 掩码 `0x7E020000 → 0x0E020000`（放开
NEWUSER/NEWPID/NEWNET 三位，NEWNS/NEWCGROUP/NEWUTS/NEWIPC 照旧拒绝）；`unshare` 按
**同一个掩码**放开（zygote 走的是 `unshare(CLONE_NEWUSER)` 而不是 `clone`）；`chroot`
放开（进入 userns 后 zygote 持有 `CAP_SYS_CHROOT`，内核会放行，而 seccomp 是进程级
过滤器、不知道 namespace 这回事）。`defaultAction` 仍是 `SCMP_ACT_ERRNO`。

**这个差别值多少，说清楚。** 初稿的方案里，一个渲染器漏洞直接落在容器边界上。现在
它先要穿过 Chromium 自己的两层：layer-1 的 user/pid/net namespace + chroot，layer-2 的
渲染进程 seccomp-bpf（它只要 `PR_SET_NO_NEW_PRIVS`，而 `no-new-privileges: true` 恰好
就是在给它）。穿过之后才轮到这个什么也不持有、且没有默认路由的容器。多出来的这一层
是浏览器安全模型里被攻击得最多、也因此被加固得最多的那一层，不该白扔。

代价是两条，都不是免费的：profile 是**整份替换**而不是"默认加补丁"（Docker 没有后者），
所以它是一份会随 daemon 升级而变旧的快照——moby 的 tag 写在产物里，升级手续就是拿新
tag 重跑一次脚本；以及产物必须入库并被测试盯住，否则三个洞会在某次"顺手更新"里变成
第四个。`tests/deployment/` 断言掩码恰好是 `0x0E020000`、`unshare` 带着同一个掩码、
`defaultAction` 仍是 ERRNO。

### 3.5a 这一节曾经只写在纸上：Playwright 默认把沙箱关掉

2026-09-12 的容器验证里，§3.5 的论证差点原样留在纸上。

`LAUNCH_FLAGS` 里没有 `--no-sandbox`，旁边的注释还专门解释了它为什么不在那里。**那句话
对那个元组是真的，对命令行是假的**：Playwright 的 `chromium_sandbox` 参数默认 `False`，
它自己会把 `--no-sandbox` 加上去。

发现它的是一个本来只想做装配验证的对照组。在 Docker **默认** seccomp profile 下——那个
profile 拒绝 `CLONE_NEWUSER`，因此根本跑不了带沙箱的 Chromium——`launch()` 却愉快地起来
并渲染了页面。只有不带沙箱才可能做到这件事。

于是这一节的每一样东西——生成的 profile、那三个刻意开的洞、上面那个 A/B——描述的都是一层
代码正在关掉的保护。而且什么都没报错：页面渲染正常，工具正常，测试全绿。**一层不存在的
防护看起来和一层存在的防护完全一样，直到有人攻击它。**

修法是一个参数：`chromium_sandbox=True`。加上之后，同一个默认 profile 的容器直接拒绝启动
（`Chromium sandboxing failed!`），而项目 profile 下正常起来并 eval 出 42 ——**这才是 §3.5
声称的那个 A/B**，上面那一版量的是 `chromium-browser` 裸命令，不是这个进程真正会走的路径。

`tests/deployment/test_chromium_seccomp.py` 现在断言这个 opt-in 存在、且 `--no-sandbox`
没有从前门回来。这条断言比 profile 本身更重要：profile 写错了会有人在容器里撞见，opt-in
漏了不会有任何人撞见。

**这一条只有容器能发现。** macOS 原生路径上没有 seccomp，Chromium 的沙箱走的是另一套
机制，两种配置都跑得起来——所以原生跑一百遍也验不出这件事。

### 3.5b Chromium 住在第二个镜像里，不在共享镜像里

ADR-0107 §3.3 把 Docker CLI 放进共享镜像，理由说得很清楚：一个静态二进制，而且没有
socket 的 CLI 是惰性的，所以别的容器带着它不花什么代价。**这两半论证在这里一半都不成立。**
Chromium 连同它链接的 X/GTK/NSS 库是几百 MB，而共享镜像是 `api`、两个 Task Worker、
`ingestion`、`sandbox`、`encoder` 和 `browser-egress` 都在跑的那一个——七个容器各背一个
它们永远不会启动的浏览器。

所以这里照 `docker/sandbox-pdf.Dockerfile` 的先例：**单独一个镜像、单独 build、在
`compose.yaml` 里按名字引用**。和那个镜像的区别是这一个**确实**派生自项目镜像——它需要
应用代码和 console script，而沙箱镜像必须刻意没有。

`browser-egress` 留在共享镜像上。它只跑 `GuardedProxy`，而「它不需要浏览器」正是 §3.3
的形状本身：**能出去的那个容器，不是渲染的那个容器。**

两个启动器都因此多一步，且都是 `docker build` 而不是 compose 的 `build:`——后者在这个
checkout 的中文路径下直接失败，报错里一个字都不提路径：

```
scripts/dev.sh browser-image        # 原生开发机（它自己的 browser-server 不用这个镜像）
scripts\stack.cmd                   # Windows：基础镜像之后自动多 build 这一个
```

这一条是 2026-09-12 的容器验证逼出来的：在那之前 `compose.yaml` 里 `browser` 服务写的是
`image: agent-workbench:local` 加 `build: context: .`，而根 `Dockerfile` 只装
`--extra embedding`——**镜像里既没有 playwright 也没有 Chromium，整条容器路径不可能起来**，
而所有单元测试都是绿的。

### 3.6 画面回流照抄 ADR-095，不发明第二种

`PreviewPanel` 要能显示浏览器正在看什么。已经有一个先例：computer 页经
`routes/computer.py` 的只读反代拿 `/session`，每 4 秒轮一次。

照抄它：`browser_mcp` 用 CDP `Page.startScreencast` 收帧，只留最新一帧；
`routes/browser.py` 只读反代，`PreviewPanel` 多一张标签页按秒轮询。**不引入
WebSocket，不引入新的推流通道**——那会给这套东西加第二种实时范式，而现有那一种
已经服务了一个同样形状的需求。

帧率不是目标。人要看的是「模型现在在看什么」，不是流畅回放。

## 4. 明确不做

- **不做人对浏览器的直接操作。** 回流是只读的。人要动手就自己开浏览器。允许人从
  面板里点，就要在两个操作者之间做仲裁，那是另一条 ADR。
  > **2026-09-13，[ADR-0117](./0117-a-person-may-drive-the-browser-while-no-turn-is.md) 按这一条
  > 自己开出的条件翻案**：仲裁规则是「有编码回合在跑时页面归模型，其余时候归人」，人的输入经
  > `POST /v1/browser/input` 作为一次 `browser_interact` 送进去，回合在跑时 409。§3.6 的帧路由本身
  > 仍然只读，`tests/architecture/test_browser_forward_is_read_only.py` 继续钉着它。
- **不做持久化 profile。** 没有 cookie 存续、没有登录态复用。一个会话一个上下文，
  结束即弃。带登录态的验证要先有一条关于凭据从哪来的 ADR。
- **不把浏览器给 Chat。** 只给 Code 会话与 Task 图节点，`audience` 照 `web` 的样子
  声明。
- **不做下载。** `browser_open` 不接受会触发下载的响应；要文件用 `web` 的
  `download_document`，它已经有自己的字节上限和媒体类型判断。

## 5. 影响

- 新 extra `browser`（Playwright wheel 本身很小；`playwright install` 拉的 Chromium 不小）。
  CI 的 `quality` job 照 `embedding` 的先例断言它**不被安装**；浏览器相关证据来自本地与
  Compose 运行，并标注为本地。
- **多一个镜像和多一个构建步骤**（§3.5b）：`agent-workbench-browser:local`，由
  `docker/browser.Dockerfile` 从共享镜像派生。`scripts/dev.sh browser-image` 与
  `scripts\stack.cmd` 各有入口；忘了它的症状是 `compose up` 找不到镜像，而不是一个
  跑起来却没有浏览器的栈。
- 十一个 profile 变十二个。每个 profile 各自冻结工具名，新工具要逐个过一遍。
  **schema 版本不动**：`docs/configuration.md` §2 的规则是「新 table 抬版，既有段下
  带默认值的新叶子不抬」，而 `[[mcp.servers]]` 是既有数组、`api.browser_frame_url`
  是既有段下的叶子——与 ADR-095 给 `api.computer_session_url` 的处置同形。本 ADR 的
  初稿写了 `1.19 → 1.20`，那是把「新增了东西」当成了「改变了形状」。
- Worker 的 MCP catalogue 在启动时冻结一次，所以 `browser-server` 必须进 `up` 的
  启动序，且 `demo-worker` 启动前探它一次——否则又是一个健康的 Worker 缺了它存在
  理由的那个工具。
- `docker/chromium-seccomp.json` 入库，由 `docker/make_chromium_seccomp.py` 生成；
  `tests/deployment/` 断言它的三处差异恰好是那三处（掩码 `0x0E020000`、`unshare`
  带同一掩码、`defaultAction` 仍为 `SCMP_ACT_ERRNO`），离线可跑。
- `tests/deployment/test_compose.py` 增断言：`browser` 服务接在 `internal` 网络上、
  不挂 socket、工作区卷是只读的、硬化三样俱全。
