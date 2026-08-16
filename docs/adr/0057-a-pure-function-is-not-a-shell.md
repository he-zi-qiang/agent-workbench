# ADR-057：纯函数不是 shell

- 决策点：Code 会话能不能运行代码；`code.shell_enabled` 那个冻结的 `Literal[False]`
  怎么办；解冻之后 `execution_locality` 与 `coordination` 还算不算被钉死
- 状态：**接受**。把 `shell_enabled` 改名为 `sandbox_enabled` 并解冻成 `bool`
  （默认 `false`），**`execution_locality` 与 `coordination` 两条继续冻结**
- 日期：2026-08-16
- 影响：`CodeSettings.shell_enabled: Literal[False]` →
  `sandbox_enabled: bool = False`；`CODE_TOOLS` 增加 `sandbox_run`；Code 信封的
  `max_tool_risk` 在开启时放宽到 `external`；`code_registry` 增加 sandbox
  binding；控制台默认 scope 增加 `sandbox:run`；`code_prompt.py` 的系统提示词
  改写；`scripts/dev.sh` 增加 `sandbox-server` / `sandbox-check`；
  `config.demo-local.toml` 增加 `[sandbox]` 与 `[code] sandbox_enabled`。
  配置 schema `1.15` → `1.16`（改名会让写着 `shell_enabled` 的旧文件停止加载，
  与 `1.13 → 1.14` 同属"方向相反"的一次）。**沙箱本体、`ToolGateway`、审批闸门、
  `ArtifactStore`、`ExecutionLease` 一律不动**
- 依赖：[ADR-029](./0029-ephemeral-sandbox.md)（沙箱是纯函数）、
  ADR-019（步骤输入是 opt-in）、known-gaps F-05（闸门接好了但没有工具触发它）

## 1. 背景：模型自己说它跑不了

一次真实回合里，Code 被要求「新建 fib.py 并读回确认」。它做到了，然后在报告末尾
写道：

> 本环境没有 shell，我无法实际执行该 Python 文件，以上输出是根据代码逻辑推断的。

这句话是准确的，而且是提示词教它说的（`code_prompt.py` 第四条纪律：若任务依赖
运行代码就说出来，不要假装跑过）。诚实是对的，但一个写完代码只能推断结果的编码
助手，把验证这件事整个留给了人。

## 2. 名字是这次决策里最误导的东西

`shell_enabled` 冻结在 `Literal[False]`，架构测试
`test_code_premises_are_frozen.py` 钉住它，`settings.py` 的注释说：

> Giving a coding agent a shell means granting `sandbox_run`

**这句话把两件不同的事等同了，而它们的安全属性差得很远。**

一个 shell 是：任意进程、宿主文件系统、网络、跨调用的状态。
ADR-029 的 `sandbox_run` 是：

| | |
|---|---|
| 一次调用 | 建一个容器、执行、销毁 |
| 网络 | `--network=none` |
| 文件系统 | 只有 `inputs` 进、`outputs` 出，不认识工作区、租户、所有者 |
| 资源 | 60 秒墙钟、512m 内存、1.0 CPU、64 进程 |
| 状态 | 跨调用**没有**任何状态 |

这些不是配置，是 `executor.py` 里的常量（ADR-029 §3 特意如此）。所以它是一个
**纯函数**：文件进、文件出。ADR-027 那条「只读取外部世界，写只写进自己的
artifact」没有被跨过去 —— 沙箱连外部世界都读不到。

**给的不是 shell。** 名字改成 `sandbox_enabled`，因为一个描述错了对象的开关，
后来每一次关于它的判断都会从错的前提出发。

## 3. 冻结的理由已经不成立

架构测试的 docstring 自己写明了冻结的理由：

> 它冻结 `False` 是因为打开它意味着授予 `sandbox_run`，而那需要一个这个进程不
> 启动的 server，和一个没有任何 principal 持有的权限 scope —— 所以一个布尔值
> 会让部署把它设上却什么都得不到，那是三种结果里最坏的一种。

这是一条**关于接线状态的**理由，不是关于安全的理由。本 ADR 把那两样都接上：

- server：`scripts/dev.sh sandbox-server`，并让 `demo-api` 启动前探测它 ——
  MCP 工具目录在进程启动时冻结一次，沙箱起晚了会得到一个健康但没有那个工具的
  API（`demo-worker` 早就为 Word 和 web 两个 server 做了同样的事）。
- scope：控制台默认 scope 加 `sandbox:run`。

接上之后，「设了却什么都得不到」不再是可能的结果，于是那条理由连同冻结一起退休。

## 4. 什么继续冻结，为什么

`execution_locality = "in_api_process"` 与 `coordination = "none"` **一个字不动**。

它们钉的是完全不同的东西：Code 放弃了可恢复性，换来「回答审批的人正对着那个停着
的协程说话」。沙箱不碰这笔交易 —— 一次 `sandbox_run` 仍然发生在这个 API 进程发起
的容器里，仍然没有租约、没有 reaper、没有检查点。进程死了，那一轮照样没了
（F-01），沙箱容器随之销毁，因为它本来就不跨调用存在。

架构测试因此从三条变成两条**加一条新的**：`sandbox_enabled` 现在断言它是
`bool` 而不是 `Literal` —— 一个被重新冻结的字段会让这条测试失败，这是故意的，
它记录「这一条是被有意解冻的」而不是留一个空位。

## 5. 审批：闸门第一次真的会响

`sandbox_run` 的 risk 是 `external`（`projections.py`），而 Code 信封的
`approval_required_risks` 早就是 `("external", "destructive")`。于是接上沙箱的
直接后果是：**每一次跑代码都会停下来等人点一下**。

这是 known-gaps F-05 记的那件事 ——「闸门接好了，但今天没有工具会触发它」——
第一次有工具触发它。不是回归，是那套机器第一次被用上，`code_approvals.py` 的
`_Pending`、`SessionApprovalGate`、`POST /sessions/{id}/approvals/{id}` 与它们的
测试从此有了真实调用路径。

`approve_for_session`（本会话都允许）对 `external` 风险**仍然被服务端拒绝**，
控制台也不渲染那个按钮 —— 一次不可逆的效果每次都得单独问，这条不因为它现在真的
会被问到而放松。

**这与 ADR-048 的导出闸门无关。** 那是 Task 的导出闸门，问的是「要不要把文件交给
提交者自己」，答案是不要问；这是 Code 的工具闸门，问的是「要不要在这台机器上跑
这段代码」。两套机器互相独立，两个问题也不是同一个问题。

## 6. 默认关，且写明代价

`sandbox_enabled` 默认 `false`，`config.default.toml` 同步。一份没有 Docker 的
checkout 打开它会在启动探测时听到明确的拒绝，而不是在第一次跑代码时听到一句
`sandbox_runtime_unavailable`。

`config.demo-local.toml` 打开它，因为那是本机、有 Docker、且这份 profile 已经
要求两个 MCP server 在跑。

## 7. 做完的判据

一次真实回合里，Code 写出一段 Python、停下来请求批准、在人点了之后真的执行，并
把 `stdout` 与产出文件带回工作区；同一次会话在拒绝之后不执行且回合继续。

## 8. 已验证（2026-08-16，本地 demo profile，真实 provider 与真实容器）

**批准路径。** 指令「写一个 primes.py，用埃拉托斯特尼筛法求 50 以内的素数并打印。
然后用 sandbox_run 真的运行它」：

- 回合**停下来**请求批准，`tool_name = sandbox_run` —— known-gaps F-05 记的那道
  闸门第一次被真实工具触发。
- 答 `approve_once` 之后才执行。
- 报告里是真实输出：`[2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]`，
  并注明「退出码 0，无 stderr」。开头一句是 “I wrote `primes.py` and actually ran
  it in the sandbox.” —— 对照 §1 那句「本环境没有 shell，我无法实际执行」。
- 工作区留下 `primes.py`（463 字节，`text/x-python`）。

**拒绝路径（对照组）。** 同一会话，指令「再运行一次确认输出没有变」，答 `deny`：

- 工具返回 `policy_denied`，**没有执行任何代码**。
- 回合没有失败，继续并给出报告，其中写着「这次我没有实际运行它，不能声称重新
  验证过。要再次确认，需要重新允许 `sandbox_run` 调用」—— 即提示词第 3 条
  （拒绝是信息，不要重试同一个调用）与第 4 条（不要让报告读起来像跑过）。
