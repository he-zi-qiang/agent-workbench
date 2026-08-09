# ADR-028：任务工作区是可变的名字压在不可变的字节上

- 决策点：Agent 能不能在一个 Task 内部积累工作产物、读回来、在上面继续改；这个可变状态
  与"节点整体重放"怎么共存
- 状态：**接受**
- 日期：2026-08-09
- 影响：新增 `workspace` artifact kind 与 workspace 版本清单；`TaskState` 增加一个
  workspace 版本引用；新增三个原生工具；`writer/synthesize` 获得它们。
  `CANONICAL_V1_NODE_IDS`、ADR-009、`SUPPORTED_KEYWORDS` **均不变**

## 1. 背景

Task 现在能产出东西，但产不出**能继续加工的**东西。artifact 是写一次的内容寻址 blob，
节点之间靠固定的命名槽位传递——`draft_ref`、evidence bundle、最终报告。这套设计对
"一条流水线，每步产出一个成品"是合适的，对"一个 agent 干活"不是。

一个真正在干活的 agent 需要的是别的：把中间结果放下、隔一步再拿起来、改一版、把三份
东西合成一份。它需要**列目录、读回、覆写**。今天它一样都做不到——它甚至无法知道自己
上一步写了什么，除非那个东西恰好落在一个图预先定义好的槽位里。

这不是缺一个工具，是缺一层。缺了它，后面几乎所有事都做不成：沙箱执行没有输入输出的
落脚点，下载的文件没有地方待，多个渲染器的产物无法互相引用。

## 2. 冲突在哪

可变状态和这个系统最贵的那个性质直接对撞：**图节点是整体重放的**
（`interrupt_boundary = "graph_node"`）。

一个节点写了 `notes.md`，跑到一半崩溃，恢复后整个节点重来——此时它看到的 `notes.md`
是**上一次未完成的自己**留下的。这不是脏数据的小问题：节点的输入不再由 checkpoint 决定，
而是由"上次死在哪"决定，重放因此不可复现，事件流也不再能解释产物是怎么来的。

朴素的解法是让工作区可覆写并接受这个后果。这个 ADR 不那么做。

## 3. 决策

### 3.1 可变的是名字，不可变的是字节

工作区**不是**一个可写目录。它是一份 `名字 → ArtifactRef` 的映射，而 artifact 仍然是
今天那个内容寻址、写一次的对象。

写一个名字 = 把字节存成一个新 artifact + 产出一份新的映射。旧映射与旧字节都不消失。

这就是 git 的做法：树在变，blob 不变。它一次买到三样东西——

- **可变性**在名字层，agent 要的就是这个；
- **不可变性**在字节层，artifact store 的既有语义一个字不用改；
- **可审计**：任何一版工作区都能被完整重建，事件流里指得出某个产物出自哪一版。

### 3.2 每个节点入口钉住一个版本，写产生新版本

`TaskState` 增加一个 `workspace_version`（一份清单的 artifact id）。节点开始时读到的
是**它入口那一版**；写操作产生新版本并在节点成功结束时随 checkpoint 落盘。

于是重放是精确的：重放的节点看到的是和第一次**完全相同**的入口版本，上一次未完成的
写入还在存储里，但不在这一版清单里，因此对它不可见。

没有删除、没有回滚、没有 GC。这也意味着一个已知代价：**中止的节点写下的字节会留在
存储里且无人引用**——与 ADR-025 §2.7 记录的那个遗留是同一类问题，同样等一个 artifact
GC 工作包来解决，本版不假装已解决。

### 3.3 名字是名字，不是路径

`ArtifactRef` 的 docstring 已经写明理由：它刻意不带 URL、bucket 或文件系统路径，因为
"client-supplied path is exactly how path traversal and cross-tenant reads enter a
system"。工作区不能把这一条丢掉。

名字受约束：`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$`，**没有斜杠**，因此没有目录、没有 `..`、
没有绝对路径。需要层次时用命名约定（`draft-v2.md`），不用真实路径。

工作区按 `(tenant_id, task_id)` 隔离。跨 Task 读取不存在，不是靠检查，是靠清单只在一个
Task 的 state 里。

### 3.4 一切有界

与这个代码库其他地方一致：单文件字节上限、文件数量上限、工作区总字节上限。超界返回
结构化错误，**不截断**——截断的文件是坏文件，而坏文件会被下一步当成好文件读走。

### 3.5 三个原生工具，不走 MCP

```
workspace_list()            -> 名字、大小、media type
workspace_read(name)        -> 内容或 ArtifactRef
workspace_write(name, ...)  -> 新版本
```

这一次**不**做成 MCP server，与 ADR-025/026/027 的取向相反，理由是具体的：工作区要读写
artifact store 并修改 `TaskState`，那是 Worker 进程的内部权限。做成 MCP 意味着把存储
访问权交给一个独立进程，而 ADR-026 立的规矩恰恰是"MCP server 不接收路径与所有者字段"。
让一个 server 拿到 artifact store 会把那条规矩反过来。

风险声明：`workspace_read` / `workspace_list` 是 `read` + `safe`；`workspace_write` 是
`write` + `exclusive` + scope `workspace:write`，`idempotency="safe"`，**不带
`operation_key`**——它写的是我们自己的、版本化的存储，重放不产生第二个外部效果。

### 3.6 先给 `writer/synthesize`

其余五个 profile 的工具集合不变。写作是唯一目前就需要在自己产物上迭代的角色；
researcher 的产物是证据 bundle，已有它自己的通道。

## 4. 后果

- Agent 第一次能在一个 Task 内积累和加工工作产物；
- ADR-029（沙箱）与 ADR-027（下载）都有了落脚点，两者都依赖本 ADR 先落地；
- 重放语义**未变弱**：节点入口版本由 checkpoint 决定，与今天一样可复现；
- 未配置这些工具的部署行为逐字节不变；
- **不解决**：跨 Task 共享工作区、目录层次、删除与 GC、并发写同一名字
  （节点内单线程，`exclusive` 已经保证）。

## 5. 备选方案

**一个真的可写目录挂在 worker 本地。** 最像 Claude Code，也最直接撞上重放：本地磁盘不在
checkpoint 里，另一个 worker 接管这个 Task 时那些文件根本不存在。

**允许覆写、不做版本。** 少一份清单，代价是节点输入取决于上次死在哪。见 §2。

**做成 MCP server 以求一致。** 见 §3.5——一致性在这里会要求交出存储权限，代价比收益大。

**复用 evidence bundle 当工作区。** 它是 researcher 的产物契约，`admits` 规则依赖它的
形状；把它变成通用可写区会同时破坏两个 researcher 的独立性。
