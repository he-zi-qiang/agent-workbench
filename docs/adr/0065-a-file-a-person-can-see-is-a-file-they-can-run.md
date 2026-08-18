# ADR-065：看得见的文件就该跑得起来

- 决策点：控制台里一个 Agent 写出来的 `.py`，读者要怎么知道它干了什么——
  当纯文本读（现状）、由读者复制到别处跑、还是花一个模型回合请 Agent 跑一遍
  并把输出贴回来；若控制台自己能跑，闸门建在哪一层
- 状态：**接受**。新增 `POST /v1/code/sessions/{id}/workspace/{name}/run`，
  用 Code 会话已经连着的那个 sandbox MCP 跑一次 `runpy.run_path`；
  `SandboxRunTool` 里"工作区进、工作区出"的那一半拆成 `WorkspaceSandbox`
  给两个调用方共用；前端 `.py` 走新的 `PythonPreview`（源码 / 运行结果 两格），
  **不**新增第六个 `PreviewKind`
- 日期：2026-08-17
- 影响：`adapters/tools/sandbox.py`（拆出 `WorkspaceSandbox` / `SandboxOutcome`
  / `SandboxRefusedError`）、`application/code_session.py`（`workspace_session`
  与三个新拒绝类型）、`apps/api/routes/code.py`（新路由）、
  `apps/api/dependencies.py`（`SandboxSlot.runner`）、`apps/api/main.py`
  （状态码表 +4 行）、新 `web/src/components/PythonPreview.tsx`、
  `web/src/components/media.ts`（`isRunnablePython`）、
  `FilePreview` / `CodeTurn` / `PreviewPanel` / `CodePage` 各接一条回调
- 依赖：ADR-029（沙箱是纯函数，不是 shell——本 ADR 全部安全论证都挂在它上面）、
  ADR-057（Code 会话可以被授予 `sandbox_run`）、ADR-058（闸门从人移到信封）、
  ADR-062（产出的 HTML 在空 origin 里运行——本 ADR 是它留下的不对称的另一半）、
  ADR-044（loopback 身份模型）

## 1. 背景：ADR-062 修好了一半，另一半更像产品

ADR-062 让 Agent 产出的 HTML 页面在控制台里**跑起来**：点开就是渲染，源码
在切换的另一格里。它解决的是"交互式页面唯一的验收方式是运行"。

同一句话对 `.py` 更成立，而 `.py` 什么都没得到。编码会话的产物**通常是程序**
——本地实测的会话列表里，`.py` 与 `.html` 各占一半——而一个 `.py` 在控制台里
只能被读。想知道它干了什么，读者的选项是：

1. 下载下来，在自己机器上跑（那台机器不一定有 Python，也不一定该跑陌生脚本）；
2. 再花一个模型回合，跟 Agent 说"跑一下 sq.py 把输出贴给我"。

第二条是实际发生的事，它的代价是本地实测的 7–9 秒、四次模型调用、一次 token
账单，换一个容器一秒就能给出的答案。而且它绕远：Agent 早就能跑
（`sandbox_run`，ADR-057/058，本地 demo profile 默认放行），跑的正是同一个容器
——只是那条路必须经过一个模型。

**能力已经在进程里，缺的只是一条不经过模型的路。**

## 2. 决定：读者选的是"哪个文件跑"，不是"跑什么代码"

新路由收不到脚本。请求体是空的，要跑的文件名在路径里，脚本由服务端拼：

```python
_ENTRY_SCRIPT = (
    "import runpy, sys\n"
    "sys.argv = [{name}]\n"
    "runpy.run_path({name}, run_name='__main__')\n"
)
```

这一点是整条决策的重心，写下来是为了将来有人想加个 `body: {script}`：
**这不是 REPL，不是 shell，也不是"在会话里执行任意代码"的接口。** 它能跑的
只有这个会话工作区里已经存在的、这个 principal 自己拥有的、他正看着的那个
文件。攻击面因此与"读者能不能写文件到自己的工作区"完全重合——而那是上传按钮
和每一次 `workspace_write` 早就给了的。

`runpy` 而不是把文件正文当 script 直接送：沙箱把收到的 script 写到
`/sandbox/script.py` 再执行，正文直送的话 traceback 里的文件名是读者从没见过
的那个；`run_path` 保住他点的那个名字和能数得出来的行号。`sys.argv` 一并设上，
因为读 `argv[0]` 的脚本该看到自己。真实容器实测（2026-08-17）：

```
  File "sq.py", line 3, in <module>
    print(helper.square(undefined_name))
                        ^^^^^^^^^^^^^^
NameError: name 'undefined_name' is not defined
```

`run_path` 之前那两行都是**被真实容器逼出来的**，写在这里因为它们看起来像
能删掉的样板：

- `sys.path.insert(0, "")`。没有它，一个第一行写 `import helper` 的 `sq.py`
  会 `ModuleNotFoundError`，而 `helper.py` 就在旁边。两件事凑在一起：
  `run_path` 对普通文件根本不动 `sys.path`，而沙箱跑的是 `python -I`，
  隔离模式蕴含 `-P`——一个目录都不预置。`python sq.py` 预置的是脚本所在目录，
  在这里恰好就是工作目录。
- `sys.dont_write_bytecode = True`。把上一条修好之后，import 成功了，**这一次
  运行仍然被拒**——拒它的是输出收集器：`output_unsupported: '__pycache__' is a
  directory; the working directory is flat`。那条拒绝是对的，本 ADR 不绕过它
  （ADR-029 让传输保持扁平，拒绝目录而不是跳过目录，正是脚本产出不会悄悄消失
  的原因）；错的是根本不该产出那个目录——一次性容器里没有任何东西会再读它。

**整份工作区跟着进容器**（目标文件排第一，超过条数或字节上限的**逐个报出来**
而不是默默丢掉）。理由是 `sandbox_run` 的 `not_found` 分支已经写过的那条：少
一个输入的脚本会在自己内部某处失败，回来的 traceback 一个字都不会提到真正的
原因；控制台若说不出是哪个文件没进去，就等于把这次失败报成了脚本的 bug。

### 三道拒绝，三个不同的答案

这条路径**前面没有 Policy Gateway**——没有信封，没有 step，没有 tool call，
Agent 走同一个能力时经过的那道闸门，在这里只能由路由自己当。所以：

| 情形 | 状态码 | 为什么不是别的 |
| --- | --- | --- |
| 部署没开沙箱 | 503 | 文件在、调用者有权看、请求没错。404 会读成"没这个文件" |
| principal 没有 `sandbox:run` | 403 | 这一条**确实**是调用者的问题 |
| 名字不是 `.py` | 422 | 不在这里拦，一个 `.md` 会进容器再回来一个 SyntaxError |
| 沙箱/工作区拒绝 | 409 | 带着拒绝方自己的话。本进程没坏，是对面不肯 |
| 脚本跑了并且失败 | **200** | 退出码和 traceback 就是读者点这一下要的答案 |

最后一行是产品判断而不是 HTTP 洁癖：把非零退出码包成错误，等于用一个红框盖住
读者唯一想看的东西。

## 3. 共用的那一半，与它为什么必须共用

`SandboxRunTool.handle` 原本一个函数里做四件事：从工作区读输入、调远端、把
输出逐个绑成新的工作区版本、把结果讲给模型听。前三件与"谁在问"无关，第四件
完全取决于谁在问。于是前三件拆成 `WorkspaceSandbox.run(...) -> SandboxOutcome`，
tool 与路由各自负责第四件。

不拆会怎样，说具体一点：路由那份复制品迟早会在某一条上与 tool 那份分叉——先
写哪个版本、部分成功报不报、`inputs` 为空时省不省字段。**同一个工作集上两套
规则**，而分叉的那天没有任何测试会响。这也是为什么 `written` 的语义（只记
真正落库的名字，ADR-063）留在共用的一半里而不是各写各的。

拆出来的类住在 `adapters/`，不是 `application/`：`MCPClientPort` 是适配层的
东西，`tests/architecture/test_dependency_boundaries.py` 不允许 core 碰它。
路由属于 `apps/`，是外层，直接用适配层合法。

## 4. 前端：不加第六个 PreviewKind

`previewKind` 是**所有**展示文件的界面共用的词表——Work 的产物面板也读它，
而那里没有工作区可以跑东西。把 `python` 加进枚举，等于逼每一个界面回答一个
只有其中一个能回答的问题。所以 `.py` 在别处仍然是 `text`，Code 的
`FilePreview` 在 text 臂前面多问一句 `isRunnablePython(mediaType, name)`。

问名字也问类型：本项目自己写出来的 `.py` 带 `text/x-python`，读者上传的那份
带浏览器猜的任何东西（多半 `text/plain`），而上传的脚本和写出来的脚本一样该
能跑。服务端用同样两条规则再判一次——客户端的判断是交互提示，从来不是授权。

**跑是点出来的，不会自动发生。** 这是与 `HtmlPreview` 唯一实质的不同：那边
"展示产物"就等于"运行它"，代价是一次绘制；这边代价是服务端起一个容器，替读者
花掉它是不对的。所以默认那一格是源码。

## 5. 被拒绝的方案

**让"运行"按钮发一条指令给 Agent**。零新接口，复用全部现有闸门。拒绝的理由是
它花一个模型回合（实测 7–9 秒 + token）去做容器一秒的事，而且输出落在对话里
而不是文件旁边——读者要的是"这个文件干了什么"，不是"关于这个文件的一段话"。

**在浏览器里跑 Python（Pyodide）**。不需要服务端，也不需要新授权。拒绝的理由
是产物页面的 CSP 与自包含约束不允许外部资源，而把几 MB 的 wasm 塞进 bundle 是
为一个次要视图付主要代价；更要命的是它跑的不是 Agent 跑的那个环境，两边结论
不一致时没人说得清该信哪个。

**通用的 `POST /run {script}`**。这才是 shell，ADR-029 §3 拒绝过一次的东西
换个门进来。读者选文件而不是选代码，是这条路由与那条路由的全部区别。

## 6. 代价与未做的

- **没有自动化的端到端测试。** 这条路径要真容器运行时，而 CI 的 `quality` job
  离线跑（同 known-gaps E-03）。测试里站在沙箱位置的是一个返回真实 envelope
  形状的假 `MCPClientPort`——项目侧那一半（读输入、入口脚本、输出绑版本、四种
  拒绝）全在测试里，服务器自己的契约在 `tests/mcp`。本地实跑证据记在
  `docs/status.md`。**这个缺口不是理论上的**：上面 §2 那两行是真容器打回来的，
  两次都是假 client 复现不出来的东西，而且第二次（`__pycache__`）根本不在脚本
  执行阶段，在输出收集阶段。加这条路径上的任何新参数，都该再对着真容器跑一遍。
- **一次运行没有事件流。** turn 有 step 流可看，这个没有：它是一次几秒的调用，
  为它开一条流是给一个不会有人盯着看的进度条建管道。超时由沙箱自己的 wall
  clock 兜。
- **断开连接不取消。** 唯一还能取消的区段是"把脚本产出的文件存回工作区"，而
  读者关掉标签页不是半途丢下这些文件的理由。
- **工作区超过上限时按列表顺序取。** 没有"脚本大概会读哪些"的启发式，也不打算
  有——被漏掉的名字直接报给读者，让他删掉几个再跑，比猜一个错的强。
