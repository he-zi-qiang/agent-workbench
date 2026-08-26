# ADR-081：被缩短过的对话要自己说出来

- 决策点：ADR-080 给了运行一个上下文天花板，撞到就停下并说出撞的是哪一条。**停下不是
  唯一的答案**：一段长对话的中间往往是可以概括的，概括之后这一轮还能继续。问题是
  概括这件事有一个所有实现都会踩的坑——它会**悄悄**发生。模型收到一段被挖掉中间的
  历史，而它不知道被挖过；读转录的人看到一次成功的运行，而看不到成功之前少了什么。
  要不要做压缩；如果要，**怎么让它不是一次静悄悄的删除**
- 状态：**接受**。`runtime/compaction.py` 决定切在哪、给概括器看什么、重建成什么；
  概括本身是一次**走同一个 `ModelPort`、记在同一本 ledger 上**的普通模型调用；
  重建后的对话里带一条带标记的 `assistant` 消息，事件流里落一条 durable 的
  `ContextCompacted`。默认**关**（`runtime.context_compaction_enabled = false`）。
  **明确不做**：不新建 port、不新建 adapter、不铸造 `ArtifactRef`、不伪造 `user`
  消息、不让这一轮变得可恢复（F-01 保持拒绝）
- 日期：2026-08-25
- 影响：新增 `src/agent_workbench/runtime/compaction.py`；`runtime/agent_runtime.py`
  新增一个构造参数、`_RunLedger.compactions`、`_compacted()` 与循环顶部的分支，以及
  `MAX_COMPACTIONS_PER_RUN`／`COMPACTION_PROMPT` 两个常量；`runtime/state.py` 的模块
  注释（`compacting` 不再是"可达且未用"）；`bootstrap/settings.py` 新增
  `runtime.context_compaction_enabled`；`bootstrap/projections.py` 两个 config 与两处
  构建；`apps/api/dependencies.py`（五处运行时）与 `apps/task_worker/composition.py`
  传参；`config/config.default.toml` 与 `config/ownership.yaml` 各一行。
  **不动配置契约**：`config_schema_version` 保持 `1.18`——既有段下新增带默认值的叶子
  不抬版，规则见 `docs/configuration.md` §2 的版本表（`1.14 → 1.15` 那一行写着它），
  ADR-080 的 `context_window_tokens` 是同一批里的同一类先例

---

## 1. 背景：一套完整的词表，和一个都没有的发射点

这件事的协议早就落地了，只是从来没有人发射过它：

- `domain/events.py` 里的 `ContextCompacted` 是 **durable** 事件，带
  `removed_message_count`、`tokens_before`、`tokens_after`、`summary_ref`；
- `runtime/state.py` 的转移表里，`recording_results → compacting` 和
  `compacting → model_streaming` 都是**合法边**；
- `ArtifactKind` 里有 `compaction_summary`。

`docs/known-gaps.md` D-06 把这个状态记为**未接线**而不是拒绝，并且说得很准：

> 协议先于实现落地是对的，但在实现出现之前，事件类型的存在不构成能力。

这份 ADR 就是那个实现。它要回答的不是"能不能把对话变短"——那是一个 `list` 切片——
而是**变短这件事怎么才能不是一次静悄悄的删除**。

## 2. 五个决定，每一个都是先否掉了显然的做法

### 2.1 不新建 port，不新建 adapter

显然的做法是 `ports/context_compaction.py` 加一个 `ContextCompactor` 协议，再写一个
adapter。**否掉**。概括是一次模型调用，而这个运行时已经握着 `ModelPort`
（`agent_runtime.py` 的 `__init__`），`ModelRequest.model_profile` 也早就能选
`"compact"`（`ports/model.py`）。

自建一条调用路径会把一次真实的 provider 请求放到**这次运行自己的核算之外**：

| 绕开的东西 | 后果 |
|---|---|
| `ledger.usage` / `_priced()` | 运行记录的花费和账单对不上 |
| `overrun_reason_for` | 一次压缩可以把运行推过成本上限而不被发现 |
| `ModelStarted` / `ModelCompleted` + `model_call_id` | 事件流里有一次没人知道的调用 |
| `model_timeout_seconds` | 一次挂住的概括没有截止时间 |
| `ports/model.py` 的取消契约 | 取消一次运行不取消它正在做的概括 |

那条取消契约在 port 里是**刻意**写成"取消消费流的 task，而不是一个参数"的。一条自建
路径要么重新实现这五件事，要么就是五个悄悄的洞。

**顺带逼出一个待决问题**（记在这里而不是留给以后的人现场发现）：
`apps/task_worker/composition.py` 与 `apps/api/dependencies.py`（五处运行时）传给运行时
的都是 `prices=main_profile.prices`，所以一次 main-profile 运行里的 compact 调用
**按 main 的价格计费**。

今天这不产生误差，但**不是因为两个 profile 指着同一个模型**——`config.code-local.toml`
与 `config.demo-local.toml` 的 `[model.main]` 已经是 `deepseek-v4-flash` 而
`[model.compact]` 仍是 `deepseek-chat`，而这两份正是 `scripts/dev.sh code-api` /
`demo-worker` 跑的 profile；`config.default.toml` 的两个占位 id 也不相同。真正的前提是
**没有任何一份配置声明过价格**：`grep micro_usd_per_mtok config/*.toml` 为空，`_priced()`
于是恒返回 0。

所以：**任何部署在开压缩的同时打开价格之前，必须先给运行时一份按 profile 的价格表**，
否则那一刻这一行就变成一个悄悄的记账错误。

同一个 profile 分歧还有第二个后果，这个已经修了：`ModelStarted.model_id` 过去写的是
构造时定死的 main 标签，而适配器是按 `ModelRequest.model_profile` 分派的——一次 compact
调用因此被记在一个它没有到达过的模型名下。`ClaudeLikeAgentRuntime` 现在多收一个
`compact_model_label`，`_label_for()` 按 profile 选。

### 2.2 不铸造 `ArtifactRef`

`ContextCompacted.summary_ref` 是 `ArtifactRef | None`，看起来在邀请把概括存成
artifact。**第一版留 `None`**：`ArtifactRef` 带 `tenant_id` 和内容 sha256，只由
artifact store 在 `tenant_filter_required` / `path_sandbox_enabled`（两个都是
`Literal[True]`）之下铸造，而运行时手里既没有 store 也没有 principal。在这里拼一个出来
就是伪造一条本该由别人签名的记录。

`None` 是诚实的："这次压缩没有留下可寻址的摘要。"

### 2.3 不伪造 `user` 消息——但也不能不说

"告诉模型它的历史被缩短过"这一半最容易被省掉，而省掉之后压缩就是**有害**的：模型会
从一段有洞的历史继续作答，且它无从知道有洞。

显然的做法是插一条 `user` 消息（"以下是之前对话的摘要……"）。**否掉，而且是这份 ADR
唯一一条硬禁止**：那会把话塞进用户嘴里，审计记录会说一个人说过一句没有人说过的话。
`docs/configuration.md` 已经用同样的理由否掉过一种审批形状。

也**不是** `system` 消息。`validate_messages` 拒绝消息列表里出现 `role="system"`，理由
是"System content has exactly one home"——那个 home 是 `AgentRunRequest.system_prompt`。
把摘要放进 system prompt 也不对：system prompt 是这一轮**开始时**冻结的世界描述，而摘要
是运行**过程中**产生的派生上下文。

留下的是 `assistant`，而且它是**真的**：这段文字是这个 agent 对自己早先工作的记述。
再加一行标记：

```
[earlier turns of this run, summarised]
```

没有标记，模型会把一段自己不记得写过的话读成自己逐字写过的话，然后把它当工具结果引用。

### 2.4 切点必须落在协议边界上

`architecture-baseline.md` §7.2 的运行时不变量 1 要求：每一个暴露出去的 `tool_call_id`
恰有一条 ToolResult。而"保留最后 N 条"这个显然的实现**大约有一半的时候**会切在
assistant 的 `tool_use` 和回答它的 `tool` 消息之间。

provider 对这种消息列表的回应是一个 400——**正是 ADR-080 刚刚不再转述的那个症状**。压缩
如果引入它，就是用一个更难查的方式把刚修好的东西弄回去。

所以边界**只向前走，不向后退**：

```python
while cut < len(messages) and messages[cut].role == "tool":
    cut += 1
```

向前走只会让保留的尾巴更短；向后退会让它变长，并且可能一路退过头部，产出一个"声称删了
东西但什么都没删"的计划。

`tests/runtime/test_compaction.py::TestTheCutIsLegal` 把 1..12 对、每个 `keep_last` 都扫
一遍，断言重建后的列表里每个 call 仍然恰有一条 result。把上面那两行删掉，这条测试连同
另外两条一起变红——这是它是测试而不是装饰的证据。

### 2.5 头部永远留下

第一条消息是这次运行**是关于什么**的那条。它也是几家 provider 明确要求对话必须以之开头
的那条，所以留着它意味着压缩永远不会产出一个本来就非法的形状。

## 3. 不变量

1. **概括失败，什么都不删。** `_compacted()` 在概括调用出错或返回空文本时返回 `False`，
   消息原样留着，运行按 ADR-080 的老样子停在 `context_limit`。没有中间结果：一段被删了
   中间又没有摘要的对话，比一次诚实的停止**更坏**。
2. **删了什么必须落成 durable 事件。** `ContextCompacted` 在消息被替换之后、返回之前
   发射，带上删了几条、之前多大、之后多大。
3. **`tokens_before` 是测量值，`tokens_after` 是估计值，且两者同单位。**
   `before` 是 provider 给出的、那次过大的请求的 `input_tokens`；`after` 没有对应的测量
   （下一个请求还没发出去），所以它是 `before` 按两份消息列表的字符比缩放而来。
   **两个都估**会把唯一一个测量值扔掉，并且让这条事件和它旁边的 `ModelCompleted` 打架。
4. **压缩调用计花费，不计步数。** token 和成本是真实支出，必须记；而 `steps` 是"agent
   循环走了几轮"，压缩没有推进循环——把它计成一步，会让一个逼近步数上限的运行被那个
   正在救它的东西掐死。
5. **一次运行最多压缩 `MAX_COMPACTIONS_PER_RUN = 3` 次。** 这是兜底不是策略：每次压缩都
   删掉中间，对话会变短，`plan_compaction` 自己最终会拒绝。但一个已经缩过三次还在线上的
   运行不会被第四次救回来，那时候诚实的答案是它一直在撞的那条天花板。
6. **压缩必须证明它起了作用，才算成功。** `tokens_after` 不只是发出去给人看的，它要
   先过 `context_reason_for` 那一关：估算值仍在软上限之上，这次压缩就**不算数**，运行
   按 ADR-080 停下。这条是补的——第一版只要"删掉了几条 + 概括非空"就返回成功，而
   `plan_compaction` 是按**消息条数**切的，所以被删的可以是四条短消息，而留下的尾巴里
   正躺着那个 60 KB 的工具结果。实测：三次压缩各自记录省下 0.03%，三个 70,000 token 的
   请求打在 64,000 的窗口上，最后死在 ADR-080 存在的意义所在的那个 HTTP 400。
   拒绝的代价是那次已经发生的概括调用；不拒绝的代价是它**加上**后面两个请求，外加一个
   没人归得了因的终局。

7. **压缩之后 `last_input_tokens` 带的是估算值，不是零。** 第一版清零，理由是"触发这次
   压缩的测量描述的是一段已经不存在的对话"——这话对，而它顺手把 ADR-080 的天花板给下一
   个请求解除了武装。带着估算值走，下一轮仍然被判断。

8. **概括调用途中被取消，运行报"已取消"。** 这是本循环里唯一一次结果不经过
   `_terminal_for_turn` 的模型调用，而一个被取消的回合返回的是"空文本、无错误"——和
   "概括器什么也没说"同形。第一版因此把它读成"没能缩短"，然后运行被归到
   `context_limit` 名下：有人按了停止，却被告知是模型窗口的错。

## 4. 这买到了什么，没买到什么

**买到了**：一段长对话可以继续，而且继续这件事在事件流里、在模型自己的上下文里、在
运行的花费里都留了痕。

**没买到**：

- **不是可恢复性。** F-01（Code 面无协调面、部署必然斩断在途回合）保持拒绝。压缩让一轮
  **活得更久**，不让它在进程死后接得上。任何"压缩顺便解决了 F-01"的说法都是错的。
- **不是无损。** 被删掉的是原文，留下的是概括。`KEEP_LAST_MESSAGES = 6` 保证模型手里还
  握着它正在用的那几条工具结果，但更早的原文回不来。
- **不是对着真模型验证过的。** `docs/architecture-baseline.md` 记着本仓没有任何测试打到
  真实 DeepSeek，CI `quality` 离线。`COMPACTION_PROMPT` 写出来的概括质量，在有一份实测
  转录之前，能力梯子停在 **Implemented + Tested**，不得描述成 Demonstrated。

## 5. 为什么默认关

ADR-080 让一次超窗的运行停下并说出撞的是哪条天花板。一个还没见过真实 `context_limit` 的
部署，没有任何东西可以用来判断压缩的收益和它的代价（一次额外的模型调用、一段被概括掉的
原文）。**先有诚实的失败，再有补救**——这也是为什么这两件事是两份 ADR 而不是一份。

`runtime.context_compaction_enabled` 只在声明了 `context_window_tokens` 的 profile 旁边
才可能生效：没有窗口就没有"超出"可言。所以在一个没声明窗口的部署上，这个开关什么也
不决定。
