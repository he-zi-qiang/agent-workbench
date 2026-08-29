# 决策记录

架构基线第 14 节的 ADR-001～011 定义了基线本身：它们是 v1.3 成文时就已经确定
的边界。这个目录放的是**实施过程中**做出的决定——计划里排期的决策检查点，以及
任何改变事实源、控制平面、Runtime owner、fusion owner 或恢复语义的选择。

两者编号连续，不重开一套。基线 ADR 留在基线里，因为它们是基线的组成部分；
新决定放在这里，因为它们各自有自己的触发时机和重审条件。

| ADR | 决策点 | 状态 |
|---|---|---|
| [ADR-012 身份边界](./0012-identity-boundary.md) | D0（WP04 前） | 接受 |
| [ADR-013 BGE-M3 sparse 必须来自 FlagEmbedding](./0013-bge-m3-sparse-encoder.md) | WP05-01 SparseEncoderPort | 接受 |
| [ADR-014 自研 PostgreSQL checkpointer](./0014-own-postgres-checkpointer.md) | WP06-06 checkpointer | 接受 |
| [ADR-015 唯一写节点的授权上限](./0015-export-authorization.md) | WP10-07 `export_artifact` | 接受 |
| [ADR-016 自研 ingestion 与 retrieval](./0016-self-built-retrieval.md) | 复核 ADR-003 与实现的差异 | 已被 ADR-017 取代 |
| [ADR-017 LlamaIndex 主 RAG + RAGAS 离线评测](./0017-llamaindex-primary-rag.md) | 恢复作品集 RAG 技术路线 | 接受，重新确认 ADR-003 |
| [ADR-018 无接地对话是显式形态](./0018-ungrounded-chat-shape.md) | Chat 交互形态；`chat.retrieval_shape` 的取值集合 | 接受 |
| [ADR-019 提示词与工具参数记进事件流](./0019-run-step-transparency.md) | 运行步骤的可观察内容；`runtime.record_step_inputs` 的引入 | 接受 |
| [ADR-020 DeepSeek `web_search` 接上外部检索](./0020-external-web-search.md) | `ExternalSearchPort` 的真实实现；Task 授权信封是否放行 `external_search` | 接受 |
| [ADR-021 Chat 的联网搜索只出现在兜底分支](./0021-chat-web-search.md) | Chat 要不要联网；"要不要联网"由谁判断；用了网页的回答算不算接地 | 接受 |
| [ADR-022 工具额度用尽是收走工具](./0022-tool-ceiling-closes-the-toolbox.md) | `max_tool_calls` 用尽时 run 应该怎么办；`max_tool_calls < max_steps` 是不是配置错误 | 接受 |
| [ADR-023 无接地作答只有一个实现](./0023-direct-chat-reaches-the-web.md) | `direct` 形态能不能联网；"无证据作答"由几份代码实现 | 接受，扩展 ADR-021 并消耗 ADR-018 的重审条件 |
| [ADR-024 一个 Worker 进程可以同时跑多个 Task](./0024-task-worker-lanes.md) | Task 并发执行的拦路石是什么；`worker_concurrency` 还该不该被钉死在 1 | 接受 |
| [ADR-025 MCP 工具在启动时冻结成本地绑定](./0025-mcp-adapter.md) | `optional_labs.mcp_adapter` 的真实实现；第三方 schema 不合规时是放宽校验还是丢掉工具 | 接受 |
| [ADR-026 Word 文档是 MCP 返回的不可变 Artifact](./0026-word-docx-is-an-mcp-artifact.md) | 项目自有 Word Server 如何保留 Gateway、scope、事件和 Artifact 所有权边界 | 接受（本地 Optional Lab） |
| [ADR-027 只读取外部世界，写只写进自己的 artifact](./0027-read-outward-write-inward.md) | Task 能不能自己取页面、下载文件、生成 Office 文档；这三件事的共同边界 | 接受 |
| [ADR-028 任务工作区是可变的名字压在不可变的字节上](./0028-task-workspace.md) | Agent 能不能在一个 Task 内积累并加工产物；可变状态与"节点整体重放"怎么共存 | 接受 |
| [ADR-029 沙箱是纯函数，断网是它保持纯的原因](./0029-ephemeral-sandbox.md) | Agent 能不能跑代码；跑代码怎么不把前面几条 ADR 的重放保证作废 | 接受 |
| [ADR-030 会干活的节点由成本和时限管](./0030-working-nodes-are-governed-by-cost.md) | 带工具迭代的 run 该由什么约束；`max_steps` 域上限 100 还合不合适；整文件覆写够不够 | 接受 |
| [ADR-031 通用任务走第二张图](./0031-a-second-graph.md) | 不是"写调研报告"的任务该走什么形状；模型能不能决定自己的步骤顺序 | 接受 |
| [ADR-032 外部研究节点在拿到工具时是一个 Agent](./0032-the-external-researcher-is-an-agent.md) | `research_external` 跑什么；ADR-027 给它的动态工具怎样才真的到得了模型 | 接受，兑现 ADR-027 §3.3 |
| [ADR-033 融合仍然只发生一次，但那一次归我们做](./0033-fusion-ranks-are-ours.md) | 混合检索为什么跨重建索引不可复现；RRF 的臂内名次该由谁决定 | 接受，取代 ADR-016 中"融合只在 Qdrant 里"一条 |
| [ADR-034 读不出来的时候再问一次](./0034-a-structured-node-asks-once-more.md) | 答案外面裹了一句话时结构化节点该怎么办；ADR-032 §3.3 的严格严在哪一件事上 | 接受，收窄并兑现 ADR-032 §3.3 |
| [ADR-035 答案不是摘要](./0035-an-answer-is-not-a-preview.md) | 一个 run 的答案该有多大；ADR-019 那个 4096 管的是什么 | 接受，收窄 ADR-019 "有界"的适用范围 |
| [ADR-036 提交前的预判决定形态](./0036-triage-decides-the-shape.md) | graph 与 wants_report 这两个提交时决定由谁做出 | 接受，取代 ADR-031 §2.3 |
| [ADR-037 图谱只提名 chunk](./0037-the-graph-nominates-chunks.md) | 跨文档检索怎么做；「抽实体关系合并成一张图」要不要照搬 | 接受（已实现）；四轮消融未达标，`rag.graph.enabled` 保持关闭 |
| [ADR-038 导出闸门守的是一份清单](./0038-the-export-gate-guards-a-list-not-a-boundary.md) | 导出必须经人工审批吗；它算不算 ADR-031 §2.4 说的那种"边界" | 接受，收窄 ADR-031 §2.4；移走 ADR-015 推理的一个前提 |
| [ADR-039 配置里的一个指标名字是一句承诺](./0039-a-metric-name-is-a-promise.md) | 配置声明的评测能力和实现对不上时以哪个为准；`[evaluation]` 该不该承载路线图 | 接受，配置 schema `1.13` → `1.14`；`ragas_enabled` 写 `true` 改为加载失败，`rag_metrics` 只接受注册表里算得出来的名字 |
| [ADR-040 调用额度先扣后花](./0040-a-task-pays-before-it-calls.md) | 一次 agent invocation attempt 的边界是什么、记账点在哪一层；跨 retry 与 reclaim 的计数器落一列还是一张表；崩溃重放算不算新花费；上限读 Task 快照还是进程配置；额度用尽是 `failed` 还是 `dead_letter` | 接受，兑现 ADR-030 §3 点名的那道未装的闸；与 ADR-022 方向相反 |
| [ADR-041 迟到的心跳没有资格续租](./0041-a-late-heartbeat-may-not-renew.md) | 一个停摆过又回来的 Worker 凭什么还能说自己活着；watchdog 的探针、两级阈值、abort 三件事本批做哪几件；那条启动校验写在哪一层才可能真的红 | 接受，收窄 WP08-12 的 watchdog 部分（本批不做 watchdog）；更正 `event_loop_lag.py:59-63` 自陈理由里的一个事实错误 |
| [ADR-042 阻塞是 Adapter 的属性，不是调用点的](./0042-blocking-belongs-to-the-adapter.md) | `AdapterCallRunner` 是不是「所有调用的唯一入口」；有界池的界从哪来、放哪个 section、几个字段；饱和之后是拒绝还是排队；加了配置字段要不要抬版 | 接受，收窄 `implementation-plan.md:900` 那句「唯一入口」；新增 2 个配置字段而配置 schema 保持 `1.14` |
| [ADR-043 读 Word 的那半边是本地工具](./0043-docx-reading-is-a-native-tool.md) | 读取器是复制 ADR-027 §3.4 的渲染器形状再起一个 MCP server 还是 native 工具；唯一那份 docx→Markdown 实现放哪一层才不让 Worker 反向依赖 API 应用；给模型的文本上限跟谁对齐 | 接受，沿用 ADR-026 §2.2 并订正 §2.4 的「保留来源关系」；只定形状与搬家，不决定入口与编辑形状 |
| [ADR-044 先有远端部署，才谈得上生产身份与远程对象存储](./0044-no-remote-no-production-identity.md) | 生产身份与 S3 后端这一批做不做；三处 `backend != local` 的拒绝算不算完成；presigned 与既有 `ArtifactStorePort` 语义能不能共存 | 接受，**明确不做**（沿用 ADR-041 的形状）；只补三条拒绝 + 一条对照的回归测试；记录 ADR-012 已被 ADR-015 事实性推翻 |
| [ADR-045 版面是一次转换，不是第三条 docx 解析路径](./0045-a-layout-is-a-conversion-not-a-third-parser.md) | 保真度从服务端 LibreOffice 转 PDF、前端渲染库还是只数损失来；新路径会不会变成全仓第二条 docx 解析路径；第一个非 Python 外部依赖的代价谁承担 | 接受，选服务端转换并复用 `preflight_docx` 遵守 ADR-043 §5；同批落地 ADR-043 §7 的计数；LibreOffice 做成构建期开关默认不装，配置 schema 保持 `1.14` |
| [ADR-046 加法的那一半不许做减法](./0046-the-additive-half-may-not-subtract.md) | `research_external` 读网页那半的 run 停在上限/守卫/传输故障上时节点该不该失败；ADR-032 §3.1 的「纯加法」在失败方向上算不算数；这条豁免按节点给还是按错误给 | 接受，收窄 ADR-032 §4；`_decoded` 多一个 `halted` 参数，解码器与图形状均不变 |
| [ADR-047 会话的名字来自第一句话，而且只来自第一句](./0047-a-session-is-named-by-its-first-sentence.md) | 编码会话的名字由哪一层产生；「第一句指令」和「人工改名」谁能覆盖谁；这份列表放浏览器还是服务端 | 接受；关闭 F-06 的 Code 那一半 |
| [ADR-048 导出闸门默认关闭，控制台不再有跨任务收件箱](./0048-the-export-gate-is-off-by-default.md) | ADR-038 §4 要求的那份 ADR：把 `export_requires_approval` 的仓库默认改成 `false`；「待我确认」这一页要不要留 | 接受；应答 ADR-038 §4 的第二个重来条件 |
| [ADR-049 评测是一个进程，不是一个 Task](./0049-an-evaluation-is-a-process-not-a-task.md) | 控制台发起的评测该建模成什么；它的状态存在哪；重启之后它变成什么 | 接受 |
| [ADR-051 实时帧没有位置，所以它不许有 id](./0051-a-live-frame-has-no-position.md) | 进程内的 transient 事件该不该到达浏览器；它和"只有 durable 事件有游标"怎么共存；慢读者拖住实时通道时是断开还是别的 | 接受，收窄基线里"溢出即断开"一句 |
| [ADR-052 撤不回的答案才可以边写边给人看](./0052-only-an-unwithdrawable-answer-may-be-shown-early.md) | `AnswerReleaseSink` 是否对每个 Chat 形态都抹掉 `ModelDelta.text`；判据是什么、由谁给出 | 接受，澄清基线 §5 里 answer release gate 的作用域 |
| [ADR-054 摘要没法被同意](./0054-a-digest-cannot-be-consented-to.md) | 停在审批上的调用，参数正文要不要进事件流；靠 `record_step_inputs` 还是别的判据 | 接受，对「事件只描述不复现」开一个有范围的例外 |
| [ADR-075 账本记的是被发起的效果，不是被提议的效果](./0075-a-ledgered-effect-is-issued-not-proposed.md) | `retryable_effects = false` 的 MCP server 该不该经由账本进 Task；ADR-025 §2.7 给自己留的那句重开条件兑现了能不能解锁 | 接受，保留拒绝并收窄 ADR-025 §2.7／§5 的重开条件——卡住的是键，不是载荷 |
| [ADR-076 没人批准过的窗口，不在那张图里](./0076-a-window-nobody-approved-is-not-in-the-picture.md) | 对照 Claude Desktop 的 computer use：合成器过滤要不要抄；批处理原语与视觉通路要不要抄；以及 ADR-070「人一次性批准」为何一直没有实现 | 接受，兑现 ADR-070 §2 并关闭 F-18；拒绝批处理原语与（本轮的）视觉通路 |
| [ADR-090 一个坐标带着它是在哪块屏上量的](./0090-a-coordinate-carries-the-screen-it-was-measured-on.md) | ADR-076 §4 记下、明确不修的那条：截图按显示器给坐标、点击按全局坐标发事件；换算放哪一层才测得了；一个没说清自己在哪块屏上量的坐标该被怎么对待 | 接受，端口下面一律全局点、`Display` 带原点、换算进 domain；并把多屏时省略 `display_id` 由「当作主屏」收窄为拒绝；关闭 F-22 |
| [ADR-091 模型可以挑窗口，但只能在人批准过的那一组里挑](./0091-choosing-a-window-is-choosing-within-a-set-somebody-approved.md) | 再对一次工具面：没有任何工具能改变前台应用（于是跨应用任务走不动）；`list_granted_applications` 没有出口；`_ALLOWED` 比工具表宽 | 接受，工具面 6 → 8；第 3 道检查的含义由「人选了这扇窗」变为「模型在人批准的集合里选了一扇」，并以「前台不在名单里就拒绝激活」收窄；`_ALLOWED` 与 `ScreenPort.move` 收窄；新增 F-29（不启动应用） |
| [ADR-092 能改变屏幕最前面那扇窗的服务器，自己必须是一个应用](./0092-a-server-that-changes-the-front-of-the-screen-is-an-application.md) | ADR-091 的 `activate_application` 真机上一次都没成功；要不要为这一个能力改变服务器的进程形态；改了之后 ADR-076 §2 拒绝 `NSApplication` 的理由还成不成立 | 接受，服务器改为签名的 `.app`、主线程交给 `NSApplication`、uvicorn 挪到后台线程；**推翻 ADR-076 §2 的结论而非它的理由**；关闭 F-30 |
| [ADR-093 控制台可以读到「下一个任务会被允许什么」](./0093-a-console-may-read-what-the-next-task-would-be-allowed.md) | 委派的四个上限每个 profile 都不一样，而前端一条读得到的路由都没有；要不要把进程配置投影成 HTTP 事实，答的是哪一份配置 | 接受，新增 `GET /v1/tasks/capabilities`，只答「下一个任务」；明确不答子代理目录、不答 `max_agent_invocation_attempts_per_task`、不做提交级覆盖 |
| [ADR-094 一个子代理干的活是一行，展开才是九行](./0094-a-sub-agents-work-is-one-row-that-opens.md) | 委派之后子运行与父运行的事件躺在同一个阶段里，而时间线是平的；要不要改 Work 与 Chat 共用的 `StepStream`；一页缺失的 `AgentDelegated` 让页面说不出子运行叫什么时怎么办 | 接受，新增按 `run_id` 连续分段 + `StepStream` 的可选 `runLabel`；不给这个 prop 时渲染逐字节不变；删除「子代理 X：」前缀，叫不出名字的段用 `运行 xxxxxxxx` 兜底且不挂徽标 |
| [ADR-095 人可以看自己的屏幕，模型不可以](./0095-the-person-may-see-their-own-screen-the-model-may-not.md) | 要做一块能读到「批准了哪些应用、此刻前台是谁」的面板，第一个要回答的是：面板可不可以说出一个**没被批准**的前台应用的名字；以及查出这条「不点名」规则三处只成立两处 | 接受，两个方向：人这一侧面板点名，模型这一侧把第三条路补齐（`_require_frontmost` 不再点名）；动作记录是有界内存环、不落盘；传输走 agent-api 只读反代而不给 8768 加 CORS；F-19 保持开着但界面改说「这个进程」 |

> **这张表在 ADR-054 之后就断了：0055–0074 与 0077–0089 都没有行。**（ADR-075、
> 0076、0090、0091、0092、0093、0094 与 0095 各是自己那一批顺手补的，不代表表已经跟上。）那些 ADR 都在同一个
> 目录里，`ls docs/adr/` 就能看见。在补齐之前，**目录本身才是权威清单**，这张表
> 只是一份停在过去某一天的摘要——按它来判断「有没有这条决策」会漏。


## 号段预留 0047–0059

2026-08-13 起的三批改动（过程可见性 / 脱离 Task 的 Code 模式 / 前端收敛）一次性
分配了 0047–0059，避免三条并行的线各自认领同一个号。**已写下的**在上表里；
未写下的号不代表已决定，只代表这批工作认领了它。落地顺序不必等于编号顺序——
上表按编号排，`git log` 按落地排，两者不需要一致。
