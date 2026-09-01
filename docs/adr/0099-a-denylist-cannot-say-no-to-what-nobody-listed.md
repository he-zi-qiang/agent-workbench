# ADR-099：黑名单说不出「没人列过的那个也不行」

- 决策点：核心层的第三方依赖守卫是一份**黑名单**（`FORBIDDEN_CORE_IMPORTS`），
  于是 `domain/workspace.py` 的 `import regex` 一路绿灯，而中英两版 README 同时写着
  「`domain/` 只依赖标准库与 Pydantic」且「这条边界会让 CI 变红」——
  是改口径承认守卫只挡具名框架，还是把守卫改成白名单
- 状态：**接受**，改成白名单：核心层只许 import 标准库、自己，以及一张写明理由的清单
- 日期：2026-08-31
- 影响：`tests/architecture/test_dependency_boundaries.py` 新增
  `CORE_THIRD_PARTY_ALLOWLIST` 与四条测试；`FORBIDDEN_CORE_IMPORTS` **保留**（见 §3.2）。
  **一行生产代码都没动**——当前核心层的第三方 import 只有 `pydantic` 与 `regex` 两个，
  两个都在清单里。README 中英两版的口径同批更正
- 依赖：[ADR-097](./0097-a-funnel-nobody-reads-is-not-a-funnel.md) 与
  [ADR-098](./0098-a-ceiling-that-only-one-process-reads-is-not-a-deployment-ceiling.md)
  （同一个月的另外两条「声称的和执行的不是一回事」）

---

## 1. 背景：一个有正当理由的 import，和一句挡不住它的承诺

`domain/workspace.py:34` 是 `import regex`。它有正当理由，而且理由是**安全**的：
标准库的 `re` 没有超时，一个来自工具调用的病态正则可以挂住整个事件循环，
而那个循环是这个进程里每一次运行共用的。`GREP_TIMEOUT_SECONDS` 靠 `regex` 的
`timeout=` 参数成立。

**问题不在这个包，在守卫的形状。**
`FORBIDDEN_CORE_IMPORTS` 列的是禁止项——langgraph、llama_index、httpx、sqlalchemy……
三十来个具名的框架与 SDK。`regex` 不在其中，所以 CI 一直是绿的。

而中英两版 README 的分层表写着：

> **domain** …… 允许依赖：标准库、Pydantic、domain 自身

紧接着写着：

> 这条边界是一条**会让 CI 变红**的测试。

**第一句是假的，第二句挡不住它。** 两句连在一起读，给出的印象是
「核心层的依赖被守住了」，而实际被守住的只有「这三十来个具名的东西」。

## 2. 真正的问题不是漏了一个包

一份黑名单能回答的问题是「**这个是不是我们说过不要的那几个**」。
它答不出「**有没有人同意过这个东西进核心层**」。

两者的差别在**下一个**包身上：一个新的第三方依赖进核心层，
按今天的守卫**不会有任何东西变红**——它要等到某个人碰巧读到那一行 import，
而这正是 A-07、F-26、A-08 反复出现的形状（[已知缺口](../known-gaps.md)）。

所以两条出路不对称：

- **改口径**（承认守卫只挡具名框架）：诚实，但把一条本该成立的保证降级成一句说明，
  而且 README 那句「会让 CI 变红」还是得删——**读者失去的不是准确性，是那道闸**。
- **改成白名单**：那句话第一次为真。

**选白名单。** 代价小得出奇：当前核心层的第三方 import 一共两个。

## 3. 决定

### 3.1 白名单是规则，条目自带理由

```python
CORE_THIRD_PARTY_ALLOWLIST: dict[str, CoreDependency] = {
    "pydantic": CoreDependency(reason=...),
    "regex":    CoreDependency(reason=..., modules=frozenset({"domain/workspace.py"})),
}
```

`modules` 在理由是**具体**的时候把许可收窄到具体文件。
`pydantic` 是 `None`（"核心层任何地方"），而这是它独有的：
它是这个项目写不变量用的语言——`DomainModel` 全局 `frozen=True, extra="forbid"`，
把它钉到一张文件清单上，等于列出每一个核心模块。
`regex` 则钉死在一个文件上：它的理由是一个具体函数的超时，不是一条通用许可。

标准库用 `sys.stdlib_module_names` 判定，**不手写清单**——
一份手写的标准库名单和这条白名单要消灭的错误是同一种形状，而且永远落后一个版本。

### 3.2 `FORBIDDEN_CORE_IMPORTS` 保留，两者分工不同

白名单在覆盖面上严格包含黑名单，删掉后者是有道理的。**不删**，理由是**错误信息**：

- 黑名单命中时说的是「把这次集成挪到 adapter 后面去」——它知道你 import 的是什么，
  也知道这个项目**考虑过并且拒绝了**它；
- 白名单命中时只能说「没有人批准过这个」。

对一个把 `httpx` 写进 `domain/` 的人，第一句直接指出该往哪走。
**两个守卫、一条规则，因此它们不许互相矛盾**：
`test_the_allowlist_and_the_rejection_list_never_disagree` 断言两张表的交集为空。
没有这条，将来某次编辑可以把某个包同时放进两边，
而哪个测试先跑决定构建是绿是红——那正是本文件存在要防的漂移，上升了一层。

### 3.3 白名单也要能变小

`test_every_allowlisted_core_dependency_is_still_imported`：
清单里的包若没有任何核心模块 import 它，测试失败。

**一份只增不减的白名单就是换了顶帽子的黑名单**——
它会慢慢积成一组没人记得为什么还在的常设例外。
这和隔壁 `KNOWN_UNREAD_LEAVES` 的反向检查是同一条理由。

### 3.4 两条对照组，都实测过

- **`regex` 出现在 `domain/workspace.py` 以外**：白名单红，**黑名单绿**。
- **`attrs`（两张表都没见过）**：白名单红，**黑名单绿**。

第二列是这条 ADR 的全部意义：**这两次黑名单都说不出话**。

## 4. 后果

### 4.1 加一个核心层依赖从此是一次决定

以前是「写一行 import」，以后是「写一行 import，再往清单里加一条并写下理由」。
这不是手续：那条理由就是下一个读者判断「这个还该不该在这儿」的全部依据，
而 `regex` 这一条今天才第一次被写下来。

### 4.2 生产代码零改动，因此没有效果证据要等

本 ADR 不改任何运行时行为，两个包本来就在核心层里。
拿到的是**机制**证据（两条对照组各让白名单红一次、黑名单绿一次），
而这里也没有别的效果可谈——一条守卫的效果就是它会不会在该红的时候红。

### 4.3 明确没做的三件

- **没有把 `ports/` 与 `domain/` 分开管**。理论上 `ports/` 该比 `domain/` 更窄
  （它只写 `Protocol`），但今天两者的第三方依赖是同一组，
  拆成两张表会得到两张一样的表加一次将来会漂的同步。
- **没有管 `pyproject.toml` 的依赖分组**。核心层能 import 什么与这个包声明了什么依赖
  是两个问题；后者由 `pip-licenses` 与 `uv lock --check` 在 CI 里管。
- **没有回头收窄 `pydantic`**。它是 `None`，也就是「核心层任何地方」，
  这是本 ADR 唯一一处没有收紧的许可——见 §3.1 的理由。

## 5. 重审条件

若将来核心层需要第三个第三方包，而它的理由写不进一句话，
那说明要么它不该进核心层，要么这条白名单的粒度不对（比如该按层而不是按包分）。
**理由写不出来本身就是信号**，这是把 `reason` 做成必填字段而不是注释的原因。
