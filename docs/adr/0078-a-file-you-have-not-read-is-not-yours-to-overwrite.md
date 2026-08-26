# ADR-078：没读过的文件，不归你覆盖

- 决策点：`project_write` 整文件替换用户机器上的真实文件（ADR-072、ADR-074），
  而在此之前**没有任何东西**问过模型是否见过它要替换的那份。编码提示词的第一条
  纪律一直在要求这件事（"Read before you write"），但 `code_prompt.py` 自己写着
  规矩：没有别处执行的散文不该写进提示词。要不要把这条纪律变成前置条件；如果要，
  放在工具层还是 store 层，以及一次"看过了"到底该有多长的保质期
- 状态：**接受**。新增每回合的读取回执台账
  （`application/file_read_receipts.py`），`project_write` 在**替换已存在的文件**
  时要求本回合读过全文；`ProjectFileStore.write` 新增 `if_unchanged` 前置条件，
  把工具层判断与字节落盘之间的窗口关掉。**明确不做**：不做锁，不做版本寻址
  （F-13 保持拒绝），不管扁平工作区，不拦新建文件
- 日期：2026-08-25
- 影响：新增 `application/file_read_receipts.py`；`domain/project_files.py` 新增
  `ProjectFileChangedError`；`ports/project_files.py` 新增 `ProjectFileVersion`
  并给 `write` 加 `if_unchanged` 关键字参数；`adapters/filesystem/project_files.py`
  实现该前置条件；`adapters/tools/project_files.py` 的
  `ProjectReadTool`/`ProjectWriteTool`/`ProjectEditTool`/`ProjectRunTool` 各新增
  一个 `receipts` 字段；`adapters/tools/reading.py::windowed_result` 新增
  `note_read` 回调；`application/code_session.py` 新增 `read_receipts` 字段、
  `__post_init__` 与每回合进入台账；`apps/api/dependencies.py` 装配一个台账。
  **不动配置契约**：`config_schema_version` 保持 `1.18`

---

## 1. 背景：一次安静的损失

这条路径今天是这样的：

1. 用户在自己的编辑器里改了 `src/main.py`，保存。
2. 模型三次工具调用之前读过这个文件，或者根本没读过。
3. 模型调用 `project_write`，整文件替换。
4. 工具返回 `Wrote src/main.py (812 bytes).`，步骤行渲染成「写入项目文件」。
5. 回合正常结束，报告写得很漂亮。

第 5 步之后，转录里**没有任何一处**与"这次覆盖掉了用户刚写的东西"的转录不同。
用户下次打开那个文件才会知道。

这不是理论。它有三个互相独立的成因，每一个都足够：

- **模型从未读过它。** 没有任何检查。`project_write` 的 schema 只要 `path` 与
  `content`。
- **模型读过，但读的是窗口。** ADR-0077 那批加了 `offset`/`limit` 之后，一次成功
  的读可能只交出了文件的一部分；把它记成"读过"就是给一次覆盖未读字节的写开绿灯。
- **模型读过全文，但那之后文件动了。** 用户的编辑器、`git checkout`、模型自己用
  `project_run` 起的 `black .`。

第三条还有个更细的版本：即使工具层检查了，**检查和写入之间也有窗口**。
`store.read` 与 `store.write` 是两次独立的 `await`，用户的编辑器保存正好落在
那种窗口里——这是 `project_edit` 的问题，它本来看起来是"安全的那个工具"。

## 2. 决策：回执在工具层，前置条件在 store 层

两层，因为要挡的是两件事。

### 2.1 工具层：本回合读过全文，才允许整文件替换

`application/file_read_receipts.py` 是一个 ContextVar 包着的、**每回合一份**的
台账：`path -> (size_bytes, modified_at, covers_whole_file)`。

- `ProjectReadTool` 在把内容交给模型之后记一笔。`covers_whole_file` 由
  `windowed_result` 回传——那正是它用来决定要不要打印窗口抬头的同一个判断，问一次
  用两处，而不是让调用方再切一次窗口然后指望两次结果一致。
- **非文本文件不记回执**。一次 `project_read` 命中 PNG 会成功返回
  "Its contents are not shown."，模型看到的是大小和判定，不是文件。
- **空文件记全量回执**。零字节文件里没有模型没看过的东西，此时拦下它就是在唯一
  一个模型证明是最新的场景上开闸。
- `ProjectWriteTool` **只在路径已存在时**查台账。新建文件不销毁任何东西，要求先
  读一个不存在的路径是在用一句无法执行的话（"先读它"）拒绝编码智能体最常做的事。

**读取只增不减。** 一次更窄的读**不会**降低更宽的那次挣到的覆盖范围——前提是文件没
动过（尺寸与 mtime 都对得上）。重读一段准备改写的区域是编码智能体最常做的检查，让它
把回执降级，就是拒绝一个比一次调用之前**信息更全**的模型，还给它一句关于"你看过什么"
的错话。文件动过时不沿用，那时更早的那次读描述的是一份已经不在的文件。

拒绝分五种句子，因为下一步不同：

| 情形 | 句子 | 模型该做什么 |
|---|---|---|
| 没有回执 | `... you have not read it in this turn ... Read it first, then write.` | 读，再写 |
| 回执是窗口 | `... Read it again with no offset and no limit ...; if it is too long ..., use project_edit` | 从头整读，或换工具 |
| 检查之后才出现的文件 | `already exists. It was not there when this call started ... Read it before writing it.` | 读，再写 |
| 文件动过、本回合跑过命令 | `A command you ran in this turn may have done it. Read it again ...` | 重读 |
| 文件动过、本回合没跑命令 | `Something outside this turn changed it, so read it again.` | 重读，并且**在报告里说** |

第二句的第一版写的是「Read the rest —— 那次读已经告诉你从哪个 offset continue」，
而那是**唯一一个永远不成立**的动作：覆盖范围来自"从第 1 行开始并读到结尾"的窗口，
所以顺着 offset 读下去只是把头部回执换成尾部回执，同一句拒绝再来一遍。实测：一个
84,090 字符的文件分两次读完每一行之后，写入仍然被拒——**拒得对**，因为一次读永远拿不
到超过 `MAX_INLINE_READ_CHARS` 的全文；错的是那句话没有指向真正可行的两条路。

由此得出一条**没写在别处的功能限制**：超过 48,000 字符的文件，`project_write` 永远
不能整文件替换它。这是闸在正常工作而不是失灵——模型确实没见过全文——但它必须被说出来，
所以那句话现在直接点名 `project_edit`。

最后两行是同一个事件的两句话，分开写是因为它们要求不同的下一步。把格式化器动过
的 mtime 说成"用户编辑了它"，模型会停下来报告；把用户的编辑说成"你自己的命令干
的"，模型会闷头重写。这是**猜测**，措辞也写成猜测（"may have done it"）：这里没有
任何东西能归因一次改动。

### 2.2 store 层：`if_unchanged` 与 `create_only`，因为工具层关不掉自己的窗口

`ProjectFileStore.write` 新增 `if_unchanged: ProjectFileVersion | None = None`。
不为 `None` 时，stat 与写入在**同一个 offload 闭包里**完成，抛
`ProjectFileChangedError`。

放在 store 里是这个参数存在的**唯一**理由。调用方先 stat 再写，两者之间就有窗口;
store 在同一次调用里检查则没有。这不是锁——**另一个进程仍然可以插进来**——它关掉的
是本进程自己开的那个窗口。

`ProjectFileVersion` 带两个字段而不是只带时间戳。mtime 在保留纳秒的文件系统上是个
好检查，在只保留整秒的文件系统上是个差检查：那里用户在读之后同一秒内的保存是不可见
的。尺寸不要钱，且能挡掉其中常见的一半。**两者都挡不住**的是同长度重写加
`touch -r`——那是刻意行为，这里点名，不设防。

**新建路径有它自己的同一个窗口。** `project_write` 先问 `store.exists`、再写，这是
两次独立的 `await`——正是 `if_unchanged` 存在的理由，只不过发生在"不存在"那条分支上。
实测：一个在这两跳之间出现的文件会被无条件覆盖，从没被读过，绕过本 ADR 的不变量 1 与
ADR-072 的新第六条。所以 store 另加 `create_only`：在同一个闭包里再问一次，抛
`ProjectFileExistsError`。两个参数互相矛盾，同时给是 `ValueError` 而不是优先级规则。

`ProjectFileExistsError` 与 `ProjectFileChangedError` 分成两个类，因为下一步不同：
后者是"你读过它、它动了"，前者是"它是在你决定的时候出现的，本回合从没见过它"——所以
一个说"再读一遍"，一个说"读一遍"，合并成一句就会有一半时间指错方向。

`project_edit` 不查回执（它自己在一句话之前读过文件，且 `count(find) == 1` 已经
挡掉了大部分错记），但它**必须**把自己那次读的版本传下去。不传的话，这个新参数只
保护了两条写路径里较弱的那一条，而较强的那条正是模型会优先用的。

### 2.3 写完必须刷新回执，但 edit **不得凭空造出**覆盖范围

两个写工具在成功之后都用 store 返回的 `ProjectFileEntry` 重记一笔。不这么做，模型
对自己刚写的文件的**第二次写会被自己的闸拒掉**——回执还描述着读到的那份，而它刚做
的写把尺寸和 mtime 都挪了。值取自返回的 entry 而不是回读，这正是
`ProjectFileStore.write` 返回一个 entry 的用途。

**两条写路径在这里必须不同，写成一样会把整道闸打开。** 第一版就写成了一样，实测
两次调用即可绕过：

```
project_edit(path="big.py", find="MARKER", replace="MARKED")   # 30 KB，从没读过
  -> 记下一条 covers_whole_file=True 的回执
project_write(path="big.py", content="# gone\n")               # 通过，30 KB 没了
```

原因是 `project_edit` 拿到的是一个片段：**读文件的是 store，不是模型**。
`project_write` 相反——模型交出了每一个字节，按构造它见过整个文件。所以：

- `project_write` 成功后记 `covers_whole_file=True`。
- `project_edit` 成功后**沿用**编辑之前那条回执的覆盖范围；编辑之前没有回执，编辑
  之后也不记。模型看到的东西没有因为一次片段替换而变多。

沿用而不是丢弃，是因为读过全文再编辑的模型仍然知道整个文件：它手里是读到的文本加
上自己写的那段替换。此时丢掉回执会拒掉它自己的下一次写，白白换一次重读。

### 2.4 台账每回合一份，且台账不存在时是拒绝

回执是"模型**刚刚**看过这个文件"的断言。带进下一个回合，它就变成关于几分钟前那份
文件的断言，而那恰恰是这套机制要不信任的信念——只不过现在它带着闸门自己的批准。

`ReadReceipts` 的每个方法在未进入回合时**抛异常**。另外两种形状都更糟，且都在转录
里显得正常：临时造一个空台账，会对每一次写回答"你没读过"——一个什么都拒绝的闸看起来
像在工作，实际是没接线；返回一个私有台账，会把每次读记进没人查的表——一个没接线的闸
看起来像在工作。

同理，`CodeSessionService.__post_init__` 拒绝"给了 `project_scope` 却没给
`read_receipts`"的装配。这个错误发生在装配处，就该在装配处炸，而不是在第一次写的
时候。反方向不拦：有台账没 scope 是无害的，此时根本不会提供任何 `project_*`。

## 3. 这道闸喂不满，这是接受的代价

**必须写下来的现实**：目录会被绕过工具的东西改动，而回执看不见其中大部分。

- `PUT /v1/projects/{project_id}/file`（`apps/api/routes/projects.py`）走同一个
  `store.write`，且**不带**前置条件。这是**对的**：那是用户自己在动自己的文件，
  没有谁会跟他们赛跑。
- 用户的编辑器、`git`、任何别的进程。
- `project_run` 能改根下任何东西，且不经过任何能记回执的工具。

后果是回执会看见它没造成的合法 mtime 移动，模型会因此被拒一次、重读一次、再写。
**这是代价不是缺陷**：多一次读，换掉一次静默的数据丢失。反过来的错误——闸放行了一次
真该拦的写——没有第二次机会。

同样要写下来的：这**不能**降低 `project_write` 的风险等级，也不构成"写入停在人面前"
的替代品。`code_session.py` 的 `approval_required_risks` 仍然只有 `("destructive",)`，
`project_write` 仍然是 `write`，按构造不停在人面前。这道闸挡的是"覆盖你没看过的
东西"，不是"未经批准就写"。后者是 ADR-079 的事。

## 4. 被拒绝的方案

**在回执里存内容的 sha256。** 最初的计划里有。放弃了，因为**没有东西读得到它**：
`project_write` 手里没有文件的当前内容，要比对就得在每次写之前多读一次整个文件。
一个没有消费者的字段正是本仓反复删掉的那种东西（ADR-059）。尺寸加 mtime 是能在不多
读一次的前提下检查的全部。

**给读取结果加行号，靠行号定位写入。** 拒绝，理由与 F-13 相同、且更强：这里的写工具
是整文件替换，而提示词第 1、2 条教模型"先读再写"。模型读到 `1\tdef f():` 再重写，就
把行号写进了用户的真实文件，没有版本可退。

**新增一个 `ErrorCode`。** 拒绝。`project_edit` 早就用 `invalid_tool_input` 回答
"你以为在那儿的片段不在那儿"，这是同一个事件的另一条写路径，就用同一个码。加第十五个
码还意味着 `web/src/features/work/failure.ts` 多一个分支——为一个消息本就承载的区别
改词表。

**把回执做成会话级而不是回合级。** 拒绝，见 §2.4。

**做成锁。** 拒绝。锁要跨进程，要有超时，要有持有者身份，还要回答"用户的编辑器被锁
住之后会发生什么"——答案是编辑器不理会它。前置条件是这个问题的正确形状：不阻止任何人
做任何事，只保证一次写不会盖掉它没看见的改动。

## 5. 不变量

1. `project_write` 替换一个**已存在**的路径时，本回合必须有该路径的
   `covers_whole_file=True` 回执，且文件自那以后未变。**新建路径由 store 的
   `create_only` 在同一个闭包里再确认一次**，所以"不存在"这条分支上也没有窗口。
2. 一次不是全文的读**不产生**全量回执。窗口、非文本，都不算读过。**但读取只增不减**：
   文件未动时，更窄的一次读不会降低更宽的那次挣到的覆盖范围。
3. 回执**不跨回合**。`ReadReceipts.using()` 每次进入给一份新的空台账。
4. 未进入台账时，`ReadReceipts` 的每个方法抛
   `ReadReceiptsUnavailableError`；给了 `project_scope` 而没给 `read_receipts`
   的装配在构造时抛 `ValueError`。
5. `if_unchanged` 的 stat 与写入在同一次 store 调用内完成。
6. 两条写路径成功后都刷新回执的尺寸与时间戳，但**只有 `project_write` 产生
   `covers_whole_file=True`**。`project_edit` 沿用编辑前的覆盖范围，编辑前没有回执
   就不记——它拿到的是片段，读文件的是 store 不是模型。
7. 回执**不命名任何版本**，也不给任何工具 schema 增加输入属性——F-13 保持拒绝，
   `tests/architecture/test_a_workspace_version_is_never_asked_for.py` 保持绿色。
8. 扁平工作区不受影响：它给每次写产生新版本，"你先读了吗"在那里是关于一份可以取回
   的文件的问题。

ADR-072 §5 的不变量表相应增加第六条：**一次整文件替换要么落在一个本回合读过全文
且未变的文件上，要么落在一个不存在的路径上。**

## 6. 怎么验证

- `tests/adapters/test_project_tools.py::TestAFileYouHaveNotReadIsNotYoursToOverwrite`
  ——13 条：未读被拒且文件原样、新建不需回执、全文读放行、窗口读不放行、
  非文本读不放行、读后文件被改被拒、跑过命令时换一句话、写后刷新回执、
  edit 不需回执、**edit 不能把没读过的文件洗成读过的**（30 KB 的实测样本）、
  **edit 沿用已经挣到的回执**、**edit 之后窗口回执不会被提升成全量**、
  **更窄的读不会撤销更宽的读**、**文件动过时不沿用覆盖范围**、
  **窗口拒绝语指向真正可行的动作**（84 KB、分两次读完仍被拒的实测样本）、
  **检查与写入之间出现的文件被拒**（用一个在 `exists` 与 `write` 之间让文件出现的
  store 驱动），以及 edit 自己那条竞态（用一个在 read 与 write 之间让用户保存的
  store 驱动）。
- `tests/adapters/test_project_file_store.py::TestAConditionalWrite`——5 条：
  未变则写、mtime 变则拒且不落盘、mtime 同尺寸不同仍拒（这条是
  `ProjectFileVersion` 为什么带两个字段的证据）、文件消失则拒而不是重建、
  不传前置条件仍是无条件写。
- `tests/application/test_file_read_receipts.py`——9 条：未进入回合时三个方法各自
  的拒绝、回合内的记录与查询、后一次读覆盖前一次、跨回合不继承、嵌套时恢复外层。
- `tests/application/test_code_session.py::test_a_project_capable_session_without_receipts_is_refused_at_assembly`
  ——半接线的装配在构造时被拒。
- `tests/contracts/test_port_contracts.py`——`ProjectFileVersion` 进样本表，
  与其余 port 聚合走同一套 JSON 往返与 schema 版本断言。

能力梯子停在 **Implemented + Tested**。这条路径从未对真实模型跑过
（`docs/architecture-baseline.md` 记着 Code 提示词从未做过真实模型验证），所以
"模型在被这样拒绝之后会去重读"是**未经证实的**——被证实的是它拿到了一句指名下一步
的话，以及那次写没有落盘。
