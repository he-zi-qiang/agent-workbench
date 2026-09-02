# ADR-104：原生启动脚本对存下的开关让路，用的是容器那一个探针

- 决策点：ADR-103 §5.5 明确写着「不改 `scripts/dev.sh`」：它的 `demo-api` 与 `demo-worker`
  无条件导出 `AW_RESEARCH__ENABLED=true`，存下的开关排在环境之下，于是「运行状态」页把
  原生路径上的每一次启动都报成 `overridden`。那句话是诚实的；问题是压过这个开关的
  「启动环境」不是任何人设的，是启动脚本自己——一个人在控制台上把联网搜索关掉、按文档
  重启、回到页面，看到的是「启动环境里显式给了这个值」，而他从没给过。要不要让脚本让路，
  以及怎么让路才不会变成 §5.5 担心的「两套谁让路给谁」
- 状态：**接受**。两个 arm 改成和 `docker/run-api-local.sh` 同一条规则、同一个探针。
  **部分推翻 ADR-103 §5.5**，推翻的边界写在 §3
- 日期：2026-09-02
- 影响：`scripts/dev.sh` 的 `demo-api` / `demo-worker` 两个 arm；`docker/decide_web_search.py`
  的用途从「只给容器启动脚本用」变成两个启动器共用；`bootstrap/switches.py` 的 docstring；
  新增 `tests/config/test_dev_script_web_search.py`。加载器、优先级、页面、Compose、
  Windows 启动器一字未动
- 依赖：[ADR-103](./0103-an-optional-part-can-be-switched-from-the-console-for-the-next-start.md)
  （开关文件、`stored` / `active`、搁置规则、容器启动脚本让路的那一段）、
  [ADR-102](./0102-a-deployment-says-what-it-could-not-assemble.md) §3（探针为什么在进程外面）、
  [ADR-101](./0101-the-console-may-hand-over-a-key-it-can-never-read-back.md)（key 文件的位置，
  开关文件由它推出）

---

## 1. 背景：报 `overridden` 是诚实的，但压过它的是脚本自己

ADR-103 §3.2 把存下的开关排在环境变量之下，理由成立：操作者导出的值是这台部署自己的
决定，页面报 `overridden` 并写明「启动环境里显式给了这个值」，而不是装作能改。

§5.5 据此说：`scripts/dev.sh demo-api` 无条件导出 `AW_RESEARCH__ENABLED=true`，会落进这一格，
「那个脚本不改，页面说实话就够了」。

在真的原生栈上把这条路走一遍，它不够。控制台在这台机器上是怎么起的？
`.claude/launch.json` → `.claude/run-api.sh` → `scripts/dev.sh demo-api`；文档
（`docs/running-locally.md`）教的也是 `demo-api` / `demo-worker`。也就是说**原生路径上没有
一条起控制台的命令不经过这两个 arm**。一个人在「运行状态」页把联网搜索拨到「关」，页面
告诉他重启 `agent-api` 与 `agent-task-worker`，他照做，回来看到：

> 这次启动：开 · 下次启动：关 · 启动环境里显式给了这个值，压过了这里的选择

他没有给过任何值。页面说的每个字都对，而整句话是错的：它把脚本的一个 `export` 报成了
操作者的决定，并且暗示改环境就能生效——而改环境的办法就是去改那个脚本。ADR-103 造出来
的开关，在这条路径上是一个**永远拨不动**的开关，这正是 §3.2 为出厂 TOML 那一行说过的话：
「一个输给出厂文件的开关是一个谁也拨不动的开关」。

还有一个更老的 bug 藏在同一行里，和开关无关：`export AW_RESEARCH__ENABLED=true` 跑在
操作者自己的 `export` **之后**。一个在 shell 里导出了 `false` 再起 `demo-api` 的人，得到的是
`true`，而且从命令行上没有任何办法看出进程用的是哪个值。容器启动脚本从 ADR-102 §3 起就
「显式值不碰，只决定未设或空的」；原生脚本从来没有。

## 2. 决定：同一个探针，同一条规则

两个 arm 里那一行换成容器启动脚本已经在跑的那三行：

```bash
if [ -z "${AW_RESEARCH__ENABLED:-}" ]; then
  if "$PYTHON" docker/decide_web_search.py >&2; then
    export AW_RESEARCH__ENABLED=true
  fi
fi
```

于是四种情形在两个启动器上是同一张表：

| 启动时 | 容器（ADR-103 §3.4） | 原生（本 ADR） | 进程里 `research.enabled` |
|---|---|---|---|
| 环境里显式给了值 | 不问探针，原样 | 不问探针，原样 | 环境的值；文件说的不一样就报 `overridden`——**这一格现在只在真有人导出时出现** |
| 文件里存了「开」 | 探针退出，不导出 | 同 | 加载器应用；没有 key 就搁置（§3.3） |
| 文件里存了「关」 | 探针退出，不导出 | 同 | 加载器应用：关 |
| 谁也没决定 | 探到 key 就开 | 同；这两个 arm 没有 key 会在更早一行拒绝启动，所以这里总是开 | 开 |

探针把走了哪条路打到 stderr，两个启动器里是同一句话（`research.enabled is stored as false
(系统 > 运行状态): the settings loader decides chat web_search`）。它的 stdout 也送到 stderr，
因为这个 arm 的 stdout 属于它即将 `exec` 的进程——测试套件用一个替身 `$PYTHON` 读那一行。

`demo-api` 结尾那句横幅从「Word + web + sandbox + chat search」改成如实写出
`research.enabled=` 是 `true`、是操作者的值、还是「由存下的开关决定」。一个存了「关」的启动
打出「chat search」，就是「运行状态」页被造出来要制止的那种话。

## 3. 与 ADR-103 §5.5 的关系：担心的是什么，为什么现在不成立

§5.5 的原话：「让脚本读文件是可以做的，但那会让原生路径和容器路径各有一套『谁让路给谁』，
而今天两条路径靠同一条规则就说得清。」

它担心的是**两套规则**：原生脚本自己解析 `switches.json`、自己定一个优先级，和容器那一套
慢慢错开。这个担心是对的，而答案不是不改，是**不写第二套**：原生脚本调用的是容器那个
文件——`docker/decide_web_search.py`，它背后是 `bootstrap/switches.py` 的
`launcher_decides_web_search()`，读文件用的是存储和加载器共用的那个解析器（ADR-103 §2
「两个方向都拒绝不认识的键」的那一个）。改了它，两个启动器一起变；一条测试钉住两个启动器
名字里写的是同一个文件。

所以「今天两条路径靠同一条规则就说得清」这句话在本 ADR 之后**更**成立，不是更不成立：
此前是「加载器一条规则、两个启动器两种行为」，现在是一条规则、一个探针、两个调用点。

**守住的**：ADR-103 的优先级一字不动——环境 > 存下的开关 > TOML。页面在真有人导出时照旧报
`overridden`。`api` / `worker` 两个普通 arm 从来不导出这个变量，加载器在它们身上本来就应用
存下的开关，不用改。Windows 那条路（`scripts\stack.cmd` → Compose → 容器启动脚本）本来就
让路，不用改。

**推翻的**：「不改 `scripts/dev.sh`」这一条，以及它给出的那个理由。

## 4. 明确不做的

1. **不把探针搬出 `docker/`。** 一个原生脚本去调 `docker/` 下的文件，看起来放错了地方；
   但 Dockerfile 是 `COPY docker ./docker` 整个目录，搬到 `scripts/` 意味着镜像里要多
   COPY 一个目录或者留一份副本——而副本正是 §3 要避免的第二套。名字留在原处，docstring
   写明它有两个调用者。
2. **不在 shell 里解析 `switches.json`。** 哪怕只是 `grep research.enabled`，也是第二个
   解析器：它不认识「不认识的键」，不会像加载器那样带着文件名拒绝，而且一个 bash 里的
   JSON 读法和一个 Python 里的迟早对同一个文件说出两句话。
3. **不改 `api` / `worker` 两个 arm。** 它们没有 key 也能起（搜索无对话、demo 图），
   `research.enabled=true` 在那里是启动错误，所以它们从来不导出这个变量；存下的开关在它们
   身上已经由加载器应用或搁置。给它们加探针是在一个没有问题的地方复制一段代码。
4. **不改 `.claude/launch.json` / `.claude/run-api.sh`。** 后者只是 `exec scripts/dev.sh
   demo-api`，它的注释里那段「dev.sh 无条件导出」的历史改成现在的事实即可。
5. **不热加载、不做认证、不产生事件。** 与 ADR-103 §5 同一组理由，本 ADR 没有碰任何
   会改变它们的东西。

## 5. 证据

[status.md 第六十九批](../status.md)：真探针 + 真加载器在原生脚本上跑过的七个场景（含
`main` 上旧脚本的对照组），以及门禁数字。
