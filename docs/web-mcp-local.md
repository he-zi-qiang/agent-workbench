# 本地只读取用 MCP：从协议探测到真实 Task

这条路径实现的是"Agent 自己上网取材"：`researcher_external` 调用
`mcp_web_fetch_page` 读一个它自己指定的页面，或用 `mcp_web_download_document`
把一个文件收进 ArtifactStore。架构决定见
[ADR-027](./adr/0027-read-outward-write-inward.md)。

**它只读，不写。** 两个工具都是 GET，都不填表、不点击、不下单。这条线不是保守，
它是让"整个图节点重放"继续成立的原因：重放会再取一次页面，拿到的是那一刻的内容，
而不是第二次没人能收回的效果。

## 1. 启动、停止与探测

在一个独立终端前台启动 loopback server：

```bash
scripts/dev.sh web-server
```

服务只监听 `127.0.0.1:8767`。按 `Ctrl-C` 停止；不写 PID 文件，也不在后台留下一个
之后忘记关闭的进程。

另一个终端运行一次完整探测：

```bash
scripts/dev.sh web-check
```

它先请求 `http://127.0.0.1:8767/health`，再通过官方 MCP Client 对
`http://127.0.0.1:8767/mcp` 完成初始化和 `tools/list`，并断言目录里两个工具都在。
成功输出的形状是：

```text
health  200 http://127.0.0.1:8767/health
mcp     http://127.0.0.1:8767/mcp
tools
  download_document: Download one file by URL and return its bytes. ...
  fetch_page: Read one web page and return its readable text. ...
```

只看 HTTP 健康不足以证明 MCP 目录有效，只跑 `tools/list` 又不容易区分进程未启动与
协议错误，所以检查命令保留两项结果。

## 2. 本地 profile 的能力边界

`config/config.web-local.toml` 已显式开启；普通 `config/config.local.toml` 保持 MCP
关闭：

```text
alias              web
remote tools       fetch_page, download_document
local tools        mcp_web_fetch_page, mcp_web_download_document
principal scope    mcp:web
endpoint           http://127.0.0.1:8767/mcp
retryable effects  true
audience           research  →  researcher_external，不是 writer
```

**它与 `config.word-local.toml` 是两份文件，不是一份。** 每一份都把自己的工具名冻进
每个新提交 Task 的授权信封，合成一份就等于把每个 Task 同时按两者加宽。

**`audience` 决定进哪个 Agent，信封决定能到多高。** 信封里仍然列出全部已配置的名字；
`audience=research` 说的是只有 `researcher_external` 够得到它们。writer 拿不到读网页的
工具，researcher 也拿不到 Word 渲染器——两个方向各有一条测试钉住。

## 3. 边界，写在前面

- **SPA 取不到正文。** `httpx` 拿到的是原始 HTML，靠 JavaScript 渲染的页面内容不在里面。
  这是 ADR-027 §3.5 记下的**已知边界，不是 bug**：真浏览器内核带来新依赖、新进程生命周期
  和新威胁模型，不该伪装成一个配置项顺手打开；
- **PDF、xlsx 不走 `fetch_page`。** 它们没有可抽的正文，硬抽会得到一团读起来像成功的乱码。
  `fetch_page` 遇到非文本 content-type 直接拒绝并指向 `download_document`；
- **下载有 8 MiB 上限，超了是错误不是截断。** 截断的文档是坏文档，而下一步会把它当好文档读走；
- **地址闸门先于连接。** 主机名解析后逐个判，只有全局可路由的地址放行，重定向每一跳单独再判。
  一个解析到内网的 URL 取不到——包括模型被网页里的一句话诱导去取的那种。

最后一条有个明确的没关上的部分：这里解析一次，HTTP 客户端连接时还会再解析一次，所以
**DNS rebinding 不在防护范围内**。关掉它要改传输层。

## 4. 无模型 key：只能验 MCP，不是假装跑了 Task

没有 `AW_SECRETS__DEEPSEEK_API_KEY` 时：

```bash
scripts/dev.sh web-worker
# web-worker requires AW_SECRETS__DEEPSEEK_API_KEY; refusing a demo graph
```

它直接退出而不是退回 demo 图。demo 图不调模型，也就不会提议任何工具——用它"跑通"一次
只能证明进程起得来，把那个当成 Task 验收是自己骗自己。

`scripts/dev.sh web-check` 在没有 key 时照样可用，因为它验的是协议不是 Task。

## 5. 真实 Task 验收

需要：PostgreSQL、Qdrant、`AW_SECRETS__DEEPSEEK_API_KEY`，以及一个带 `mcp:web` scope
的 principal。四个终端：

```bash
scripts/dev.sh services
scripts/dev.sh migrate
scripts/dev.sh web-server
scripts/dev.sh web-api
scripts/dev.sh web-worker
```

提交一个必须读某个具体页面才能回答的 Task（例如"读 <某文档 URL>，总结它的三条主要结论"）。

**这个节点会跑两半**（ADR-032）：先是 ADR-020 那次确定性的 `external_search`——本 profile
没开 `research.enabled`，所以它在 Gateway 被拒，事件流里看得见——然后才是带
`mcp_web_fetch_page` 的那次 agent run。第一半那对 `ToolProposed external_search` /
`PermissionResolved deny` 是预期内的，不是故障。

那次 agent run 交出的不是散文而是 JSON 证据条目（`{"items":[{"url","title","text"}]}`），
因为 `synthesize` 读的是 `EvidenceBundle`。解析不了的输出会让节点失败而不是被当成
"没读到"。

**任务成功不等于验收通过。** 至少核对这四条：

| # | 要看的 | 怎么看 |
|---|---|---|
| 1 | `researcher_external` 真的拿到了工具 | 该节点的 `RunStarted` 事件里 `tool_names` 含 `mcp_web_fetch_page` |
| 2 | 它真的调了，并且走完了闸门 | 同一个 run 里出现 `ToolProposed → PermissionResolved → ToolStarted → ToolCompleted` 四件套 |
| 3 | 下载的文件真的落了库 | artifact store 里有该 artifact，`media_type` 与源一致，提交者能下载 |
| 4 | 少了 scope 会被拒 | 用一个不带 `mcp:web` 的 principal 提交同样的 Task，同一工具在 Gateway 被拒绝 |

第 1 条单独不够：`tool_names` 里有名字只说明它被广告了。第 2 条单独也不够：事件齐全但
`researcher_external` 从没拿到工具的话，那是 writer 在调。两条一起看才说明"读网页这件事
发生在研究节点上"。

第 4 条是唯一一条**不需要模型也能验**的，`tests/adapters/test_mcp_scope_refusal.py`
已经把它自动化了：同一个 binding，principal 带 `mcp:web` 时放行、不带时被 Gateway 拒。
前三条要真跑，因为它们要的是事件流里的事实。

**已经真跑过的**（2026-08-09，task_d3dc69b3…，DeepSeek + 本机 PostgreSQL/Qdrant）：
第 1、2 条成立——`research_external` 的 `RunStarted` 带
`["mcp_web_download_document","mcp_web_fetch_page"]`，同一个 run 里
`mcp_web_fetch_page` 走完了四件套，读到的正文变成一条 `source="external"`、URL 就是
被读那一页的证据，最后 writer 的报告是从它写出来的。**第 3 条（`download_document`
落库）还没有真跑过**，别把它当已验证。

## 5.1 三件会让它跑不起来的事，都不是 bug

- **每次 agent 调用的 token 上限。** 默认 16000 装不下一个读网页的节点：一页正文
  20–50 KB，两次读就是约 28000 tokens，run 会以
  `budget_exceeded: token_budget` 结束在半句话上，而工具其实全部成功。
  `config/config.web-local.toml` 把 `multi_agent.max_tokens_per_agent_invocation`
  提到 120000，只在这份 profile 里提；
- **挑一个真有正文的页面。** 用搜索引擎首页当靶子会得到一屏 CSS，模型会反复重读、
  把预算耗在叙述上。`robots.txt` 这类小的纯文本页是最省事的验收靶子；
- **writer 想写工作区要另一个 scope。** `workspace_write` 在事件流里被拒的
  `missing_permission_scope` 说的是 principal 没有 `workspace:write`，与本 profile 无关。

## 6. 常见问题

**`web-check` 报 health probe did not succeed。** server 没起，或者起在别的端口。
`scripts/dev.sh web-server` 前台跑，看它自己的输出。

**Worker 起来了但 `researcher_external` 没有工具。** 看 Worker 日志里有没有
`mcp_connection_failed`：server 不可达时 Worker 照常启动、只是不带这组工具（ADR-025 的
fail-soft）。这是设计，不是故障——但它意味着"任务跑完了"不能当作"工具用上了"。

**取一个页面返回空。** 先确认它不是 SPA（§3 第一条）。再确认它不是 PDF：那种情况会得到
一条明确的拒绝并指向 `download_document`。

**取一个内网地址被拒。** 这是闸门在工作。它连"解析到内网的公网域名"一起拒，包括重定向
过去的那一跳。
