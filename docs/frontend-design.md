# Agent Workbench 前端设计与实现基线

## 1. 目标

这个界面服务于校招作品集，不伪装成已经商业化的 SaaS。视觉上借鉴现代 Chat/Work
工作台的克制与连续性，信息上保留工程项目最有价值的部分：真实运行状态、协议边界、
恢复语义、权限事实与“尚未实现”的能力。

全局只有两个一级入口，它们在窄导航里**画成一组**——分隔线在这两项之下，而不是在它们
之间：

- **工作台**：一页两标签。**对话**是多轮问答、知识库检索、durable SSE 执行记录与安全
  引用；**任务**是长任务提交、LangGraph 时间线、HITL 审批、取消与最终产物。两者的
  URL 没有变（`/chat/:sessionId`、`/work/:taskId`），合并的是外壳不是组件——标签条是
  一个无路径 layout 路由，两个页面的内部一行未动。
- **Code**：编码会话、工作区文件与实时步骤。

分组是视觉上的，不是路由上的：Code 仍然是它自己的路由，不在工作台的标签条里。分隔线
的位置由 `primary` 推导（`AppShell.tsx` 的 `FIRST_SECONDARY_INDEX`），而不是写死一个
下标——写死的那版把线画在了 Code **上面**，于是这两个一级入口被画成了两组。

Knowledge、Evaluation、System 是证据与操作辅助页，不与主流程争夺视觉层级。
Approvals 那一页已随 ADR-048 移除：导出审批仍然可以回答，位置是等待中的那个 Task
的详情，而不是一份跨任务的收件箱。

## 2. 信息架构

```text
全局窄导航
├── 工作台（标签：对话 ｜ 任务）
│   ├── 对话
│   │   ├── 本机会话入口（Chat 侧服务端仍无列表 API，见 §3）
│   │   ├── 对话正文
│   │   ├── durable execution activity
│   │   └── Citation / withheld 发布边界
│   └── 任务
│       ├── Task 列表与提交
│       ├── TaskInput artifact
│       ├── 节点时间线与未知事件
│       ├── Approval 权威记录（就在等待的那个 Task 里）
│       └── 严格关联的 export_artifact 报告
├── Code
│   ├── 服务端会话列表，名字来自第一句指令（ADR-047）
│   ├── 工作区文件：点开看正文，或下载
│   └── 本轮步骤（持久事件，延迟下限是一个轮询周期）
├── Knowledge
│   ├── declare → raw PUT → complete
│   └── /v1/search 检索检查
├── Evaluation
└── System / 本地身份
```

标签条是链接加 `aria-current`，不是 ARIA tabs：它切的是路由，中键、复制链接、
浏览器前进后退和读屏软件的链接列表都必须成立。`.aw-segmented` 保持原样不动——
那是同一个视图内容上的单选组（状态筛选、预览模式），是控件不是导航。

桌面端采用窄全局 rail + 业务上下文栏 + 主内容的结构；移动端改为底部一级导航，Chat
会话横向滚动，Work 将提交表单与任务列表压缩为上半区，详情保留独立滚动。

切换标签会卸载另一半，因此 Chat 的 SSE 连接会断开——和今天任何一次导航一样。想把
`useChatRuntime` 提到 layout 里"修"这件事的人请先读 `WorkbenchLayout.tsx` 的注释：
那会让 layout 拥有 chat 状态，把这次刻意避开的组件合并重新造出来。

## 3. 不能伪造的前端语义

### Chat

- SSE 使用 `fetch + ReadableStream`，因为开发身份必须随请求 Header 发送；不用
  `EventSource`。
- cursor 按 tenant、principal、scope set、session 隔离；只有 envelope 结构合法且
  reducer 接受事件后才持久化。
- durable stream 不包含 token delta，因此界面只写“等待安全发布”，不声称逐 token
  streaming。
- `ModelCompleted.text` 永不成为答案。正文只来自 `AnswerCommitted`、
  `AnswerWithheld` 的安全替代文本或完成发布后的同步 `AskResponse`。
- HTTP 与 SSE 可任意先后到达；run orphan buffer、event id 去重和幂等终态归并必须同时
  成立。
- 离开页面不能取消 Ask HTTP，因为服务端把客户端断开解释为取消真实工作。
- **Chat** 后端仍没有 Session list/title projection，侧栏必须标“本地列表”。Code
  已经不是这样了（ADR-047）：它的列表来自 `GET /v1/code/sessions`，名字来自第一句
  指令。两者的差别是真的，不要把这句话当成整个控制台的事实。

### Work

- Task snapshot 决定是否终态；timeline 用 opaque cursor 增量轮询，并按 event id 去重。
- `waiting_approval` 与 `waiting_migration` 不是终态。
- Task objective 从 `TaskSubmitted.input_ref` 指向的 owner-readable `task_input` artifact
  懒加载，不对列表做 N+1 请求。
- Approval GET 是按钮状态与 `decision_version` 的权威来源；网络失败不能伪造成 pending。
- 最终报告只在同一 `tool_call_id` 的 `export_artifact` 提议/开始、`ToolCompleted.artifact`
  和之后的 `TaskSucceeded` 同时存在时展示；最后一个 Model 输出不是任务结果。
- 后端没有真实进度百分比，所以页面展示已观察事件与节点，不画假进度条。

### Knowledge / Evaluation / System

- Upload complete 只表示文档版本与 outbox 已提交，不表示已经索引。
- Search 空结果不区分“无匹配”“尚未索引”与“无可读内容”，避免权限枚举。
- **LlamaIndex 是确定的主 RAG 框架，RAGAS 是离线评测基线；两者当前仍是 Planned。**
  页面不展示假集成状态或手抄分数。
- Health 只解释 `/health/live` 与 `/health/ready` 的实际含义，不推断模型、Worker、索引或
  Trace 状态。

## 4. 视觉语言

- 暖中性画布、深色窄 rail、低饱和橙色作为唯一主要强调色；
- 小半径边框和轻阴影表达工程面板，避免营销式大渐变与虚假 KPI；
- 正文最大宽度受控，Chat 用户问题靠右，Agent 结果保持文档式阅读；
- 状态颜色只表达 success/warning/danger/info，不把颜色作为唯一信息；
- 支持系统 dark mode、键盘焦点、44px 移动端触控目标与 reduced-motion。

## 5. 工程结构

```text
web/src/
├── api/             # HTTP 类型、client、SSE frame parser
├── app/             # provider、路由、身份边界、全局 shell
├── components/      # 安全 Markdown 与通用展示组件
├── features/
│   ├── chat/        # 独立状态机、runtime、cursor store、session stream
│   ├── work/        # timeline reducer/hook 与 Task 页面
│   ├── code/        # 会话、工作区文件查看器、步骤流
│   ├── knowledge/
│   ├── evaluation/
│   └── system/
└── styles/          # token 与响应式样式
```

路由使用 HashRouter，生产构建的 base 是 `/ui/`。Docker 在独立 Node stage 中锁定安装并
构建，Python 最终镜像只复制 `web/dist`；FastAPI 继续同源服务 API 与静态资源，不开放
CORS。

## 6. 验收门禁

```bash
pnpm --dir web install --frozen-lockfile
pnpm --dir web check
pnpm --dir web exec playwright install chromium
pnpm --dir web test:e2e
```

`check` 必须依次通过 ESLint、严格 TypeScript、Vitest 与 production build。Python 的
console mount 测试使用最小静态 fixture，不要求开发者先运行 Node build；GitHub Actions
单独执行前端门禁，并在 Chromium 的桌面与移动视口运行 Chat → Work 外壳冒烟测试。
