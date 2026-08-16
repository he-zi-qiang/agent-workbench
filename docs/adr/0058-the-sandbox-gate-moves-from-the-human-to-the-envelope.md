# ADR-058：沙箱之门从人移到信封

- 决策点：Code 会话里每一次 `sandbox_run` 都要停下来等人批准吗；这个选择归代码
  还是归部署；提示词与超时要不要跟着动
- 状态：**接受**。新增 `code.sandbox_requires_approval: bool = False`——默认
  **不**逐次批准；`destructive` 风险无条件继续上膛；审批机器（闸门、registry、
  决定端点、`approve_for_session` 对 external 的拒绝）**一个字不动**
- 日期：2026-08-16
- 影响：`CodeSettings` 增加一个带默认值的叶子（schema **不抬版**，
  `docs/configuration.md` §schema 1.14→1.15 条目里的既有先例）；
  `CodeConfig` 与装配线把它带到 `CodeSessionService`；信封的
  `approval_required_risks` 从写死的 `("external","destructive")` 变成按开关取
  `("destructive",)` 或原样；`code_prompt.py` 增加 `_UNGATED` 变体；
  `config.demo-local.toml` / `config.code-local.toml` 的 `turn_timeout_seconds`
  240 → 360；控制台在 `status != completed` 时用 `stop_reason` 给一句话
- 依赖：[ADR-057](./0057-a-pure-function-is-not-a-shell.md)（沙箱接线）、
  [ADR-054](./0054-a-digest-cannot-be-consented-to.md)（摘要无法被知情同意）、
  [ADR-029](./0029-ephemeral-sandbox.md)（容器即安全边界）、known-gaps F-05

## 1. 背景：门起作用的第一天，暴露的是门买不到的东西

ADR-057 接通 `sandbox_run` 后，F-05 早早上膛的闸门第一次被真实工具触发：每一次
运行代码都停下来等人。实测两条代价立刻可见：

1. **批准的人看不到要批准的东西。** 卡片上是工具名和一个 SHA-256 参数摘要。
   ADR-054 已经论证过：摘要无法被知情同意——它能证明"批准的就是那次调用"，
   不能告诉人"那次调用是什么"。于是这道门买到的不是同意，是延迟。
2. **延迟直接吃掉迭代。** 本地 profile 的回合墙钟 240 秒，单次批准等待 120 秒：
   写-跑-改-再跑需要至少两次批准，两次等待就能耗光整个回合。回合死在
   deadline 上时服务端一句助手消息都不写（有意的：不替模型编报告），控制台
   于是显示指令后一片空白——用户读到的是「它没有再修改的能力」。

同一个平台的另一条路早已给出对照：Task Worker 跑同一个 `external` 风险的
`sandbox_run`，信封是 `approval_required_risks=()`，从 ADR-015 起就是如此。
平台自己的立场从来是：**沙箱的安全故事是容器**（一次调用一个容器、
`--network=none`、用完即毁、文件进文件出），不是每次调用旁边站一个人。

## 2. 决定

把这个选择交给部署，默认放行：

- `code.sandbox_requires_approval = false`（默认）：`external` 不再要求逐次
  批准，信封上膛 `("destructive",)`。
- 设为 `true`：恢复 ADR-057 发布时的行为，`("external","destructive")`。
- **`destructive` 在两种安排下都上膛。** 今天没有任何工具声明这个风险；哪天
  有了，门必须已经在那里。这也是 F-05 当年"早早上膛"论证的延续，只是范围
  缩小到了它真正守得住的东西。

不动的部分：`adapters/tools/sandbox.py` 的 `risk="external"` 声明（与 Task
路径共享，ADR-029 §3.5 钉死——`external` 描述的是"内容离开本进程"这个事实，
不是"要不要问人"这个政策）；审批闸门、registry、决定端点、
`approve_for_session` 对 external/destructive 的 422（ADR-054）；批准卡 UI
（destructive 的那一天它还得在）。

## 3. 跟着门一起动的两件事

**提示词。** 带门的变体教模型「every call stops and asks a human -- expect to
wait, and do not spend one on something you could have read」。门拆了之后这句
话就是在教模型回避一个刚被解放的工具——和 ADR-057 §1 记录的那次「模型自己说
它跑不了」是同一类错误，方向相反。`_UNGATED` 变体改说：调用立即执行，用运行
来检查你的工作，写-跑-读-改-再跑。变体经 `_rewrite` 锚定派生，锚漂移在
import 时就炸。

**超时与可见性。** 240 秒是按"回合大半在等人"定的；不等人的回合真的会迭代，
360 给它余量（`approval_timeout < turn_timeout` 的校验器不动，destructive
仍可能等人）。控制台补上它一直缺的那半句：`AskResponse` 早就返回
`status`/`stop_reason`，现在 `status != completed` 时按 `stop_reason` 渲染一句
中文（"这一轮到时间停下了。已完成的改动都在工作区里，直接说下一步就能继续。"），
下一次发送即清除。刷新后提示不再重现——接受，回合本就不可恢复
（`CodeSettings` 的定位注释）。

## 4. 为什么默认是 false 而不是 true

一个默认 `true` 的开关等于把 ADR-057 的现状改个名字续下去，而 §1 的两条代价
是**默认配置**下测出来的。安全上它不买东西（摘要卡 + 容器已经是边界）；体验上
它把这个面最核心的能力（跑代码验证自己）变成了最慢的操作。要门的部署一行配置
拿回去，且拿回去的是完整的旧行为——这正是"政策归部署、事实归代码"的分界：
`external` 是事实，问不问人是政策。

## 5. 证据

- `tests/application/test_code_session.py::test_the_gate_arms_what_the_deployment_chose`：
  两种安排下信封与提示词成对切换。
- `tests/api/test_code_api.py::test_a_standing_yes_is_refused_for_an_external_tool`
  原样通过：台账对 external 的 standing-yes 拒绝与信封无关，继续有效。
- 前端 `CodePage.test.tsx::says why a turn stopped when it produced no report`：
  deadline 回合渲染一句话而不是沉默。
- 本地 demo profile 实跑见 `docs/status.md` 当日条目。
