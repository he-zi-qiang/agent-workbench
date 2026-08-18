# ADR-067：一条引用的原文是一次新的读取

- 决策点：Chat 的引用今天只是一个不可点的 id。读者要核对一句话的依据，唯一的路是
  打开知识库自己搜。给它一条读取路：这条路按什么寻址、鉴权重放还是重做、被引用的
  段落算「结构化事实」还是「运行时正文」
- 状态：**接受**。新增 `GET /v1/chat/sessions/{id}/turns/{turn_id}/citations/{chunk_id}`
  ——本次唯一的新 HTTP 面。**鉴权全部重做，一条昨天的引用今天可以正确地 404。**
  `ChatTurnStore` 新增本协议上第一个读方法 `turn(...)`。Chat **不**获得产物容器、
  不获得工作区、不获得下载
- 日期：2026-08-18
- 影响：`ports/conversation_store.py`（`ChatTurnStore.turn`）、
  `adapters/memory/conversation_store.py` 与 `adapters/persistence/conversation_store.py`
  各一个实现（PostgreSQL 那份用 `connect()` 而不是 `begin()`）、
  新 `application/citation_source.py`（`CitationSourceReader` / `CitedPassage` /
  两个错误类型）、`apps/api/routes/chat.py`（新路由 + `CitedPassageView`）、
  `apps/api/main.py`（状态码表 +1）、`apps/api/dependencies.py`（`citation_source`）、
  前端 `api/{client,types}.ts`、`features/chat/ChatPage.tsx`（`CitationChip`）
- 依赖：[ADR-018](./0018-ungrounded-chat-shape.md)（有据回答与引用的语义）、
  [ADR-037](./0037-the-graph-nominates-chunks.md)（`VectorIndexPort.fetch` 为什么存在）、
  [ADR-063](./0063-a-produced-name-is-a-fact-not-a-sentence.md)（本 ADR §4 借用它
  「门管的是内容」那条判据，并得出相反的结论）、
  [ADR-066](./0066-showing-is-not-checking.md)（它把 Chat 的这一半划出了范围，
  并记下这是单位收益最高的一项）

## 1. 背景：引用存在的意义是让核对变便宜，而它没有

`Citation`（`domain/context.py`）带四个字段：`chunk_id`、`document_id`、
`document_version`、`locator`。还有第五个 `quote`，**全仓生产代码里从未被赋值**
——唯一的构造点 `application/retrieval.py` 只写前四个。

于是控制台能显示一个答案引了哪些 chunk，除此之外一个字都拿不到。读者想核对一句话，
唯一的路是打开知识库页自己搜。**引用的全部意义是让核对变便宜，而这套引用让核对
和没有引用一样贵。** ADR-066 把这一条排在「单位收益最高」，并明确留给这份 ADR。

`quote` 那个字段还有一层代价：它让「读者能看到原文」看起来像已经做过了。ADR-066
随附的改动已经把界面上那个永远渲染空字符串的分支删掉了；本 ADR 补上它当初想做的事。

## 2. 决定：路由挂在轮次下面，因为数据就是这个形状

```
GET /v1/chat/sessions/{session_id}/turns/{turn_id}/citations/{chunk_id}
```

**不是 `GET /v1/chunks/{chunk_id}`**，而拒绝的理由不是整洁，是那个形状按它自己的
入参调不动它自己引的那个端口：

- `VectorIndexPort.fetch` 的必填参数里有 `knowledge_base_id`
  （`ports/vector_index.py`），它的 docstring 明写这个收窄「必须不是那一个悄悄
  停止收窄的读」；
- 一个裸 `chunk_id` 换不出 `knowledge_base_id`：它活在 `AskRequest` 那一次请求里，
  `ConversationSession` 上没有，`chat_turns` 表也没有这一列；
- 也换不出 `document_id`：PostgreSQL 里根本没有 chunks 表。

轮次能给出 `document_id`，文档能给出知识库。**形状跟着数据走。**

### 四步，顺序是全部安全论证

```
1. 读这一轮（ChatTurnStore.turn）——它引过这个 chunk 吗
2. 读文档的可读性（documents.readable_versions）——现在还能读吗，属于哪个知识库
3. 读索引（VectorIndexPort.fetch）——按 tenant / kb / principal 收窄
4. 比对 revision——相等才给
```

**第一步必须在最前面，而且它回答的是一个不能从请求里拿的问题**：这个答案到底引没引
过这个 chunk。把索引读放前面、事后再检查，等于让这个端点变成一个「给我任意 chunk
id」的读，而 turn id 只是挂在上面的装饰。测试钉住了这一点：一个这一轮没引过的
chunk 会在**第一步就停下**，文档和索引一次都没被碰。

### 鉴权重做，不重放

**一条昨天的引用今天可以正确地 404，这是要保住的行为，不是要修的缺陷。**

存下来的引用记录的是「当时答了什么」，不是「此刻还能读什么」。第 2 步走的是
`retrieval.py` 走的同一条 `readable_versions`，理由也是它写下的那条：索引里带着
一份 ACL 副本，而那份副本只和最后一次重新索引一样新，**一个还没传播到索引的撤权
正是最值得抓住的情形**。第 4 步的 revision 相等同理，且必须是相等而不是 `>=`
——派生存储跑到权威前面也不是一个可以供数的状态。

反过来说：若这里重放存下来的结论，每一个发布过的答案都会变成一条**永久的读取通道**，
比授予它的那次授权活得更久。那是这份 ADR 唯一不能接受的结果。

### 两种拒绝，都是 404，各说各的话

| 情形 | 答 | 说什么 |
| --- | --- | --- |
| 这一轮没引过这个 chunk / 轮次属于别人 / 会话属于别人 / 都不存在 | 404 | 「没有这条引用」——一句话盖住四种，任何区分都会确认别人的东西存在 |
| 权限被收回 / revision 已经变了 / 点不在索引里 | 404 | 「这段现在读不到了」 |
| 这套部署没有向量索引 | **503** | 引用可能完全是真的，缺的是这个进程读它的能力 |

**区分第二组不是泄漏**：调用者手里本来就拿着这条引用，在他自己拥有的一轮里，所以
这句话没有告诉他任何答案没告诉过他的东西。这与 ADR-063 §2「发出这次调用的
principal 本来就能列出整个工作区」是同一条判据。

503 那一行与 `CodeRunUnavailableError` 在状态码表里紧挨着，理由一样：404 会把读者
支去自己的数据里找一个不存在的错。

## 3. `ChatTurnStore` 上的第一个读方法

这个协议此前只有写和为协调器做的扫描——`claim_turn` / `prepare_release` /
`mark_released` / `finish_failed` / `finish_running_if_current` /
`list_release_pending`。**一轮自己的记录只有正在执行它的那个协程够得着**，这正是
控制台能显示引了哪些 chunk 却拿不到别的的底层原因。

`turn(...)` 返回它当时的状态，原样。不按 `status` 过滤：一个 running 的轮次没有
result，一个 withheld 的 result 是被擦过的，两者都以「没有引用可匹配」落进同一个
404——而给它们各自一句话，等于告诉探测者哪些轮次处于哪个状态。

**PostgreSQL 那份用 `connect()` 而不是 `begin()`，两处读都用非加锁的 helper。**
这个类上其他每个方法都拿 `FOR UPDATE`，因为它们接下来要写；这一个从不写，而锁住
会话行会让一个打开引用的读者与那个会话里**正在执行的轮次**串行——那恰好是最可能
有人在读它的时刻。

## 4. 被引用的段落是结构化事实，还是运行时正文？

按 ADR-063 的格式回答，因为这个问题的形状和它一样，而答案的走向不同。

**它受 ACL 门控，不受 `runtime.record_step_inputs` 门控。** 那个开关治理的是
「这套部署愿不愿意把**运行时正文**抄进事件日志」——参数体、提示词、工具回答的那段
文本，共同点是它们**复制了内容**。本 ADR 没有把任何东西写进事件日志：它是一次读，
读的是语料库自己那份唯一的副本，读完就发给一个刚刚被验证有权读它的 principal。

一句话：**那道门管的是「要不要留副本」，这里管的是「这个人能不能读」，是两件事。**

顺带划清与 ADR-054 的界线：那份 ADR 为 `approval_preview` 开的是**无条件复制正文
入库**的例外，很贵。本 ADR 不需要那张票，因为它不入库。

## 5. Chat 不因此获得产物容器

这一条要写下来，因为它是最容易顺手做的下一步。

**被引用的段落是证据，不是产物。** 产物是这一轮产生、归属于这一轮的输出；被引用的
chunk 在这一轮之前就存在，之后也不属于这一轮。所以 Chat 得到的是一条按 id 取证据的
读取路，而不是：

- **artifact 容器**——会把 Chat 推成第三套产物机制（存储、寻址、生命周期、GC），
  而它真正缺的东西比那小得多；
- **下载按钮**——会破坏「每个界面恰好一个带标签的下载控件」这个模型，
  `WorkPage.test.tsx` 与 `BlobPreview.test.tsx` 两处钉住了它；
- **导出成文件**——与轮次级附件（known-gaps D-01）是同一个决定的两面。

ADR-066 随附的「复制答案与引用」已经拿到了读者要的那件事，且不产生需要归属的字节。

## 6. 被拒绝的方案

| 方案 | 为什么否 |
| --- | --- |
| `GET /v1/chunks/{chunk_id}` | 按它的入参调不动 `VectorIndexPort.fetch`（§2） |
| 重放轮次里存下来的授权结论 | 每个发布过的答案都变成一条比授权活得久的永久读取通道（§2） |
| 把段落正文写进 `Citation.quote`，随答案一起发布 | 那是**复制正文入库**，要 ADR-054 那张贵票；而且它会让每一条引用的正文永久留在 `chat_turns.result` 的 JSONB 里，权限收回之后仍在 |
| 抬高 `BoundedText` 的 4096 上限，让证据装进 `prompt_preview` | ADR-035 与 ADR-063 的论证前提都建立在这个数上；更根本的是那条路在 `record_step_inputs` 门后、出厂默认关，把「看证据」这个产品能力挂在一个可观测开关上是错的 |
| 把 `char_start/char_end` 一路带下来，在原始 PDF 里高亮 | `ports/vector_index.py` 的字段注释直接否掉：偏移索引的是抽出来的文本，不是存下来的文件 |
| 给 `ChatTurnStore.turn` 加 `status` 过滤 | 给 running 与 withheld 各一句话，等于告诉探测者哪些轮次处于哪个状态 |
| PostgreSQL 那份读也用 `begin()` + `FOR UPDATE` | 会让打开引用的读者与该会话里正在执行的轮次串行，而那正是最可能有人在读的时刻（§3） |
| 让 `GET /messages` 一并回引用标记 | **本次不做，但它是对的，见 §7。** 它改变了一次历史读取披露的内容，是第二个决定 |

## 7. 代价与未做的

1. **历史里的引用仍然没有标记。** `GET /v1/chat/sessions/{id}/messages` 返回
   `StoredMessage`（role + text），而引用躺在 `chat_turns.result` 这个 JSONB 列里。
   所以刷新页面之后，引用 chip 连同它的展开能力一起消失，界面上那句「历史记录只保存
   对话文本，不含引用与证据标记」仍然诚实。**做它要一次真实的面积**：`ChatTurnStore`
   还需要一个按会话列轮次的读、两个适配器、两套契约测试；而且它改变一次历史读取
   披露的内容，值得自己的一段论证。**新缺口 F-16。**
2. **没有对着真 Qdrant 的端到端证据。** 应用层四步全部由假 port 覆盖，契约层的
   `turn()` 对着真 PostgreSQL 跑过，而「读回来的确实是那一段」需要一个装着数据的
   索引。`tests/vector` 那套在本机跑得起来，本批没有为这条路径加用例。
3. **每条引用是一次请求。** 一个答案引十条、读者全点开就是十次读。没有批量端点：
   一次只读一条是这个交互的实际形状（读者点开一条、读完、可能再点一条），而批量
   接口会把「只读你点开的那条」这条性质变成「一次把十条都读出来」。
4. **展开的段落缓存到永远**（`staleTime: Infinity`）。回来的段落是「让它通过的那次
   检查之时」的段落；重新轮询只会花掉读取额度去最终推翻读者正看着的东西，而两种
   结果他都无从行动。
5. **一个没有绑定 turn id 的轮次，chip 是不可点的。** 刷新之后的历史轮次、以及
   claim 尚未返回就失败的轮次，都是这种。此时按钮 `disabled`，因为提供一次必然
   404 的点击，而 404 的原因与读者的权限毫无关系，是一种更坏的误导。

## 8. 证据

- `tests/contracts/test_chat_turn_store.py`：`turn()` 对**两个实现**跑同一套
  ——按 id 读回原样；错租户 / 错 principal / 错会话 / 不存在的轮次四种一律
  `NotFoundError`。真 PostgreSQL 本地实跑 84 passed / 1 skipped
- `tests/application/test_citation_source.py`：九条，覆盖四步的顺序与三种不可用
  ——**这一轮没引过的 chunk 在第一步就停下**（文档与索引 `seen == []`）；
  权限收回、revision 前进、revision 倒退、点不在索引里；索引读的四个收窄参数逐字钉住
- `tests/api/test_chat_routes.py`：没有索引的部署答 **503** 而不是 404
- `web/src/features/chat/ChatPage.test.tsx`：不点不取；点开显示原文且按
  `(sessionId, turnId, chunkId)` 三段寻址；读取失败说的是「读不到这段原文了」
  而不是「引用坏了」
