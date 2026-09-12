# ADR-0116：重复的调用按记录作答；命令的门可以由回合预先答；文件可以删和改名

- 状态：Accepted
- 日期：2026-09-13
- 关联：ADR-0114（同问熔断与第 25 次提醒——本 ADR 把熔断从「拒绝」改成「按记录作答」，提醒不动）、
  ADR-0115（runner 容器——本 ADR 第三个决定的前提，没有它那一档不存在）、ADR-087（一段会话可以比
  部署更谨慎，不可以更宽松——本 ADR **按它自己写下的理由**加第三档，不推翻它的不变量）、
  ADR-0077（一条在这台机器上的命令在跑之前先给人看——原生路径上一字不动）、ADR-0078（读写回执——
  `project_move` 把回执带走，`project_delete` 不要回执）、ADR-086（`project_writes` 是事实源——
  重放不声称写入）、ADR-058（`external` 由部署决定——本 ADR 不碰）

## 1. 背景

2026-09-12 23:44–23:47（UTC+8），`windows测试` 项目里的一轮编码会话，deepseek-chat，
`run_3444…3232`。事件流 172 条：23 步、25 次工具调用，前 12 次是 `project_list` / `project_read`，
后 13 次全是 `project_run`，命令只有六条不同的：

| 命令 | 内容 | 派发 | 被拒 |
|---|---|:---:|:---:|
| #1 | 数 `LEVEL_ROWS` 有几行、每行多长 | 1 | |
| #2 | 打印每一行 | 1 | |
| #3 | 按字符统计每行的连续区段（带一句 `# group consecutive` 注释） | 1 | |
| #4 | 同 #3，去掉那句注释 | **3** | 1 |
| #5 | 同 #4，末尾加一句中文注释 | 1 | |
| #6 | 同 #5，末尾加一个换行 | **3** | 2 |

每一次派发都停在一张审批卡上（`destructive` 风险，ADR-0077）：**六条命令，十张卡，用户在三分钟里点了
十次「允许一次」**。第三次被拒之后运行时判 `RunFailed`：「the run kept proposing calls it had already made:
3 were refused as repeats」。控制台显示「这一轮没有跑完」。

在 runner 容器里把 #4 原样再跑一遍（`docker exec agent-workbench-runner-1 …`）：退出码 0，14 行正确的
区段统计。也就是说，**模型拿到的是一个正确的答案，然后把同一条命令又发了一遍**——它每一步的
`ModelCompleted.text` 都是空的，没有解释。

这是模型的毛病，运行时治不了；但运行时在这一轮里做了三件让它更糟的事：

1. 同一条命令派发了三次，每次都问人一次（`MAX_IDENTICAL_CALLS = 3`，ADR-0114 之前就有）；
2. 第四次**拒绝**，而拒绝是一个新答案——模型对它的反应是改一句注释，签名变了，三次派发从头再来；
3. 三次拒绝之后杀掉整轮，工作区里已经写好的东西留下了，报告没有。

用户当晚的三句话，就是本 ADR 的三个决定：「老是进入死循环」「已经有自动权限还是来询问我的同意」
「文件夹中的文件不能进行删除和改名」。第三句在每条路上都成立：文件语言有读、写、改、列、搜五件，
没有删和改名；Claude Code 用 Bash 里的 `rm` 和 `mv`，这里的 shell 是 `project_run`——Windows 原生
启动器不提供它，而一个握着五件文件工具的模型也不会想到用一条命令去删文件。

### 1.1 Claude Code 是怎么做的，对照表

| Claude Code | 它解决的问题 | 这个仓库对应物（本 ADR 之后） |
|---|---|---|
| 同一条 Bash 命令再发一次就再问一次；模型很少这么做 | 重复 | 运行时：同一调用、中间没跑过任何非只读的东西 → **按记录作答**，不派发、不问人；第三次重复收走工具，让它写报告 |
| 审批框上的「Yes, and don't ask again for `<命令>`」 | 反复点同一张卡 | `approve_for_session` 对 `destructive` 开放；规则本来就按参数摘要记，所以答应的是**这一条命令** |
| 权限模式 plan / default / acceptEdits / bypassPermissions；auto 模式由一个分类器逐条判命令，明显危险的仍然弹窗 | 「什么都别问我」 | 第四档 `approvals="unattended"`（界面「放手做」）：信封 `unattended=true`，策略引擎对每一次 destructive 调用的**参数**再问一遍「有没有理由拦」，`domain/commands.py` 的六种形状仍然停；**只在命令跑进 runner 容器的部署上提供** |
| `rm` / `mv` 走 Bash | 删、改名 | `project_delete` / `project_move`，走同一个 `ProjectFileStore` 与同一个沙箱 |
| 步骤列表里两次一样的命令就是两次 | 看得出哪一次没花钱 | `ToolCompleted.replayed` / `ToolFailed.replayed`，控制台那一行写「（沿用上次结果）」 |

第一行是 Claude Code **没有**的机制——它不需要，它的模型不会连发四次。ADR-0114 的提醒也是这个性质。
其余四行都是搬它的。

## 2. 决定

**四件事。**

### 2.1 同一个调用，中间什么也没跑，第二次按记录作答

`runtime/agent_runtime.py`。`_RunLedger` 多三样东西：

- `world_version`：每一批里只要**派发过**一个风险不是 `read` 的调用，批次结束时加一。写会动文件，命令可能
  动任何文件，搜索可能在同一个查询后面换上新页面；读什么也不动。它记的不是「世界变了没有」——
  运行时无从知道——而是「这一轮做过可能让它变的事没有」。
- `answered`：签名（工具名 + 排序后的参数）→ (答案时的 `world_version`, `ToolResult`)。**在这一批加过
  版本之后**才写，所以一个调用自己的效果不算作它自己的重复：`run pytest` 连发两次，第二次按记录答；
  `run pytest`、`edit`、`run pytest`，第二次真跑，因为中间有一次编辑。
- `replayed`：每个签名被按记录答过几次。

一个签名被提出时，如果记录里有它，且记录的版本等于当前版本，就不派发：`ToolGateway.replay()` 把上一次
的结果换上新的 `tool_call_id` 交回去——成功的结果在正文后面附一句话（这是同一个调用、中间没跑过别的、
这是同一个答案、不要再调用它），失败的结果把这句话并进 `error.message`（`ToolResultBlock` 只在正文为空时
渲染错误文本，附在正文后会把拒绝盖掉）。**写入字段不带过去**：`workspace_writes` / `project_writes` 是
「这一步把哪些字节换了」的事实（ADR-086），而这一步什么也没换。

`MAX_REPLAYS = 2`：同一个签名按记录答两次；第三次仍然作答，但那句话改成「工具已收走，写报告」，
并且 `ledger.tools_withdrawn = True`——下一次模型请求不带任何工具，和工具调用配额用尽时一样
（那段代码的注释早就写着：拿走工具，是问这一轮还答得了的那个问题：写下你手里有的）。一个诚实的
provider 在没有工具的请求上只能写文字，于是这一轮以 `RunCompleted` 和一份报告结束。一个在没有工具的
请求上仍然提出调用的模型（脚本化的替身能做到）才走老路：`RunFailed`，句子还是「kept proposing calls it
had already made」。

拒绝也进记录：被策略拒掉的调用再来一次，答的还是那次拒绝（代码不变，`policy_denied` 还是
`policy_denied`），加同一句话。唯一不进记录的是标着 `retryable=True` 的失败——那是模型有理由再问一次的
答案。

删掉的：`MAX_IDENTICAL_CALLS = 3`（派发上限）、`MAX_REPEAT_REFUSALS = 2`（拒绝上限）、批次末尾那个
`RunFailed`。它们的注释里「再读一次是寻常的——写完之后再读、列表前后各一次」这句话是对的，错的是
机制：让那些重复寻常的是**中间那次写**，而派发次数看不见写。

事件：`ToolCompleted` 与 `ToolFailed` 各多一个 `replayed: bool = False`。不在 `record_step_inputs` 那道
闸后面，理由和 `workspace_writes` 一样——它说的是谁作的答，不是答了什么；而且正是在看不见正文的部署上
最需要它。控制台 `stepGroups.ts` 读它，标题后面加「（沿用上次结果）」。

### 2.2 `approve_for_session` 对 `destructive` 开放

`application/code_approvals.py`：`UNREPEATABLE_RISKS = {"external"}`。规则从一开始就按
`argument_digest` 记（`ports/approval_gate.py` 写明了为什么），所以一个「本会话都允许」给的是这一条
命令——改一处、跑一次 `pytest`、再改一处、再跑一次——不是这件工具。此前拒绝它的理由「对不可撤销效果
的一次概括同意」在按摘要记的规则上不成立：它不概括。`external` 留着：一个离开本进程的问题每次都值得问，
而且那是部署的决定（ADR-058）。界面同步：`toolVocabulary.ts` 的 `UNREPEATABLE` 只剩 `external`，
审批卡上「这一类调用每次都要单独问」那句只对 external 显示。

### 2.3 第三档：`approvals="unattended"`，只在命令跑进 runner 容器的部署上

ADR-087 §2 明确否掉过「什么都别问我」，理由是它要拿掉 `destructive`，而 `destructive` 是
`project_run`——在用户自己的机器上跑一条命令，ADR-0077 说它跑之前要给人看见。本 ADR 接受这两句话，
并且两句都不再成立于同一个地方：

- **它不拿掉 `destructive`。** `code_approval_risks("unattended", …)` 返回的就是部署的地板，一个风险不加
  不减；ADR-087 那个「`base` 是每条返回路径的子序列」的不变量逐字保留（`test_the_write_gate_adds_a_risk_
  and_can_subtract_none` 没改）。变的是信封多一个字段 `AuthorizationEnvelope.unattended`，
  `EnvelopePolicyEngine` 在「这个调用要停」之后多问一句：提交的人是不是预先答了，参数里有没有拦它的理由。
  没有理由 → `allow(reason_code="unattended_turn", requires_approval=False)`；有 → 照旧停在人面前，
  `reason_code="command_still_asks:<理由>"`。两个 reason_code 都落在 `PermissionResolved` 上，事后能从
  事件流里分出「人点的」「规则答的」「形状拦的」。
- **拦的理由是一张清单**，`domain/commands.py`，六种形状，每一种是「跑了会赔掉什么」而不是「听起来
  多危险」：删掉整个目录或目录里的一切（`rm -rf .` / `*` / `/` / `~`；`rm -rf build` 不拦）；用 git 丢掉
  未提交的工作（`reset --hard`、`clean -f`、`checkout -- .`、`restore .`）；改写共享历史（`push --force`）；
  把网上下来的东西直接灌进 shell（`curl … | sh`）；伸出项目目录之外（`sudo`、`mkfs`、`dd of=/dev/…`、
  `chmod -R 777 /`、`> /dev/sd…`）；无限 fork。命令位置才算数：`grep -r sudo .` 搜的是这个词，
  `git commit -m 'sudo'` 记的是这个词，`echo rm -rf .` 打印它，都不拦；`sh -c "rm -rf ."`、
  `cd build && rm -rf .`、`env FOO=1 rm -rf .` 拦。48 + 30 条用例钉在 `tests/domain/test_commands.py`。
- **在哪提供，由命令跑在哪决定。** `CodeSessionService.unattended_available = config.runner is not None`
  ——和 `ProjectRunTool(runner=…)` 读的是同一个事实。runner 容器只挂项目文件夹、没有 key、没有数据库
  地址、没有工件卷（ADR-0115 §2），所以「在用户自己的机器上跑一条命令」那句话在那条路上不再是
  它保护的东西；原生路径上（`scripts/dev.sh`）它原样成立，这一档不出现——`ask()` 在碰转录之前就以
  `CodeUnattendedNotOfferedError`（422）拒绝，`GET …/tools` 的 `unattended_available` 提前说了会拒。
- **提示词说清没人在看。** `with_unattended`：把 `_HAS_SHELL` 里「每次调用都停下来问用户、他们看得见
  你写的命令」那句**替换**成「这一轮不问，用户事后在转录里读命令」（`_rewrite`，锚点漂了在导入时就炸），
  再追加一段：报告要写清跑了什么、改了什么；仍然会停的那六种形状用人话列一遍，别绕着它们计划。
  计划回合不加这段（它握不到任何 destructive 工具）。
- 界面：输入框旁边的三档变四档，「放手做」只在 `unattended_available` 为真时画出来；工具清单里
  `project_run` 那句「会先问你」随这一档一起消失（服务端风险表没变，变的是那道门被预先答了）。

`external` 不在这一档的范围里，和 ADR-087 对写入闸的态度一样：一个不想被问联网的人，要去改部署。

### 2.4 `project_delete` 与 `project_move`

- `ports/project_files.py` 多一个 `move(path, new_path) -> ProjectFileEntry`；
  `adapters/filesystem/sandbox.py` 多一个 `rename`：两端都过 `_checked`（越出根、软链穿越各自的拒绝
  一字不改），源按字面路径改名（软链本身移动，不是它指向的文件），目标必须为空——POSIX 的 `os.rename`
  会静默覆盖已有目标，那等于删掉一个从没被读过的文件。目录拒绝，理由同 `delete`。
- `adapters/tools/project_files.py` 多两件工具。`project_delete`：`destructive`（没有撤销），不要读回执
  （`rm` 从来不问你读没读过，没人读过的构建产物正是最常删的东西；拦它的是人，除非这一轮放手做），
  `project_writes=(path,)`。`project_move`：`write`（什么也没丢，字节还在，只是换了名字，而且落不到已有
  文件上），读回执随文件走——读过 `a.py` 整个、移成 `b.py`、再写 `b.py`，写入闸看得见模型读过它要换掉的
  每一个字节；`project_writes` 两端都报。
- `CODE_PROJECT_TOOLS` 五件变七件。由此推导的两个后果，都是算出来的不是决定的：基础集的风险上限
  从 `write` 变成 `destructive`（`project_delete` 在里面），计划回合按风险丢掉这两件（ADR-0079 的
  `read_only` 当初就写着「a future `project_move`」）。原生路径也拿到它们——它们不需要 shell。
- 提示词第一条纪律多一句：`project_move` 改名或挪位置、带着你读过的部分走；`project_delete` 删一个，
  没有地方找回来。控制台步骤列表两个动词：删除项目目录文件 / 移动项目目录文件。

## 3. 为什么是这些做法而不是别的

### 3.1 按记录作答，而不是拒绝

拒绝是一个新答案。事件流里看得清楚：#127 拒绝之后，模型的下一步是 #130——同一段脚本加一句注释；
#167 拒绝之后是 #170——再发一次同一条。一个把「已经调用过」读成「被拒了，换个写法」的模型，拒绝
只会给它一个新签名去撞。按记录作答给它的是它上次没读的那个答案，一字不差，外加一句它是同一个答案；
如果它连这个也读不进去，第三次收走工具让它写字。测过的差别：同样的六条命令，旧机制十张卡、23 步、
`RunFailed`；新机制四张卡（#1 #2 #3 #4 各一张）、大约 14 步、`RunCompleted`。

### 3.2 用 `world_version` 而不是次数

「写完之后再读」是编码 agent 最常见的合法重复，也是旧机制放三次派发的全部理由。但三次是个上限，
不是判据：第四次写完之后的第四次读会被拒。版本号把判据换成了正确的那个——中间有没有跑过可能改变
答案的东西——于是合法的重复不再有上限，不合法的一次也不派发。

它保守在错的方向上：一个不改任何文件的 `project_run`（`python3 -c print`）也会让版本号加一，所以
`read F`、`run`、`read F` 第二次真读。这是运行时不知道命令做了什么的诚实代价，宁可多读一次。

它的一个真代价见 §4：同一条命令连发两次做轮询，第二次不跑。

### 3.3 第三次是收走工具，不是 `RunFailed`

这一轮已经写了文件（工作区里有），杀掉它丢的是报告。收走工具是仓库里已有的形状：工具配额用尽时
运行时就是这么做的，注释里那句「write what you have」是本 ADR 直接引用的。`RunFailed` 留给一种情形：
没有工具还提出调用。

### 3.4 `unattended` 不从风险表里减

如果做成「拿掉 `destructive`」，策略引擎对 `project_run` 就永远不问 `requires_approval`，什么也拦不住，
那六种形状就没地方站；`PermissionResolved` 上也只剩一个 `within_submitted_envelope`，事后分不出这条命令是
人点的还是规则放的。信封多一个字段，风险表原样，引擎多问一句参数——三样都是加法，ADR-087 的
不变量和它的测试一个字不动。

### 3.5 形状是一张正则清单，不是分类器

Claude Code 的 auto 模式在这个位置放的是一个模型。本仓库没有第二个模型可花，也不该让第一个给自己的
命令打分。清单短、有注释、有 78 条用例，而且它的覆盖面是明说的：它拦的是「跑了会赔掉工作」的六种
形状，不是「所有危险的命令」——绕过它的命令，能赔掉的也只是那个文件夹（§4）。

### 3.6 为什么不干脆在所有部署上提供 `unattended`

因为 ADR-0077 那句话在原生路径上是真的：命令跑在用户自己的机器上，带着他们的 `PATH`、`SSH_AUTH_SOCK`
和凭据。Claude Code 在那种机器上也提供 `bypassPermissions`，但它用一个吓人的 flag 名和一行警告来卖；
这个仓库的选择是**不卖**：用命令跑在哪决定这一档在不在，和 `project_run` 本身走的是同一个事实。

### 3.7 为什么 `project_delete` 不要回执，`project_move` 是 `write`

回执（ADR-0078）是「模型刚看过这些字节」的凭据，用来挡「没读就覆盖」。删除不是关于内容的陈述——
`rm build/out.bin` 里没有人读过 out.bin，那正是要删它的原因。挡删除的是人，这才是 `destructive` 的
意思。移动什么也没丢，也落不到已有文件上，所以它和写一样是 `write`，而回执跟着文件走是为了
「读过、改名、再写」这条最常见的路不被自己的闸拦住。

## 4. 代价与已知缺口

- **同一条命令连发两次，第二次按记录答**（F-41）。`sleep 3; curl localhost:8080` 连发两次做轮询，
  第二次不会真去问端口。换一个 sleep 的秒数就是新问题；提示词没有为此加字，因为编码回合的 360 秒里
  轮询本来就少见。
- **形状清单是静态的**（F-42）。一条绕过六种形状的破坏性命令在放手做的回合里会直接跑。它能赔掉的是
  runner 容器里那个项目文件夹——ADR-0115 把它限定在那——而不是别的东西；这是这一档只在那条路上
  提供的原因，也是清单可以短的原因。
- **`project_move` 只移文件。** 目录改名走 `project_run mv`（Compose 档）；原生 Windows 上没有。
- `ToolCompleted` / `ToolFailed` 各多一个字段，`AuthorizationEnvelope` 多一个字段，都带默认值，
  老事件读回来不变；`tests/domain/golden/domain_v1.json` 与 `tests/cli/golden/demo_tool_round.jsonl`
  相应多了三行和一个键。事件 schema 版本不动。
- 基础集的风险上限变成 `destructive`，意味着一个没打开 `shell_tools_enabled` 的项目回合信封也是
  `destructive` 上限——由工具推导，`project_delete` 每次都停在人面前，行为上没有变宽。

## 5. 证据

- 复现：`docker exec agent-workbench-runner-1 sh -c 'cd "/projects/windows测试" && python3 -c "…"'`
  跑本 ADR §1 的 #4，退出码 0，14 行区段统计——模型重复的是一个正确答案。
- 事件流：`docker exec agent-workbench-postgres-1 psql … "select sequence, event_type, … from events
  where run_id like 'run_344%3232'"`——25 次工具调用、3 条 `ToolFailed` 全是 `already called with these
  arguments`、`RunFailed`。
- 测试（本批新增或改写）：`tests/runtime/test_agent_runtime.py`（按记录作答、写之后重新派发、
  命令不再问第二次、第三次收走工具且完成、没有工具还提出就失败、拒绝也进记录、参数顺序不买第二次
  派发）；`tests/runtime/test_tool_gateway.py`（`replay` 三条）；`tests/domain/test_commands.py`
  （78 条形状用例）；`tests/contracts/test_policy_engine.py`（unattended 四条）；
  `tests/application/test_code_session.py`（unattended 的信封与提示词、原生路径拒绝、计划回合不提、
  七件工具）；`tests/api/test_code_api.py`（destructive 的长期规则 200、原生路径 422 且转录不留问题、
  提供时 200）；`tests/adapters/test_project_file_store.py`（`move` 七条）；
  `tests/adapters/test_project_tools.py`（删与移九条）；前端 `CodePage.test.tsx` 四条、
  `stepGroups.test.ts` 四条。
- 从控制台真发一轮（2026-09-13，重建后的栈，`run_750c…`）：写、改名、同一条 `wc -l mario.html`
  两次、删——5 次调用、零张审批卡，第一次命令 `PermissionResolved reason_code=unattended_turn`，
  第二次 `ToolCompleted replayed=true` 且没有任何 `PermissionResolved`，`RunCompleted`。
  同一次实测发现起始屏（还没有会话）画不出第四档，当夜以能力清单多一行 `code.unattended` 补上
  （§2.3 的「在哪提供」由此多了一个读者：起始屏）。
- 门禁数字见 `docs/status.md` 第八十四批。
