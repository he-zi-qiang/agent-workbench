# Agent Workbench 仓库复核报告

> - 复核日期：2026-07-27
> - 主分支基线：`main@4d03f697449b02a2c742e5083ce7ba907d0bd41f`
> - 当前工作分支：`pr-034-knowledge-search-tool@0639fe7ab86a21974ca29c3f7b9e53db1e176cc7`
> - 上一轮复核基线：`main@5f4edd3c7e3ba55a9b5819e130ed5650432bfa91`
> - 复核方式：只读源码审计、提交差异分析、静态检查、类型检查、测试执行与最小复现
> 本报告不修改历史审计报告，也不把未合入主分支的功能标记为主分支能力。
>
> 归档说明（2026-07-28）：第 9 节的 PR-041/PR-042 是当日建议编号，不是后续实际
> 增量编号；实际编号与完成事实以 [实施状态](../status.md) 为准。

## 1. 执行摘要

本轮新增了两项实质代码：

1. 主分支 `#49 / 4d03f69` 增加 `IngestionWorker` 和
   `last_applied_revision` 数据库迁移；
2. 当前未合并分支 `0639fe7` 增加 `knowledge_search` Tool。

这两项都采用了正确的基础方向：

- Outbox 事件只负责唤醒，文档、ACL、Owner 和 Artifact 身份从 PostgreSQL
  当前状态重读；
- Outbox ACK 使用 claim token，陈旧 Worker 不能确认已经被重新领取的事件；
- `knowledge_search` 从 `ExecutionContext` 获取 principal/tenant，不允许模型通过
  Tool 参数选择身份；
- 固定 2-step RAG 与 Agentic Retrieval 复用同一个 `RetrievalService`。

但是，当前仓库仍不能描述为“完整 Chat + Task Agent 平台”，也不能把本轮新增内容描述为
“生产摄取闭环”或“可用的 Agentic RAG”。

当前最准确的阶段定位是：

> 自研框架无关 Agent Runtime + PostgreSQL 持久化 + Dense RAG/Chat Alpha；
> 已增加摄取 Worker 原型和 Agentic Retrieval Tool 原型，但生产闭环、安全提交门、
> 多 Worker 一致性和 Task Mode 尚未完成。

本轮确认：

- 上一轮发现的 SSE/ACL P0 尚未修复；
- 旧文档版本仍可能进入回答；
- `IngestionWorker` 只在测试中被实例化，没有常驻生产进程；
- Worker 的数据库行锁不能 fence Qdrant 外部副作用；
- `knowledge_search` 在常规多 Chunk 输出下可超过 `ToolResult` 上限并失败；
- Agentic Retrieval 丢失 source revision，无法执行最终 ACL 复核；
- LangChain、LlamaIndex、LangGraph、CrewAI、Reranker、RAGAS 与 OTel
  仍未成为真实运行链路。

因此，本轮建议先完成安全与一致性修复，再继续扩展框架覆盖。

## 2. 当前 Git 基线

复核时工作树状态：

```text
branch: pr-034-knowledge-search-tool
HEAD:   0639fe7ab86a21974ca29c3f7b9e53db1e176cc7
main:   4d03f697449b02a2c742e5083ce7ba907d0bd41f
ahead:  当前分支比 main 多 1 个提交
dirty:  否
```

相对上一轮 `5f4edd3`，本轮共变更：

```text
7 files changed
801 insertions
```

新增或修改的主要文件：

```text
docs/status.md
migrations/versions/0005_last_applied_revision.py
src/agent_workbench/adapters/persistence/models.py
src/agent_workbench/adapters/tools/knowledge_search.py
src/agent_workbench/workers/ingestion.py
tests/persistence/test_ingestion_worker.py
tests/vector/test_authorized_retrieval.py
```

其中：

- `IngestionWorker` 已进入 `main`；
- `knowledge_search` 只存在于当前功能分支，尚未进入 `main`。

本轮环境无法访问 GitHub API，因此没有独立重新确认这两个提交的远端 Actions
执行结果。报告中的测试结论以本地执行证据为准。

## 3. 本轮新增能力分析

### 3.1 `IngestionWorker`

实现位置：

```text
src/agent_workbench/workers/ingestion.py
```

新增迁移：

```text
migrations/versions/0005_last_applied_revision.py
```

已经实现的行为：

1. 从 PostgreSQL Outbox 竞争领取事件；
2. 在短事务中锁定并重读 Document 当前状态；
3. 从 PostgreSQL 重读当前 ACL；
4. 使用 Owner 身份读取 Artifact；
5. 调用 `IngestionService` 解析、分块、Embedding 并写入 Qdrant；
6. 索引成功后推进 `last_applied_revision`；
7. 最后使用 claim token ACK Outbox 事件；
8. 将旧事件识别为 superseded，并进行 ACK。

这些设计能够正确处理：

- 同一事件重复投递；
- 同一版本在“索引成功、ACK 前崩溃”后的重放；
- 一个 Worker 顺序处理、且不存在并发外部写入时的 happy path。

但它目前只是可调用的 Worker 类，没有：

- Worker CLI 或项目入口；
- 常驻 poll loop；
- API lifespan 后台任务；
- Docker Compose Worker service；
- Worker 配置投影；
- `ensure_collection()` 启动初始化；
- 健康检查和队列 lag 指标；
- 优雅停止；
- heartbeat、retry、dead-letter；
- 多 Worker 外部副作用 fencing。

所以运行 `agent-api` 后，上传文档仍不会自动进入索引。测试通过手动构造 Worker 并调用
`drain()` 才完成摄取。

### 3.2 `knowledge_search` Tool

实现位置：

```text
src/agent_workbench/adapters/tools/knowledge_search.py
```

设计中正确的部分：

- Tool 参数只有 `query`、`knowledge_base_id` 和 `top_k`；
- JSON Schema 使用 `additionalProperties: false`；
- principal 和 tenant 只来自 `invocation.context.principal`；
- Tool 被标记为只读、可并行和安全幂等；
- 与固定 2-step Chat 共用同一个 `RetrievalService`；
- 返回 Chunk ID，给后续引用验证保留了基础标识。

当前限制：

- 尚未从 `adapters/tools/__init__.py` 导出；
- 尚未注册到 API 的 `StaticToolRegistry`；
- Chat 的 `tool_names` 和授权 Envelope 仍为空；
- Chat 预算固定为 `max_steps=1`，首轮提出 Tool 后会在执行 Tool 前耗尽步骤；
- `model.main.tool_calling_required=true` 会在存在 Tool 时每轮强制调用 Tool，
  无法自然进入最终回答；
- Tool 输出没有接入 Artifact/摘要策略；
- Tool 丢弃 `AuthorizedContext.authorized_revisions`，无法在最终回答前重验 ACL；
- Tool 检索证据没有进入 Run evidence ledger、Citation 或 durable Context 事件。

因此该分支更准确的状态是：

> Tool Adapter 原型已实现，身份边界方向正确；尚未具备可合并的输出边界、安全提交语义
> 和主链装配。

## 4. 发现项

### 4.1 P0：ACL 二次复核仍可被 durable SSE 绕过

调用顺序如下：

1. Chat 调用 Runtime；
2. Runtime 完成模型调用；
3. Runtime 发出包含完整回答的 `ModelCompleted(text=turn.text)`；
4. `ModelCompleted` 被标记为 durable；
5. PostgreSQL EventLog 持久化完整 payload；
6. Runtime 返回 `AgentOutcome`；
7. Chat 才调用 `confirm_unchanged()`；
8. 如果 ACL 已改变，HTTP 回答被替换为拒绝文本；
9. 但是 SSE 可以重放已经持久化的原始 `ModelCompleted.text`。

关键代码：

```text
src/agent_workbench/runtime/agent_runtime.py:397
src/agent_workbench/domain/events.py:117
src/agent_workbench/domain/events.py:258
src/agent_workbench/application/chat.py:139
src/agent_workbench/application/chat.py:145
src/agent_workbench/apps/api/routes/events.py:132
```

触发窗口：

```text
检索完成
  → 模型生成回答
  → ModelCompleted 写入 EventLog
  → ACL 被撤销
  → Chat 二次检查发现变化
  → HTTP withheld
  → SSE 仍能读取原回答
```

影响：

- “回答提交前 ACL 二次复核”的安全保证不成立；
- HTTP 和 Conversation History 不泄漏，不代表 EventLog/SSE 不泄漏；
- 后续 Agentic Retrieval 复用同一 Runtime 时会继承相同问题。

修复要求：

- 在最终 ACL/证据复核前，不得把 answer-bearing payload 写入公开 durable log；
- 可采用 turn-local buffer 或 staging sink；
- 通过复核后再发布 `AnswerCommitted`；
- 复核失败时只发布不含原回答的 `AnswerWithheld`；
- 增加确定性撤权 failpoint；
- 同时断言 HTTP、Conversation、EventLog 和 SSE 均不包含秘密文本。

### 4.2 P1：旧版本 Qdrant Point 仍会进入回答

当前 Chunk ID 包含：

```text
index identity + document version + ordinal
```

所以新文档版本一定生成新的 Point ID。当前摄取只 upsert 新 Point，不删除旧版本 Point。

Retrieval 的 PostgreSQL 重验只判断：

```python
candidate.document_id in readable_documents
```

没有判断：

```python
candidate.source_revision == current_document.source_revision
```

也没有比较当前 document version。

关键代码：

```text
src/agent_workbench/application/ingestion.py:85
src/agent_workbench/application/ingestion.py:134
src/agent_workbench/workers/ingestion.py:153
src/agent_workbench/application/retrieval.py:137
src/agent_workbench/application/retrieval.py:146
src/agent_workbench/adapters/vector/qdrant.py:115
```

直接后果：

- 文档 v2 上线后，v1 内容仍可能被召回；
- 文档缩短后，已删除段落仍可能进入回答；
- 文档变成空文件时，`IngestionService` 不写任何 Point，但 Worker 仍推进
  `last_applied_revision`，旧内容可能永久残留；
- 更换 Chunker/Embedding/Sparse identity 后，旧索引与新索引可能混在同一 Collection；
- 二次复核使用的是 PostgreSQL 当前 revision，因此不能发现候选 Point 本身已经过期。

最低限度安全修复：

1. Retrieval 只接受候选 revision 等于 PostgreSQL 当前 revision 的 Point；
2. 新版本采用 replace 语义，清理该文档旧 Point；
3. 空文档和删除事件也必须执行清理；
4. 增加 v1→drain→v2→drain→查询 v1 专有词的测试；
5. 增加空版本和缩短版本的测试。

### 4.3 P1：Worker 的数据库锁不能 fence Qdrant 写入

Worker 在短事务中读取 snapshot，然后释放行锁；Artifact IO、Embedding 与 Qdrant upsert
发生在事务之外。

这能避免长期占用数据库行锁，但不能保证每个 Document 只有一个 Worker 正在写外部索引。

可出现：

```text
Worker A 读取 revision 1，释放锁后暂停
提交 revision 2
Worker B 读取并写入 revision 2
Worker B 将 last_applied_revision 更新为 2
Worker A 恢复并写入 revision 1
Worker A 的 PostgreSQL UPDATE 失败，但 Qdrant 副作用已经发生
```

关键代码：

```text
src/agent_workbench/workers/ingestion.py:81
src/agent_workbench/workers/ingestion.py:143
src/agent_workbench/workers/ingestion.py:167
```

`UPDATE ... WHERE last_applied_revision < revision` 只能防止 PostgreSQL 计数回退，
不能 fence 已经发生的 Qdrant 写入。

需要：

- 使用 document-key session advisory lock；
- advisory lock 必须由专用物理连接持有；
- 锁覆盖 snapshot、Artifact、Embedding、Qdrant 写和 revision 提交；
- 或者引入带 generation/fencing token 的索引写协议；
- 使用可控 barrier/failpoint 确定性复现双 Worker 交错；
- 测试必须断言旧 Worker 不能在新 Worker 后留下可检索的旧内容。

### 4.4 P1：Lease、Heartbeat 与配置没有进入 Worker

当前 Worker：

```text
drain limit = 32
处理方式 = 串行
Outbox lease = 60 秒默认值
heartbeat = 不存在
```

配置声明：

```text
claim_batch_size = 1
lease_duration_seconds = 90
heartbeat_interval_seconds = 20
max_attempts = 5
retry_base_seconds = 2
retry_max_seconds = 60
```

这些配置均未被 Worker 消费。

风险：

- 同批靠后的事件开始处理前 lease 就可能过期；
- 真 BGE 模型加载或大文档 Embedding 很容易超过 60 秒；
- lease 被新 Worker 回收后，旧 Worker 仍可继续写 Qdrant；
- claim token 只能拒绝旧 Worker ACK，不能阻止旧 Worker副作用；
- 当前没有 renew/heartbeat Port。

需要扩展 Outbox 协议：

```text
claim
renew / heartbeat
ack
nack / release
retry schedule
dead-letter
```

### 4.5 P1：删除事件会被 ACK，但不会删除向量

Worker 没有按 `event.kind` 分支。

文档不存在或 `deleted=true` 时，它直接返回 `skipped`；`drain()` 随后对 skipped
事件执行 ACK。

现成的 `VectorIndexPort.delete_document()` 没有被调用。

关键代码：

```text
src/agent_workbench/workers/ingestion.py:69
src/agent_workbench/workers/ingestion.py:99
src/agent_workbench/adapters/vector/qdrant.py:260
```

需要分别处理：

- `document_upserted`；
- `acl_changed`；
- `document_deleted`。

删除完成的验收标准应包括：

- PostgreSQL 不再允许读取；
- Qdrant 中该 Document 的 Point 被物理删除；
- 删除事件重复投递仍然幂等；
- 删除后旧 Worker 不能重新写回。

### 4.6 P1：不支持的文件会成为 Poison Event

Upload API 接受任意 `media_type`，但 Parser 目前只支持：

```text
text/plain
text/markdown
text/x-markdown
```

PDF、DOCX 或非法 UTF-8 可以完成 Upload 和 Document Version 提交，但 Worker
随后会在 Parser 阶段失败。

当前 Worker 没有：

- per-event 异常隔离；
- permanent/transient 分类；
- retry attempt；
- backoff；
- failed ingestion 状态；
- dead-letter；
- 继续处理同批后续事件的逻辑。

一次异常会终止整个 `drain()`，其余已经 claim 的事件只能等待 lease 过期。

需要：

1. Upload 阶段拒绝部署不支持的格式，或者完整实现 PDF Parser；
2. Worker 为每个事件单独捕获异常；
3. 永久格式错误进入 `failed`，不能无限重试；
4. 临时错误使用指数退避；
5. 后续事件不应被单个 Poison Event 阻塞。

### 4.7 P1：`knowledge_search` 输出超过领域上限

`knowledge_search._render()` 把所有 Chunk 全文写进一个 JSON 字符串。

`ToolResult.content` 使用 `BoundedText`，最大长度为 4096。

最小复现：

```text
3 个 Chunk
每个 Chunk 2000 字符
渲染结果 6189 字符
构造 ToolResult 时抛 ValidationError
```

默认 `top_k=8`，而常规 512-token Chunk 很容易超过该上限，所以该问题不是极端边界。

关键代码：

```text
src/agent_workbench/adapters/tools/knowledge_search.py:93
src/agent_workbench/adapters/tools/knowledge_search.py:109
src/agent_workbench/domain/tools.py:106
src/agent_workbench/domain/schema.py:38
```

修复建议：

- Tool 结果构造前执行统一 ResultBudget；
- 按字符/token/byte 三个维度裁剪；
- 优先减少 Chunk 数，而不是从 Chunk 中间盲目截断；
- 大结果写 Artifact，Inline 内容只返回摘要和 ArtifactRef；
- 返回结构中保留 Chunk ID、Document ID、revision 和 locator；
- 增加 1、8、20 个长 Chunk 的边界测试；
- 测试必须通过 `ToolGateway → ToolExecutor → Handler`，不能只直接调用 Handler。

### 4.8 P1：Agentic Retrieval 没有最终 ACL 复核

`RetrievalService.retrieve()` 返回：

```text
ContextPacket
authorized_revisions
```

`knowledge_search` 只把 `ContextPacket` 渲染成字符串，随后丢弃
`authorized_revisions`。

因此：

```text
Tool 检索时有权限
→ ToolResult 进入模型上下文
→ ACL 被撤销
→ 模型生成最终回答
→ 没有证据 revision 可以再次确认
```

固定 2-step Chat 至少存在 `confirm_unchanged()` 调用；Agentic Retrieval 目前没有同等机制。

关键代码：

```text
src/agent_workbench/adapters/tools/knowledge_search.py:81
src/agent_workbench/application/retrieval.py:150
src/agent_workbench/application/retrieval.py:162
```

接入前必须：

- 将检索证据和 revision 写入 run-scoped evidence ledger；
- 多次 `knowledge_search` 合并 evidence，而不是覆盖；
- 最终回答提交前统一重验所有 evidence；
- 任意来源变化时拒绝或基于当前可读来源重新生成；
- 与 4.1 的 answer staging/event staging 一起设计；
- Agentic 路径不得比固定 2-step 路径少一道安全检查。

### 4.9 P1：当前 Chat 配置无法运行 Tool Loop

API 当前：

```text
ToolRegistry = empty
tool_names = empty
AuthorizationEnvelope = deny-shaped empty allowlist
RunBudget.max_steps = 1
RunBudget.max_tool_calls = 1
```

即使只把 `knowledge_search` 注册进去：

1. 首次模型调用已经消耗 1 step；
2. 如果模型提出 Tool，Runtime 在派发前发现 step allowance 已耗尽；
3. Run 以 budget exceeded 结束；
4. Tool 不会执行。

即使将 `max_steps` 调高，默认配置仍有：

```text
model.main.tool_calling_required = true
```

DeepSeek Adapter 只要看到 Tool 就发送：

```text
tool_choice = required
```

这会要求最终回答轮继续调用 Tool，不能自然结束为文本回答。

需要单独设计 Agentic Mode：

- 明确区分 fixed 2-step 与 agentic；
- agentic 使用足够的 step/tool budget；
- Tool choice 应允许首轮/中间轮 `auto`，最终轮允许 `none` 或不强制；
- Envelope 只允许 `knowledge_search`；
- 输出仍必须经过最终 evidence/ACL commit gate。

### 4.10 P1：Chat 产品语义仍不完整

上一轮发现的下列问题没有对应代码修改，因此仍然存在：

#### Failed/Cancelled 被包装成成功

`ChatService` 不检查 `AgentOutcome.status`，始终：

- 读取 `output_text or ""`；
- 写入 Assistant History；
- 返回 HTTP 200。

Provider error、deadline、budget exceeded 或 cancellation 会成为空成功回答。

#### Chat 不是多轮

Conversation History 会持久化，也可以查询，但下一轮模型请求只包含：

```text
当前问题 + 当前检索证据
```

历史对话从未进入模型上下文。

#### Citation 不是答案实际引用

当前为每个检索候选预先生成 Citation，最终原样返回全部 Citation。没有：

- 模型输出引用解析；
- 引用 ID 验证；
- 未使用证据排除；
- Citation precision/recall；
- 空检索代码级拒答。

#### Turn 无幂等和原子性

用户消息与 Assistant 消息分别提交。两个并发请求可能形成：

```text
user-A
user-B
assistant-B
assistant-A
```

客户端重试还会重复检索、重复调用模型和重复写消息。

#### Run/Stream Identity 不一致

Route、EventScope 和 `AgentRunRequest` 分别生成 ID，导致：

- `EventEnvelope.run_id` 与 `AgentOutcome.agent_run_id` 不一致；
- request stream ID 与实际 SSE session stream 不一致；
- API 不返回可供客户端关联的 run ID。

### 4.11 P1：Runtime/DeepSeek 边界问题仍存在

本轮没有修改相关文件，因此上一轮已复现的问题仍然存在：

1. DeepSeek 对结构错误的 Tool Fragment 进行跳过，前后 Fragment
   可能拼成模型从未真正发送的合法 ToolCall；
2. DeepSeek 成功流缺失 usage 时按 0 token 完成，可绕过
   `max_total_tokens`；
3. 多个 Hook 或 Policy rewrite 重复使用同一份 remaining-time 快照，
   累计时间可越过 run deadline；
4. model timeout 后，`aclose()` 自身没有 timeout，可无限阻塞；
5. 大量重复 `tool_call_id` 可把 `ErrorInfo.message` 撑过上限并抛
   ValidationError；
6. Runtime/Policy 配置没有完整投影到 API 进程装配。

这些问题需要独立 Runtime Hardening PR，不应和 Agentic Retrieval
功能 PR 混在一起修复。

### 4.12 P1/P2：Upload 与持久化边界仍需完善

上一轮发现的下列问题也没有对应修改：

- 同一 Upload 可以重复 PUT 并生成孤儿 Artifact；
- Transfer 上限使用全局 Artifact 上限，而不是
  `min(declared_size, max_artifact_bytes)`；
- completed Upload 仍可继续 PUT；
- Document ID 是客户端提交的全局主键，不是 tenant-scoped 复合身份；
- Local Artifact metadata 不是原子 rename；
- Artifact ownership 只在本地 sidecar 中，数据库 artifacts 表没有成为事实源；
- Worker 使用 `get()` 整体读取最高约 100 MiB 文件，并进行同步文件 IO，
  会阻塞事件循环和未来 heartbeat。

## 5. 当前真实能力矩阵

| 能力 | Planned | Implemented | Tested | Demonstrated | 说明 |
|---|:---:|:---:|:---:|:---:|---|
| Domain / Ports / Schema | ✓ | ✓ | ✓ | ✓ | 基础较稳 |
| 自研 Agent Runtime | ✓ | ✓ | ✓ | ✓ | 边界问题仍需 Hardening |
| Tool Gateway / Policy / Hook | ✓ | ✓ | ✓ | ✓ | 累计 deadline 等边界待修 |
| DeepSeek Adapter | ✓ | ✓ | ✓ |  | 离线协议测试为主 |
| PostgreSQL ConversationStore | ✓ | ✓ | ✓ |  | 尚未形成多轮 Chat |
| Document / ACL / Outbox | ✓ | ✓ | ✓ |  | 基础事务语义已实现 |
| Outbox claim/lease/token ACK | ✓ | ✓ | ✓ |  | 无 heartbeat/retry/DLQ |
| IngestionWorker 类 | ✓ | ✓ | ✓ |  | 主分支已实现 |
| 常驻 Ingestion Worker 进程 | ✓ |  |  |  | 无 CLI/装配/健康检查 |
| Upload→Worker→Index→Chat E2E | ✓ |  |  |  | 尚未贯通 |
| Text/Markdown Parser | ✓ | ✓ | ✓ |  | PDF/DOCX 未实现 |
| BGE-M3 Dense Adapter | ✓ | ✓ | 部分 |  | 真模型不在 CI |
| BGE-M3 Sparse Adapter | ✓ | ✓ | 不稳定 |  | 权重加载仍未定论 |
| Qdrant Dense Search | ✓ | ✓ | ✓ |  | stateful 测试存在 |
| Qdrant Dense+Sparse RRF | ✓ | ✓ | ✓ | 实验性 | 未装配进 API |
| 旧版本替换/索引 Generation | ✓ |  |  |  | 旧 Point 会残留 |
| Reranker | ✓ |  |  |  | 只有配置 |
| 固定 2-step Chat | ✓ | ✓ | ✓ |  | 非真正多轮 |
| Chat HTTP API | ✓ | ✓ | ✓ |  | failed/cancelled 语义待修 |
| Durable EventLog / SSE replay | ✓ | ✓ | ✓ |  | 存在 ACL P0 |
| Live token delta SSE | ✓ |  |  |  | 当前只轮询 durable log |
| `knowledge_search` Tool | ✓ | 分支实现 | 部分 |  | 尚未合入、尚未装配 |
| Agentic Retrieval Mode | ✓ |  |  |  | 无最终 evidence/ACL gate |
| 可验证 Citation | ✓ | 部分 | 部分 |  | 当前是检索候选列表 |
| LlamaIndex Adapter | ✓ |  |  |  | 只有配置 |
| LangChain Adapter | ✓ |  |  |  | 只有配置 |
| LangGraph Task | ✓ |  |  |  | 未实现 |
| Task Registry / Checkpoint | ✓ |  |  |  | 未实现 |
| Multi-Agent | ✓ |  |  |  | 未实现 |
| CrewAI Benchmark | 可选 |  |  |  | 未实现 |
| RAGAS | ✓ |  |  |  | 只有配置 |
| OpenTelemetry | ✓ |  |  |  | 只有配置 |
| 生产身份认证 | ✓ |  |  |  | 当前强制 local/loopback |
| UI / Docker Compose | ✓ |  |  |  | 未实现 |

## 6. 质量门

本轮执行结果：

```text
ruff format --check .    通过，175 files already formatted
ruff check .             通过
pyright                  通过，0 errors / 0 warnings
git diff --check         通过
alembic heads            0005_last_applied_revision
```

全量测试：

```text
697 passed
187 skipped
1 failed
```

唯一失败：

```text
tests/api/test_bind_address.py::test_the_default_host_binds_a_socket_that_is_loopback
PermissionError: sandbox 禁止 socket.bind()
```

该失败属于执行环境限制，不是业务断言失败；对应测试代码本轮没有变化。

重要限制：

- 新增 6 条 Worker 测试在本机因没有 PostgreSQL/Qdrant 测试环境而跳过；
- 真 BGE-M3 与 Sparse 权重测试默认跳过；
- 本轮无法连接 GitHub API，因此未独立获取当前分支的远端 CI 状态；
- CI 没有安装 embedding extra；
- 当前没有 coverage/branch coverage 阈值；
- 当前没有依赖漏洞/SBOM Gate。

因此，不能只根据 `ruff + pyright + deterministic pytest` 宣称 Worker 并发、
真实模型或产品 E2E 已完成。

## 7. 配置与实现漂移

当前配置将以下能力声明为 enabled：

```text
langchain_adapter.enabled = true
workflow.control_plane = "langgraph"
multi_agent.enabled = true
rag.llama_index.enabled = true
rag.embedding.sparse_enabled = true
rag.reranker.enabled = true
observability.otel_enabled = true
evaluation.ragas_enabled = true
```

但当前运行代码中：

- 没有 LlamaIndex 依赖或 Adapter；
- 没有 LangChain Adapter；
- 没有 LangGraph Workflow；
- 没有 Multi-Agent；
- API 没有装配 Sparse；
- 没有 Reranker；
- 没有 OTel；
- 没有 RAGAS。

建议二选一：

1. 未实现前将这些配置标记为 `false`；
2. `agent-config-check` 在 enabled Adapter 缺少实现或依赖时直接拒绝启动。

配置文件不能把“架构目标”伪装成“运行时已启用能力”。

## 8. 文档漂移

以下文件顶部仍停留在旧基线：

```text
README.md
README.en.md
docs/README.md
docs/architecture-baseline.md
docs/implementation-plan.md
docs/status.md
```

常见旧描述：

```text
main@f071323
PR-001～PR-015
Chat RAG 未实现
DeepSeek 尚未装配
```

实际已经推进到：

```text
main@4d03f69
IngestionWorker 已进入主分支
Chat API / EventLog / SSE / Dense RAG 已有代码
knowledge_search 位于未合并分支
```

建议：

- 保留 `docs/repository-audit-2026-07-25.md` 为历史记录；
- 使用本报告作为 2026-07-27 新快照；
- 修完安全 P0 和索引一致性后，再统一更新 README 和能力矩阵；
- `docs/status.md` 的历史 PR 段落可以保留，但顶部摘要必须更新；
- PR-033 应描述为“Worker 原型/组件闭环”，不能描述为“生产摄取闭环”；
- PR-034 在合并前必须补输出边界和最终 ACL evidence gate。

## 9. 建议增量实施顺序

### PR-035：回答发布安全门

目标：

- 修复 SSE/ACL P0；
- 将答案生成与答案公开分离。

工作项：

1. 增加 turn-local/staging EventSink；
2. `ModelCompleted` 在 Chat/RAG 场景中不直接公开完整 text；
3. ACL/evidence 检查通过后发布 `AnswerCommitted`；
4. 失败时发布 `AnswerWithheld`；
5. 添加撤权 failpoint；
6. 检查 HTTP、Conversation、EventLog、SSE 四个出口。

验收：

```text
模型生成秘密文本
→ failpoint 撤权
→ HTTP withheld
→ History 不含秘密
→ EventLog 不含秘密
→ SSE 不含秘密
```

### PR-036：索引 Revision 安全栅栏与 Replace 语义

目标：

- 不让旧 Point 进入 Context；
- 更新、缩短、清空和删除后都不保留可检索旧内容。

工作项：

1. Retrieval 比较 candidate revision；
2. Qdrant 增加 replace-current-document 操作；
3. 空文档也执行旧 Point 清理；
4. 删除事件调用 `delete_document()`；
5. 校验 index identity/generation；
6. 增加 v1→v2、缩短、空、删除测试。

### PR-037：可靠 Ingestion Worker

目标：

- 多 Worker 下仍保持 per-document 单写；
- 失败可恢复，不阻塞整批。

工作项：

1. document-key advisory lock；
2. 专用物理连接；
3. lease renew/heartbeat；
4. 默认 claim batch 读取配置；
5. per-event error isolation；
6. attempt/backoff/dead-letter；
7. `document_upserted`、`acl_changed`、`document_deleted` 分支；
8. 可控 barrier/failpoint；
9. 旧 Worker 晚写测试。

### PR-038：Worker 生产进程与产品 E2E

目标：

- 让上传的文档在真实运行环境中自动变为可检索。

工作项：

1. `agent-ingestion-worker` 入口；
2. Worker 配置投影；
3. Qdrant `ensure_collection()`；
4. poll/jitter；
5. SIGTERM 优雅关闭；
6. readiness、queue lag 和 failure 指标；
7. Docker Compose Worker service；
8. HTTP Upload → Worker → Retrieval → Chat E2E。

### PR-039：修正并合并 `knowledge_search`

合并当前 PR-034 前必须：

1. ResultBudget；
2. Artifact/摘要策略；
3. source revision evidence ledger；
4. 最终 ACL/evidence commit gate；
5. Gateway 级测试；
6. Prompt injection 边界；
7. Agentic Mode 独立预算；
8. `tool_choice` 允许最终回答；
9. 注册表与 Envelope 装配。

### PR-040：Chat Turn 协议

目标：

- 解决失败状态、重试、并发和身份关联。

建议增加 `chat_turns`：

```text
turn_id
session_id
idempotency_key
run_id
status
user_message_sequence
assistant_message_sequence
error_code
created_at
completed_at
```

要求：

- 同一 Idempotency Key 不重复调用模型；
- 同一 Session 的 Turn 有明确顺序；
- failed/cancelled 不伪装成 200 空答案；
- Route、EventScope、AgentRunRequest、AgentOutcome 共用一个 run ID；
- API 返回 turn/run ID。

### PR-041：真正多轮与可验证 Citation

目标：

- 从“保存历史的单轮 RAG”升级为真正 Chat；
- Citation 表示模型实际引用。

工作项：

1. 历史裁剪/摘要策略；
2. 当前问题与多轮历史共同进入模型请求；
3. 模型结构化返回 cited chunk IDs；
4. 服务端验证 cited IDs 必须来自本次 evidence；
5. Citation 使用真实 page/paragraph/char locator；
6. 增加 citation precision/recall 和拒答测试。

### PR-042：Runtime 与 Upload Hardening

Runtime：

- Tool Fragment 结构错误整体 fail closed；
- usage 缺失时不能把硬 token budget 当成 0；
- Hook/Policy 使用绝对 deadline；
- `aclose()` 有独立 cleanup timeout；
- 重复 ID 错误消息裁剪；
- Runtime/Policy Settings 完整投影。

Upload：

- 一个 Upload 只允许一次有效 Transfer；
- completed 后拒绝 PUT；
- 上传上限使用 declared size；
- Artifact 与 Upload Intent 绑定；
- 孤儿清理；
- tenant-scoped Document identity；
- metadata 原子写；
- 数据库成为 Artifact ownership 事实源。

## 10. 框架扩展顺序

安全与一致性完成后，再按下列顺序增加就业项目的技术覆盖：

### 10.1 LlamaIndex

只放在 ingestion/retrieval Adapter：

- Reader/Connector；
- Node parsing；
- Ingestion pipeline；
- Retriever 映射。

不使用 LlamaIndex Agent/QueryEngine 生成最终回答，不接管自研 Runtime。

### 10.2 LangChain

只做最薄互操作：

- Model Adapter；
- Tool Adapter；
- Message round-trip contract。

不使用 LangChain AgentExecutor 接管主循环。

### 10.3 LangGraph

用于 Task Mode：

- 固定 Workflow；
- conditional edge；
- checkpoint；
- interrupt/HITL；
- resume；
- supervisor/worker nodes。

Agent Node 内部调用自研 Runtime，LangGraph 不接管同一次 Tool Loop。

### 10.4 CrewAI

只做可切换 Benchmark Adapter：

- 与自研 supervisor/workers 跑同一任务集；
- 对比成功率、耗时、Token、恢复能力；
- 不进入默认生产链。

## 11. 简历描述边界

### 当前可以写

> 设计并实现框架无关的 Python Agent Runtime，包含 Provider-neutral ModelPort、
> Tool Gateway、Policy/Hook、预算、取消和事件协议；基于 FastAPI、PostgreSQL 与
> Qdrant 实现带对象级 ACL 重验的 Dense RAG/Chat Alpha，并实现 Outbox 摄取 Worker
> 原型、Durable EventLog、SSE Replay 与可评测检索基线。

### 当前不应写

- 已完成生产级 Chat + Task 双模式平台；
- 已完成 LlamaIndex RAG；
- 已完成 LangGraph Task；
- 已完成 Multi-Agent；
- 已完成 CrewAI 对比；
- 已完成 Hybrid RAG；
- 已完成可验证 Citation；
- 已完成生产级身份认证；
- 已完成可恢复 Worker 闭环；
- 已完成真实多轮 Chat。

### 完成 PR-035～PR-041 后可以升级为

> 完成安全可提交的 Chat/RAG 纵向切片：文档上传经可靠 Outbox Worker 自动进入
> Qdrant，检索按 PostgreSQL 当前 revision 与 ACL 双重重验，支持真正多轮会话、
> 可验证 Citation、幂等 Chat Turn、Durable Event/SSE Replay 与故障恢复测试。

## 12. 最终判断

仓库的代码量和组件数量已经明显增长，但“组件存在”不等于“产品链路成立”。

当前最有价值的下一步不是继续增加框架名字，而是完成三条可证明的不变量：

1. **未通过最终授权检查的答案，不能从任何出口公开；**
2. **数据库当前版本之外的索引内容，不能进入模型上下文；**
3. **失去 lease/fence 的旧 Worker，不能留下外部副作用。**

完成这三条以后，再接通 Worker 进程和产品 E2E，项目就能从“优秀的组件化原型”
进入“可在简历和面试中经得住追问的 Agent 系统基线”。
