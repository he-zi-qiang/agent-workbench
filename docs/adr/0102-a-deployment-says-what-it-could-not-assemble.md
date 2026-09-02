# ADR-102：一台部署要说得出自己没装配起什么

- 决策点：一台把一半能力装配失败的部署，从控制台上看和一台完好的一模一样——要不要
  让这个进程把「我没装配起什么、为什么、怎么补」答成一条 HTTP 事实；以及能力开关
  能不能写死在 `compose.yaml` 里
- 状态：**接受**，新增 `GET /v1/system/capabilities`（只读、只答这个进程自己）；
  Chat 的联网搜索开关**不写进 Compose**，由容器启动脚本探到 key 之后再决定
- 日期：2026-09-01
- 影响：新增 `apps/api/routes/system.py` 与控制台「运行状态」里的能力清单；
  `bootstrap/provider_key.py` 新增 `usable_key_present()`；新增
  `docker/provider_key_present.py` 与 `docker/run-api-local.sh` 的一段判定；
  `compose.yaml` 的 api 服务透传 `AW_RESEARCH__ENABLED`；known-gaps 新增 D-08
- 依赖：[ADR-093](./0093-a-console-may-read-what-the-next-task-would-be-allowed.md)
  （同一形状：把进程配置投影成「下一个任务会被允许什么」）、
  [ADR-101](./0101-the-console-may-hand-over-a-key-it-can-never-read-back.md)
  （同一前提：这个端口是受控本机，控制台可以谈论自己）、
  [ADR-021](./0021-chat-web-search.md)（`web_search` 只在配了 research 时存在）

---

## 1. 背景：一台看起来完好、其实半边没接上的控制台

Windows 上双击 `scripts\stack.cmd`，Docker 默认栈起来，控制台开在 `/ui/`：六页
都在，Chat 能答，任务能提交、能跑到 `succeeded`。

而这台部署里**没有联网搜索、没有 Word/web MCP、没有沙箱、没有知识库检索**，两个
Task Worker 是 `--demo` 合成 Worker。这些不是坏了，是启动那一刻就没装配——镜像里
没有 embedding extra，配置里没有 `[[mcp.servers]]`，环境里没有
`AW_RESEARCH__ENABLED`。

问题不在于少了这些，而在于**界面上没有任何一处说得出来**。缺席的能力今天只会用三
种方式把自己说出来，而使用控制台的人一种都看不到：

| 缺席时发生什么 | 谁看得见 |
|---|---|
| 启动日志里的一行（`task_worker_grounding_unavailable` 之类） | 只有翻容器日志的人 |
| 模型从不提起某件工具 | 没人；模型不会说「我本来该有个工具」 |
| 一句「我没有联网查询功能」 | 使用者看见了，并且**读成了模型坏了或者 key 失效** |

最后那一行是这份 ADR 的直接起因：一个人拿着一台 Chat 正常工作的控制台，去查 key、
查网络、查模型，唯独查不到「这个进程从一开始就没造过那件工具」。他最后是靠
`RunStarted.tool_names=[]` 这个事件字段定位的。

**那个字段并不是没被渲染过。** `components/stepDetail.ts` 早就把它画成一行「可用
工具：无」，摘要还写着「没有可用工具」——但它是**一次回合的事实**，藏在一步的详情
里，要先展开才看得见，而且它只说「这次没有」，不说「这台部署从来没有」，更不说为
什么、以及怎么才能有。这份 ADR 补的正是这三样：**部署级、带原因、带补法**。

**每一条事实都已经在进程里了。** `chat_unavailable`、`rag_unavailable`、
`config.research`、`config.task.default_authorization_envelope`——全是装配时就决定
并且已经记在 `ApiDependencies` 上的字段。缺的从来不是事实，是读者。

## 2. 决定：三个状态、两个层级、只说名字不说地址

### 2.1 `unknown` 是一个答案，不是前两个的四舍五入

这个进程看不见另一个进程。它不知道有没有 Task Worker 在跑，更不知道那个 Worker 是
不是 `--demo` 起的——本系统没有任何 Worker → 控制平面的上报通道（新登记的 [D-08](../known-gaps.md)）。

于是 `task.worker` 这一行答 `unknown`，并写明「从这里看不出来」以及哪条命令看得出
来。把它答成 `absent` 是替另一个进程作它没作过的证；答成 `available`（因为 API 收
得下提交）更糟。这与「提交任务」单独成行是同一个决定的两半：**能提交和有人跑是两个
问题**，一台部署完全可以只成立前一个，而这正是这台 Docker 栈的样子。

### 2.2 `core` / `optional`：这是产品自称是什么，和它还能被要求做什么

分层不是装饰，是这份报告要回答的第二个问题。看见「缺了七样」不能行动；看见「缺的
七样里有两样是核心」才能。所以核心缺失在页面上是红的，附加缺失是灰的——**附加项缺
席是一个选择，不是一处故障**，把两者画成同一种红，这一页就又变回了一份让人挨个去查
的清单。

分法写在路由里，不写在浏览器里：`chat.direct`、`chat.knowledge_base`、
`knowledge.search`、`task.submit`、`task.worker` 是核心，其余七行是附加。一行在两层
之间移动是一次产品决定，应当**必须改这份清单**才做得到，而测试钉住了这一点。

### 2.3 只说名字和状态，永不说地址和值

`/health/ready` 刻意不返回原因，理由写在那个文件里：那是给编排器的探针，而原因描述
的是部署内部。这条路由**返回原因**，靠的不是推翻那条理由，而是 ADR-101 已经写下的
前提——同一个端口已经在提供 `/v1/settings/provider-key`，它回答一把 key 存不存在、
末四位是什么、放在哪个路径。

这条路由不做的是：不出现任何地址、DSN、URL 或凭据。一台 MCP 服务器在这里只以 alias
和它给任务的工具名出现，永远不带 endpoint。这条规则写在模块 docstring 的第二段，因
为**下一个往这里加行的人正是它会被破坏的地方**。

### 2.4 任务那几行读的是信封本身，不是对同一份配置的第二次推导

`task.external_search` / `task.mcp_tools` / `task.sandbox` / `task.delegation` 全部
从 `config.task.default_authorization_envelope.allowed_tools` 读出——那正是下一个任
务提交时会被冻结进去的那个元组。所以这一页不可能和任务真正拿到的授权走散：它就是
同一个值。

MCP 那一行取的是「信封里减去内建工具名之后剩下的」。这是构造上精确的（信封本来就是
内建名 + `mcp_tools` 拼出来的），代价是那张内建名单必须维护——所以钉住它的测试不是
对着名单写的，是对着一个**真实构造出来的信封**写的：哪天有人加了第五个内建工具而忘
了这张表，这条测试会先红，而不是让控制台把那个内建工具叫成 MCP 工具。

**这不与 [ADR-096](./0096-a-session-may-hold-fewer-tools-than-it-was-offered.md)
拒绝过的那件事冲突。** 那次拒绝的是把 `[[mcp.servers]]` 投影进 **Code 会话**的工具
路由——那些绑定装在 Task Worker 上，Code 回合永远拿不到，报出来就是承诺一件够不到
的东西。这里报的是**任务**信封，而任务正是那些绑定唯一生效的地方。

### 2.5 报的是「装配成了什么」，不是「此刻还活着没有」

上面那三格健康检查 15 秒一次，因为它们问的是会变的事；这份清单不轮询，因为它问的是
启动那一刻定下的事。只有重启会改变它——而重启会重新拉一次。

## 3. 同一批的第二个决定：有一个开关不能静态写死

Chat 的 `web_search` 只在 `[research]` 配了的时候才存在（ADR-021）。而
`research.enabled` **在没有 key 时是启动错误**，不是降级启动——那条拒绝本身是对的，
它挡的是「配置描述了一个不存在的系统」。

两条规则合在一起，在容器里长出一个新的形状：

> 把 `AW_RESEARCH__ENABLED=true` 写进 `compose.yaml`，一台全新的栈就起不来了——它
> 还没有 key，而**存 key 的那个页面就在这个拒绝启动的进程里**。

所以这个开关由 `docker/run-api-local.sh` 在进程启动前判定：探到可用的 key 才打开，
探不到就明说「暂时关着，存了 key 再重启」。探测问的是包本身
（`bootstrap/provider_key.usable_key_present()`，占位串规则从 settings 导入而不是
重述），不是 shell 里重写一遍那张占位前缀表。`scripts/dev.sh demo-api` 对同一件事早
就是同一个判断，这只是那个判断第一次进到容器里。

操作者仍然说了算：`AW_RESEARCH__ENABLED` 从 Compose 透传，显式值原样保留，只有未设
或空串才由脚本决定。代价也写在脚本注释里——搜索花的是这把 key 的钱，每回合以
`research.max_uses` 为界。

## 4. 明确不做的

1. **不加 Worker 上报通道。** 这份改动只让「看不见」变成一句写出来的话，没有让它变
   成看得见。真做需要 Worker 往控制平面写自己的能力与心跳，那是一个新的事实源，要它
   自己的 ADR。D-08 记的就是这条。
2. **不报「工具真的能用」。** 信封说的是「允许」，Worker 有没有注册那件工具是另一个
   进程的事（`profile_with_dynamic_tools` 正是为这个差别存在的）。这一页答不了，也不
   假装答得了。
3. **不做认证。** 与 ADR-101 完全同一条前提：任何碰得到这个端口的东西都能读这份报
   告。它不含凭据、不含地址，但它确实描述这台部署的形状。不缓解，写下来。
4. **不给这份报告加写口。** 页面上没有任何一个开关能改这里的任何一行。能力由配置和
   启动决定，一个能从浏览器改能力的控制台，会让「这台部署是什么」不再有单一答案。
