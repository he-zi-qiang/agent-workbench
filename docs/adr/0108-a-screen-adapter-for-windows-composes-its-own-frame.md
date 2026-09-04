# ADR-0108：Windows 的屏幕适配器自己合成那张图

- 状态：Accepted
- 日期：2026-09-03
- 关联：ADR-070（权限是关于一个窗口的——四道检查、tier 表、截图预算**一字不改**，本 ADR 只加
  第二个碰屏幕的薄模块）、ADR-076（没人批准过的窗口不在那张图里——`exclude_native` 那个词
  在 Windows 上怎样兑现是本 ADR 的核心，§3.1）、ADR-091 §2.3（激活不合成任何输入——本 ADR
  因此拒绝 Windows 上「敲一下 Alt」的常见把戏，§3.3）、ADR-092（macOS 上 server 必须是
  .app——Windows **没有**对应的四个条件，§3.3）、ADR-095（人可以看自己的屏幕——控制台那一页
  在 Windows 上怎么读到 `/session`，§3.5）、ADR-0105 §4.1（「补不上」——被本 ADR 推翻）

## 1. 背景

ADR-0105 把 computer use 写成「不是暂未支持，是这条拓扑里补不上」，依据是 `computer-use`
extra 的三个依赖全带 `sys_platform == 'darwin'`，`adapters/screen/__init__.py` 在非 darwin
直接抛错。

那句话说的是**容器**拓扑，而且对容器拓扑永远成立：一个 Linux 容器够不着 Windows 的桌面。
它没说的是：屏幕适配器从 ADR-070 起就被设计成「一个平台一个薄模块」，四道检查、tier 表、
预算、坐标换算全在 `domain/computer.py` 里、不碰屏幕、跨平台可测；缺的只是**第二个薄模块**，
和一种把它放到桌面所在的那台机器上跑的办法。

## 2. 决定

**新增 `adapters/screen/win32.py`，用 ctypes 直接对 user32 / gdi32 / dwmapi / shcore 说话，
不加任何新的平台绑定；那张图由适配器**自己**从逐个渲染的已批准窗口合成；同意对话框是
`MessageBoxTimeoutW`；server 在 Windows 主机上原生跑（`scripts\computer.cmd`），API 容器
经回环隧道读它的 `/session`。**

具体：

1. `adapters/screen/win32.py`：`Win32Screen` 实现 `ScreenPort` 的每一个方法。
   `adapters/screen/__init__.py` 按平台分派。
2. `domain/computer.py` 多一张 `_KIND_BY_EXECUTABLE` 表：Windows 的精确身份是可执行文件名
   （小写），与 bundle id 那张表并列、永不相交；拒绝文案的第三段把 PowerShell 与 SendKeys
   加进「永远不要用」的清单。
3. `apps/computer_mcp/consent.py` 按平台分派：darwin 仍是 osascript，win32 是
   `MessageBoxTimeoutW`（默认按钮是「否」），其余平台抛 `ConsentUnavailableError`。
4. `pyproject.toml` 的 `computer-use` extra 多一行 `pillow; sys_platform == 'win32'`。
5. `scripts\computer.cmd`：Windows 上起 server 的入口（只需要 uv）；`compose.yaml` 的 api
   加 `extra_hosts: host.docker.internal:host-gateway`，`docker/run-api-local.sh` 起一条
   `127.0.0.1:8768 → host.docker.internal:8768` 的隧道。

## 3. 为什么是这些做法而不是别的

### 3.1 `exclude_native` 在 Windows 上靠合成兑现，不靠过滤

ADR-076 把 gate 收窄到只接受 `exclude_native`：未批准窗口的像素**从未被画出来过**。
macOS 靠 `SCContentFilter` 在合成器那一层做到；Windows 没有等价物——
`Windows.Graphics.Capture` 要 WinRT 投影（新依赖），`BitBlt` 整屏再涂黑正是 ADR-076 拒绝的
`exclude_mask`。

Windows 有的是 `PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)`：让合成器把**一扇窗**渲染进调用
方自己的位图，被遮挡也照样。所以适配器枚举顶层窗口、只留可执行文件在 allowlist 里的、逐扇
渲染进各自的缓冲区、按 z 序贴到一张空画布上（`compose_frame`）。未批准的窗口**没有被渲染、
没有被读取、不在这个进程持有的任何缓冲区里**——这是 `exclude_native` 那个词许下的承诺，
用「从不去要整屏」而不是「要了再过滤」来守。

差别要说出来：一扇**被**未批准窗口挡住的已批准窗口，在这里会完整出现，在 macOS 上会被
盖住。方向是「已批准的窗口露得更多」，永远不是「未批准的窗口露出一点」。画布底色是纯灰而
不是壁纸：壁纸不是窗口，没有 allowlist 点过它的名，而一个人的桌面图片是他自己的。

`compose_frame` 是纯函数，`tests/apps/test_computer_win32.py` 在 POSIX 上断言画布只含被
交给它的东西——这是 Windows 抓屏唯一一条安全相关的性质，它因此在 CI 跑的机器上被测，而不是
只在一台 Windows 上。

### 3.2 物理像素就是点

port 只说点、不说像素，理由 `ports/screen.py` 讲过：两次换算里任何一次错了，点击就落在两倍或
一半的位置。Windows 上决定「Windows 报什么坐标」的是进程的 DPI 感知：默认感知下一台 150 %
的 1920 面板报 1280 宽，`SetCursorPos(1000, …)` 落在真实的 1500 处。所以构造函数第一件事是
`SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`，之后每个矩形、每个坐标都是该显示器
的物理像素——点与像素在这个平台上成为同一个单位，`Display.scale_factor` 只是一个描述
（`dpi / 96`），没有人拿它做除法。

### 3.3 激活：不敲 Alt，因为激活不许合成输入

Windows 也锁前台：一个进程只有在它是前台进程、由前台进程启动、或收到过最后一次输入时才能
`SetForegroundWindow`。民间做法是先 `keybd_event(VK_MENU)` 敲一下 Alt 骗过这条规则。
本 ADR **拒绝**它，理由是 ADR-091 §2.3：激活之所以不受 tier 管，正因为它不合成任何输入；
一次 Alt 是一次落进当前前台窗口的按键，而那扇窗可能是没人批准过的。

做的是：`ShowWindow(SW_RESTORE)`（最小化的话）→ `SetForegroundWindow` → 轮询 `frontmost()`
最多两秒；不成就 `AttachThreadInput` 把本线程的输入队列挂到前台线程上再试一次（这是文档记载
的「以那个线程的身份行事」，不合成任何事件）；再不成就如实报「没到前面，现在在前面的是谁」
——和 darwin 一样的诚实结尾。macOS 的四个条件（bundle、签名、辅助功能、run loop）Windows
一个也没有，所以 ADR-092 那个 `.app` 在这里没有对应物，server 是一个普通进程。

**这段没有实测。** 本 ADR 写于一台 Mac，Windows 前台锁在不同版本上的行为是出了名的不一致。
`_ACTIVATION_TIMEOUT_SECONDS` 与 `darwin.py` 的同名常量一样标了「未量」。

### 3.4 输入被数过，不被假定

macOS 上 `CGEventPost` 在没有辅助功能授权时返回成功、什么也不做，ADR-070 §4 为此写了一整段。
Windows 上对应的静默失败是 UIPI：前台是提权窗口而 server 没提权时，`SendInput` 插入 0 个事件
并返回 0。适配器每次都比较返回值与请求数，少了就抛 `ScreenUnavailableError` 点名 UIPI；
打字那条路径把它当作「送达了多少」返回，正好落进 `focus_lost` 那句文案。这一点比 macOS
**好**：至少能数。

### 3.5 server 在主机上跑，API 经隧道读它

容器够不着桌面，所以 `agent-computer-mcp` 在 Windows 主机上原生跑——这是 Windows 那条路上
**唯一**需要 Python 的地方，`scripts\computer.cmd` 通过 uv 要它（uv 会自己取一个 3.12），
所以要装的只有 uv。

控制台「计算机」页读的是 API 转发的 `/session`（ADR-095 §5），`api.computer_session_url`
被校验为回环。API 在容器里，主机上的 8768 不是它的回环。用 ADR-0107 同一条隧道：API 容器
里 `127.0.0.1:8768 → host.docker.internal:8768`，校验照过、Host 头照是 `127.0.0.1:8768`、
主机上的 server 照是 `--host 127.0.0.1` 起的。没起 server 时隧道逐连接断开，
`routes/computer.py` 报「没在应答」——和一台从没起过它的机器同一个答案。

`host.docker.internal` 是 Docker Desktop 给主机的名字（Windows 与 macOS 都有）；
`extra_hosts: host-gateway` 让同一个名字在 Linux 引擎上也解析。

### 3.6 同意：MessageBoxTimeoutW，默认「否」

`MessageBoxW` 没有超时，一个没人在机器前的请求会把工具调用挂到客户端自己超时为止、不留
一句解释——正是 macOS 路径用 `giving up after` 避免的事。`MessageBoxTimeoutW` 是 user32 导出
但未文档化的入口，XP 起就在、shell 自己在用；将来哪个 Windows 拿掉它，答案是「问不了」，
不是换一个对话框。

三条规则照抄 ADR-076 §2：文本是数据（这里更简单——它是函数参数，中间没有任何解释器让模型
挑的名字变成代码）；默认按钮是「否」（顺手敲回车的人拒绝了）；一切说不清的答案是拒绝
（超时 `MB_TIMEDOUT`、关闭、任何认不出的返回值）。`MessageBoxW` 不能给按钮改名，所以正文
最后一行写明「是 = 允许这次会话，否 = 拒绝」。

## 4. 后果

- Windows 上有 computer use 了，但它是**主机上的第二个入口**，不在 `stack.cmd` 里；
  `stack.cmd` 的首启摘要指向 `computer.cmd`。
- `known-gaps` E-03 的「从不在 CI 跑」名单多一个文件：`win32.py`（`test_computer_win32.py`
  只测它的纯函数半边）。
- 两条新的已知代价登记：被遮挡的已批准窗口在图里完整可见（F-35）；激活受前台锁限制且未实测
  （F-36）。
- **证据口径：Tested 的只有纯函数半边与同意的读法；碰屏幕的那一半是 Planned 落地成了代码，
  一次也没在 Windows 上跑过。** 本 ADR 里没有一个来自 Windows 的数字，因为没有。

## 5. 被拒绝的方案

**pywin32 / pyautogui / pynput。** 三者都能做，都多一个依赖，而且都在内部做本 ADR §3.2 要
亲手控制的 DPI 决定（pyautogui 用逻辑坐标）。ctypes 是标准库，每一个调用都写在这个仓库里。

**`Windows.Graphics.Capture`。** 真正的合成器级逐窗抓取，但要 `winrt` 投影包与 COM 互操作
的一整套；`PrintWindow` 在 Win 8.1+ 对 DWM 合成的窗口给出同样的逐窗结果。独占全屏的 DirectX
表面两者都拿不到。

**把 computer server 也放进容器、用 RDP / VNC 够主机。** 那是给容器一个远程输入设备，
ADR-044 与 ADR-070 都拒绝过的形状。

**Windows 上不做 computer use，只做沙箱与内存。** 使用者点名要它。代价在 §4，没有藏。
