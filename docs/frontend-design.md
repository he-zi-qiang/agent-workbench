# Agent Workbench 前端设计与实现基线

> **2026-08-31 修订。** 本文档被 [`docs/README.md`](./README.md) 指定为「改前端」的入口
> 读物，而 §1／§2／§3 描述的是一个 2026-08-20 之后就不存在的外壳：它写着「全局只有两个
> 一级入口」「工作台一页两标签」，让读者去读 `AppShell.tsx` 的 `FIRST_SECONDARY_INDEX`
> 与 `WorkbenchLayout.tsx` 的注释——**两个符号在 `web/` 里都 grep 不到**，后者已于
> ce74730 删除。一个照着它动手的人会去找两个不存在的东西。
>
> 这一版按当前 `web/src/` 重写了 §1、§2、§3 与 §5 的对应部分，并在每处标出原文说了什么。
> 视觉语言（§4）与验收门禁（§6）未变。

## 1. 目标

这个界面服务于校招作品集，不伪装成已经商业化的 SaaS。视觉上借鉴现代 Chat/Work
工作台的克制与连续性，信息上保留工程项目最有价值的部分：真实运行状态、协议边界、
恢复语义、权限事实与“尚未实现”的能力。

全局有**三个**一级入口，各自是自己的路由，**没有标签条**：

- **Chat**（`/chat/:sessionId`）：多轮问答、知识库检索、durable SSE 执行记录与安全引用。
- **Tasks**（`/work/:taskId`）：长任务提交、LangGraph 时间线、HITL 审批、取消与最终产物。
- **Code**（`/code/:sessionId`）：编码会话、工作区文件与实时步骤。

哪几项是一级由 `navigation.ts` 的 `primary: true` 声明，`AppShell.tsx` 用
`NAVIGATION.filter((item) => item.primary)` 取出来；**没有 `FIRST_SECONDARY_INDEX`
这个符号**，也没有一个把 Chat 与 Tasks 合起来的 layout 路由。

> 原文写的是「全局只有两个一级入口」「工作台一页两标签」，分隔线由
> `FIRST_SECONDARY_INDEX` 推导。那次合并已经被推翻：三个工作区各自独立，
> 名字也一并改成了英文（`navigation.ts` 里记着这次推翻的理由）。

知识库、用量、效果评测、计算机、运行状态是证据与操作辅助页，不与主流程争夺视觉层级。

**Computer 页读接口**（ADR-095）：`routes/computer.py` 的 `GET /session` 经
`apps/api` 的**只读反代**服务，`main.py` 无条件挂载，页面用 `useQuery` 每 4 秒轮询，
画出 allowlist、当前前台应用与最近动作三态。它同时仍然说明规则（ADR-070 的四道检查、
tier 推导、拒绝文案、截图预算）——**规则是手抄的，与 `domain/computer.py`／`gate.py`
没有交叉校验**，这一点登记在[已知缺口](./known-gaps.md)里。

> 原文写的是「Computer 是唯一一个不读接口的页面……『这次会话批准了哪些应用』在页面上
> 明确缺席」。ADR-095 已经把那条路修出来了，而这份文档、`CLAUDE.md` 与 `README.en.md`
> 三处都没跟上——这一条尤其要紧，因为它是读者用来判断「要不要给这个页面加端点」的前提。

Approvals 那一页已随 ADR-048 移除：导出审批仍然可以回答，位置是等待中的那个 Task
的详情，而不是一份跨任务的收件箱。跨任务的「待我确认」收件箱后端在服务
（`GET /v1/approvals`）而前端零调用，登记为 F-08。

## 2. 信息架构

```text
全局窄导航（rail）
├── Chat                       ← primary
│   ├── 服务端会话列表（GET /v1/chat/sessions，可改名可删除）
│   ├── 对话正文
│   ├── durable execution activity
│   └── Citation / withheld 发布边界与被隔离位次的披露
├── Tasks                      ← primary
│   ├── Task 列表与提交
│   ├── TaskInput artifact
│   ├── 节点时间线与未知事件
│   ├── 参与的 Agent 面板（委派树，选中一行收窄下面的步骤流）
│   ├── Approval 权威记录（就在等待的那个 Task 里）
│   └── 严格关联的 export_artifact 报告
├── Code                       ← primary
│   ├── 服务端会话列表，名字来自第一句指令（ADR-047）
│   ├── 工作区文件：点开看正文，或下载
│   └── 本轮步骤（持久事件，延迟下限是一个轮询周期）
├── 知识库
│   ├── declare → raw PUT → complete
│   └── /v1/search 检索检查
├── 用量
├── 效果评测
├── 计算机（只读会话面板 + 规则说明）
└── 运行状态 / 本地身份
```

快速跳转（`QuickSwitcher`）按 label + group + description + keywords 匹配，关键词表
在 `navigation.ts` 里按路由索引，**键类型是 `NAVIGATION` 的 `to` 字面量联合**——
新增一个导航项而不给它关键词是一个类型错误。它此前是一条三元链，漏掉 `/usage`，
于是用量页拿的是运行状态页的关键词。

导航项是链接加 `aria-current`，不是 ARIA tabs：它切的是路由，中键、复制链接、
浏览器前进后退和读屏软件的链接列表都必须成立。`.aw-segmented` 保持原样不动——
那是同一个视图内容上的单选组（状态筛选、预览模式），是控件不是导航。

桌面端采用窄全局 rail + 业务上下文栏 + 主内容的结构；移动端改为底部一级导航，Chat
会话横向滚动，Work 将提交表单与任务列表压缩为上半区，详情保留独立滚动。

离开 Chat 会卸载它，因此 SSE 连接会断开——和任何一次导航一样。
`useChatRuntime` 属于 Chat 这棵子树，把它提到全局外壳上会让外壳拥有 chat 状态。

> 原文让读者「先读 `WorkbenchLayout.tsx` 的注释」。那个文件已于 ce74730
> （2026-08-20）随标签条一起删除，`grep -rn 'WorkbenchLayout' web/` 零命中。

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
- **Chat 的会话列表来自服务端**：`GET /v1/chat/sessions`，可 `PATCH` 改名、可
  `DELETE`（4c40474，2026-08-20；`tests/api/test_chat_session_management.py`）。
  仍然留在 `localStorage` 里的只有 `answerMode` / `knowledgeBaseId` 与游标——
  那是一台机器上的偏好，不是会话本身（F-06）。

  > 原文写的是「后端仍没有 Session list/title projection，侧栏必须标『本地列表』」，
  > 并且那个可访问名「本地 Chat 会话」在前端已经 grep 不到。

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
- **LlamaIndex 是确定的主 RAG 框架，RAGAS 是离线评测基线。** LlamaIndex 的检索适配器
  已建成、契约测试与 52 题等价评测都过了（`b9aa057`），但**默认仍未切换**，
  切换是一个待写 ADR 的决定；RAGAS 整条链仍不存在（A-04）。
  页面不展示假集成状态或手抄分数。
- **评测页渲染 API 送来的三类报告**（检索消融 / 任务分流 / 回答质量），
  三张表分开——三套评测问的是三个不同的问题，合成一张就得发明一个共同指标。
  页面上的每一个百分比都必须能在某一份报告里找到，这条由测试守着。
- Health 只解释 `/health/live` 与 `/health/ready` 的实际含义，不推断模型、Worker、索引或
  Trace 状态。

## 4. 视觉语言

- 暖中性画布 `#f2f0eb`、深色窄 rail `#1b1917`、低饱和橙色 `#d97757` 作为唯一主要
  强调色。卡片是白的，画布不是——两者同色时"这是一张卡片"只能靠 1px 边框说；
  rail 是界面里唯一的深色面，它不参与阅读，所以由它承担对比度；
- 小半径边框和轻阴影表达工程面板，避免营销式大渐变与虚假 KPI；
- 正文最大宽度受控，Chat 用户问题靠右，Agent 结果保持文档式阅读；
- 状态颜色只表达 success / warning / danger 三档好坏，外加一档 **evidence（证据 /
  边界）**——它不表达好坏，只表达依据与授权（引用、被调用的工具、只读边界）。
  evidence 是原 info 改的名，同一支青灰，不是新增的第五档；`--aw-info` 仍作为别名
  服务于通用提示。不把颜色作为唯一信息；
- 支持系统 dark mode、键盘焦点、44px 移动端触控目标与 reduced-motion。

`docs/design/` 存着视觉稿。本节写的是 `web/` 里已经存在的东西，那边写的是提议——
两者冲突以本节为准。`agent-workbench-refactor-2026-08-18.dc.html` 提的三件事已于
同日落地（见 `status.md` 第五批），仍未落地的是「这次会话批准的应用」，它需要一条
后端路由。

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
│   ├── usage/       # 三个模式各花了多少 token 和钱
│   ├── evaluation/
│   ├── computer/    # 只读会话面板（ADR-095）+ 手抄的门禁规则说明
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
