# ADR-069：正在跑的脚本自己能说话

- 决策点：ADR-068 把工具执行期的进度信道接通了，但沙箱里那 300 秒仍然只有
  「executing in the sandbox」加一个跳动的秒数——**脚本自己 `print` 的东西一个字
  也到不了控制台**。要不要把它接出来；如果要，从容器里出来走哪条通道、跨 MCP 走
  什么协议、传输层要不要改
- 状态：**接受**。容器内的 bootstrap 把子进程两条流的增量**帧化后写在容器的
  stderr 上**（stdout 仍然只有信封，一个字节都不让），host executor 增量解析，
  沙箱 MCP 服务端翻成 `notifications/progress`，客户端 `call_tool` 收回调，
  `WorkspaceSandbox` 转成 `ToolProgress`。**沙箱 MCP 应用的响应从单个 JSON 文档
  改成 SSE**——这是让上面整条链从「理论上通」变成「真的通」的那一行。
  关闭 ADR-068 记下的那条缺口
- 日期：2026-08-18
- 影响：`apps/sandbox_mcp/_bootstrap.py`（`_run_child` / `_ProgressTail` /
  四个常量 / 子进程加 `-u`）、`apps/sandbox_mcp/executor.py`
  （`OutputSink` / `_read_records` / `_progress_record` / `run(on_output=)`）、
  `apps/sandbox_mcp/server.py`（`STDERR_PREFIX`、`report_progress`、
  `json_response=False`）、`adapters/mcp/client.py`（`ProgressSink`、
  `call_tool(on_progress=)`）、`adapters/tools/sandbox.py`（`_forwarding`）、
  `web/src/features/code/useCodeStream.ts`（`lines` 取代 `message`）、
  `CodeTurn.tsx`、`styles/app.css`
- 依赖：[ADR-068](./0068-a-running-tool-owes-the-reader-a-sign-of-life.md)（跑着的
  工具欠读者一个活着的信号——本 ADR 是它的下半段，用的是它建好的 `ToolProgress`
  通道，一个字段都没加）、[ADR-029](./0029-ephemeral-sandbox.md)（一次性沙箱：
  `--network=none`、无 tty、一次性容器——§2 里「为什么必须是容器的 stderr」整段
  由它推出）、[ADR-028](./0028-task-workspace.md)（截断的
  流是坏掉的流——本 ADR 的预览**不是**流，所以它可以被截断，§4）

## 1. 背景：ADR-068 记下的那条缺口，和它其实没那么远

ADR-068 落地时把这条写进了[已知缺口](../known-gaps.md)：MCP 有
`notifications/progress`，而本仓库的 `adapters/mcp/client.py` 只做请求/响应。

实测下来这句话只对了一半。SDK 的 `Client.call_tool` **本来就带
`progress_callback` 参数**，服务端 `ServerSession.report_progress` 也在，两头的
协议支持都是现成的。真正挡住的是另一件事，而且它一个字节的错误信息都不给。

### 挡住的是传输层，而且它是静默的

`create_app` 用的是 `json_response=True`。实测（两种模式各跑一遍同一个会在中途
报三次进度的服务端）：

| `json_response` | 结果 | 客户端收到的进度回调 |
| --- | --- | --- |
| `True`（改前） | `done` | **0 次** |
| `False`（改后） | `done` | **3 次** |

一次调用一个 JSON 文档，工具还在跑时抬起的通知**没有地方可去**——不报错，不警告，
就是没有。所以「客户端只做请求/响应」这条缺口描述本身也不够准，改对了客户端也不会
有任何变化，除非同时改传输。

### 容器那头还有一层，而且更容易被漏掉

沙箱不是「子进程的 stdout 就是脚本的 stdout」。按 `_bootstrap` 的设计，子进程两条
流都重定向到文件，容器的 stdout **只有信封**——这是「脚本 `print` 一个
`{"outputs": []}` 也伪造不了结果」的全部依据。

所以脚本的输出根本不在容器的 stdout 上，得另找一条路出来。

## 2. 决策：走容器的 stderr，因为只有它同时满足两条

需要一条通道，它必须同时是：**host 能增量读到的**，而且**脚本写不进去的**。

- 容器的 stdout：host 读得到，但它是信封。在信封前面插记录，就是拿
  「stdout 只有信封」这条保证换一点方便。**拒绝**。
- 容器的 stderr：host 已经在单独 drain 它，而子进程的 stderr 被重定向到了文件——
  **脚本够不着它**，bootstrap 是这条流上唯一的写入者。两条都满足。
- 第三条 fd / 一个文件 / 一个 socket：都要动 `ISOLATION_FLAGS`，而那份 flag 列表
  是 ADR-029 逐条论证过的。为一个预览去松它，代价和收益不成比例。**拒绝**。

于是记录是容器 stderr 上带前缀的一行 JSON：

```
@@sandbox-progress@@ {"channel": "stdout", "text": "line 0\n"}
```

伪造问题因此**不存在而不是被防住了**：脚本印一模一样的前缀，那行字会作为
`text` 被**引在一条真记录里面**——它是输出，本来就该被当成输出。有测试钉住
（`test_a_script_that_prints_the_marker_cannot_forge_a_record`）。

### 2.1 `-u` 是承重的，而且最容易丢

子进程从 `subprocess.run(..., timeout=)` 改成了 `Popen` 加轮询，这一步是明显的。
不明显的是同时加的 `-u`：

**Python 的 stdout 在指向文件而不是终端时是块缓冲的。** 没有 `-u`，一个每秒打印
一行的脚本在它 8 KB 的缓冲填满或者进程退出之前，往文件里写的是**零字节**——尾随
逻辑完全正确，而且完全读不到东西。

用 flag 而不是 `PYTHONUNBUFFERED`：`-I` 蕴含 `-E`，子进程环境根本不会被查。

### 2.2 预览读的是文件，不是管道

尾随读的是「信封稍后据以构建的同两个文件」，而不是把子进程的输出改接到管道再转发。
这样文件仍然是**脚本产出了什么**的唯一事实来源：一条记录被丢掉、被截断、被解码坏
了，都改变不了调用方最终收到的东西。**这是一份文件的预览，永远不是那份文件。**

## 3. 传输改成 SSE，以及它的代价

`json_response=False`。收益见 §1 的表。代价要说清楚：响应现在是一条流，所以任何
**整份缓冲响应**的中间层都会把这件事抵消掉。这台服务器绑在 loopback 上、由一个
进程访问，正好是没有这种中间层的部署形态——换个部署形态，这一行要重新论证。

`report_progress` 在调用方没带 progress token 时是 no-op，所以服务端无条件上报是
安全的：没要的客户端只多付一次计数器自增。有测试钉住，因为这一条要是不成立，每个
不带回调的调用都会死在一个它从没要过的通知上。

`progress` 字段计的是**已流出的字符数**而不是完成度，`total` 恒为 `None`：协议要求
这个值单调递增，而这个进程确实不知道脚本还剩多少——它是一个跑着任意代码、退出前
什么都不返回的容器。ADR-068 §2.3 拒绝心跳带 `percent`，理由完全相同，`_forwarding`
因此把 `progress` 和 `total` 一起丢掉。

## 4. 预览可以被截断，而流不可以

ADR-028 立过一条规矩：截断的流是坏掉的流，因为下一步会把它当成完整的读。本 ADR 的
预览有三处**故意**的有损，而且都不违反那条：

| 有损 | 数字 | 为什么不违反 ADR-028 |
| --- | --- | --- |
| 单条记录上限 | 2 KB | 一次 `print` 一兆不该变成一行一兆 |
| 整次调用上限 | 64 KB | 到顶后静默停止 |
| UTF-8 解码 | `errors="replace"` | 按固定字节数读，早晚落在多字节字符中间 |

**因为信封没有被截断。** 完整的两条流照旧按各自大得多的上限回到调用方，预览只是
预览。到顶之后是静默停止而不是插一个标记：这时候的输出早已不是任何人在读的东西，
而给一份显式声明「我是预览」的东西加省略号，是在回答没人问的问题。

## 5. 前端：`message` 变成 `lines`

ADR-068 的 `ToolProgressView` 只有一个 `message`，因为那时候一次调用只会说三句话。
现在脚本自己在说话，一句变成一串，于是它变成 `lines`——保留最后 8 行的一个窗口。

**阶段和脚本输出共用这一串，故意的。** 它们本来就是交错按序到达的：
`executing in the sandbox`，然后脚本印的东西，然后 `saving 2 output file(s)`。这
个顺序是这次调用的一份忠实记录。拆成两个字段就得决定哪个显示在上面，而任何一种
决定对某些调用都是错的。

一条记录可能含多行（尾随读的是字节块不是行，一个轮询间隔里的四次 `print` 会以一条
带三个换行的记录到达），所以拆行在前端做。空行丢掉：脚本印一个空行是在说它自己的
排版，不是在说进度，而一个只有 8 行的窗口不该花在这上面。

## 6. 被拒绝的替代方案

**让 `ToolProgress` 多一个 `channel` 字段。** stderr 用一个可读前缀标而不是结构化
字段，因为 progress 通知本身没有结构化字段可放；而 `message` 里塞 JSON 会让这条
通知对任何一个照直渲染进度消息的 MCP 客户端都变成乱码。前缀是纯文本，谁看都能懂。

**把预览也做成 durable 事件。** 每个跑着的脚本每秒几行 PostgreSQL 写入，换一份
没人回放的记录——脚本产出了什么，`ToolCompleted` 已经说了。同 ADR-068 §6。

**给 `_bootstrap` 开第三条 fd。** 见 §2：要动 ADR-029 逐条论证过的隔离 flag。

**在 host 侧按行重组之后再限流。** 试过一版按记录节流，然后拿掉了：容器那头的轮询
间隔（250 ms）已经是天然的节流，再加一层就是两个地方各有一半的节奏控制。
