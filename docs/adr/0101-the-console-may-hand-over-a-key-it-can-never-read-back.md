# ADR-101：控制台可以交出一把 key，但永远读不回来

- 决策点：这台部署的模型 key 只能由启动进程的人从环境里给，而控制台里没有任何位置
  能设置它——是把「设置一把 key」做进控制台，还是维持「key 是启动参数，不是设置项」
- 状态：**接受**，做进控制台，**并且只写不读**；同时把这条改动的前提写死在 ADR-044 上
- 日期：2026-09-01
- 影响：新增 `apps/api/routes/settings.py`（本仓库第一个写配置的端点）、
  `application/provider_key.py`（写盘与拒绝规则）、`bootstrap/provider_key.py`（只回答
  「key 文件在哪」）。`load_settings` 新增第四个显式入参 `provider_key_file`；
  `AW_KEY_FILE` 进入 `CONTROL_ENV_VARS`——它此前不在里面，导出它的进程一律起不来。
  控制台设置面板新增「模型密钥」一类
- 依赖：[ADR-044](./0044-no-remote-no-production-identity.md)（只信请求头、只绑
  loopback），[ADR-095](./0095-a-page-that-shows-nothing-is-not-a-page.md)（同一形状的
  只读反代路由）

---

## 1. 背景：key 在哪，谁读得到它

模型 key 的来源此前只有一条：进程环境里的 `AW_SECRETS__DEEPSEEK_API_KEY`。
`scripts/dev.sh` 在它缺席时读 `AW_KEY_FILE`（默认 `~/.config/agent-workbench/key`）
并导出——**而这件事只有那个 shell 会做**。Python 侧从来没有读过那个文件：
`grep -rn AW_KEY_FILE src/` 在这份 ADR 之前零命中。

后果是两条，第二条更难看：

| | 现象 |
|---|---|
| 换一条启动路径 | `uv run agent-api`、Windows 上的启动器、容器，都跳过那段 shell。key 明明躺在文档说的位置，进程却报告「没有 provider」 |
| 导出那个变量 | `_reject_unknown_environment_variables()` 抛 `unknown Agent Workbench environment variables: AW_KEY_FILE`。**`.env.example` 和 `CLAUDE.md` 都让人用这个名字**，而用了就起不来。没人撞上，只因为 `dev.sh` 是用 shell 局部变量而不是 `export` 写的 |

所以在做「控制台里能不能设置 key」之前，先有一个更朴素的事实：这个仓库对自己文档里
写着的那个变量是拒绝的。

## 2. 这是一次边界改动，不是一个表单

在这份 ADR 之前，**本仓库没有任何一个端点写配置，更没有写密钥**。最接近的写操作是
`POST /v1/evaluation/runs`，它起一个子进程。所以诚实的说法不是「加了一个设置页」，
而是：**控制平面获得了一次对 checkout 之外文件的写权限，写的内容是一把凭据。**

而 [ADR-044](./0044-no-remote-no-production-identity.md) 说的是：Identity Adapter 只
信任请求头，API 只绑 loopback，这是受控的本机开发而不是部署。两件事放在一起的含义
必须写出来，而不是留给读者推：

> **任何能碰到这个端口的东西，都能设置这把 key。** 路由像其它路由一样解析 principal，
> 但那是形状，不是防御——请求头是请求者自己写的。

这条不做缓解。能缓解它的东西叫认证，而这台部署没有，[已知缺口](../known-gaps.md)里
一直记着。这份 ADR 做的是把代价写下来，并且把**在没有认证的前提下仍然能守住的那几条**
守住。

## 3. 决定：只写不读，不热加载，不进 checkout

### 3.1 key 不会被读回来

`GET /v1/settings/provider-key` 返回的最长字段是四个字符的指纹（末四位），
以及两个布尔。**没有任何方法返回明文**——不是这一版省略了，是那一侧就没写。

理由是这条边界唯一一条不需要认证也成立的推理：一个能读回明文的端点，把「有人碰到了
这个端口」变成「有人拿到了这把 key」。不写那个方法，是最便宜的不让它发生的办法。

指纹取末四位而不是前缀：供应商在前缀放固定标记（`sk-`），前缀识别的是厂商而不是这
把 key，两把不同的 key 会显示同一个。

key 走请求体而不是路径或查询串，所以任何记 URL 的东西都记不到它。

### 3.2 存下的 key 不会立刻生效，而界面必须说出这件事

模型客户端在**组装期**构造一次，而 chat 那几条路由只有在那次构造拿到了 key 时才会
被挂上（`main.py` 的 `if dependencies.serves_chat`）。所以一把此刻存下的 key，是
**下一次启动**才会用上的 key。

这不是可以绕过的：让它热生效意味着运行中重建模型客户端、重挂路由，而「一个进程提供
哪些东西是启动时定死的」是这套设计刻意的选择——一条每次请求 500 的路由，比一个客户端
一次就能发现的 404 更糟。

于是 `ProviderKeyStatus` 有 `active` 和 `stored` **两个**布尔，而不是一个「已配置」。
把它们并成一个，正是一个设置页会声称刚存的 key 已经在用、而用户回头发现 Chat 还是
不在的那条路。设置面板自己的文档里写着这条规矩：

> 只装真能改或真能看的东西。一个设置面板最容易犯的错是把「以后想支持的」先画上：
> 一个点了没反应的开关，读者读成的是坏了。

一个说了「已保存」却什么也没变的开关，是同一个错的另一种拼法。所以界面分两格说，
并在两者不一致时给出该重启什么。

### 3.3 key 不会被写进 checkout

`ProviderKeyStore` 在写之前拒绝任何解析后落在 `CHECKOUT_ROOT` 之内的路径。
`zip -r` 和 Finder 的「压缩」都不认 `.gitignore`，所以工作树里的密钥会在第一次有人
打包这个文件夹发给别人时离开这台机器。这条规矩此前是文档里的一句话，现在是一次失败。

写入本身是先写邻居再 `replace`：一次中途崩溃留下的半截 key 是可读、非空、且错的，
那会在供应商那边报错——比在这里报错晚一层。

## 4. `load_settings` 为什么多一个显式入参，而不是自己去找

第一版把「环境变量缺席就读 key 文件」直接放进 `load_settings()`。它跑通了，然后
`tests/config/test_settings.py` 红了两条，而**原因比那两条测试重要**：它读到的是开发
者自己 `~/.config/agent-workbench/key` 里的真 key。

`load_settings()` 被测试套件调用几百次，也被 `agent-config-check` 调用。让它去读家目录，
等于让这些调用的结果取决于它们跑在谁的机器上——而这正是配置这一层存在的理由的反面。

所以它多了第四个入参 `provider_key_file`，与 `config_file` / `env_file` / `secrets_dir`
并列，**默认是「不要去看」**。指向那个文件是**进程入口**的事：`agent-api` 和
`agent-task-worker` 各自在 `main()` 里传，这也正是 `dev.sh` 一直待的那一层——
「这一处知道本机环境」。

值经由 `settings_customise_sources` 排在 `env_settings` 与 dotenv **之下**注入，
而不是构造完再补：`Settings` 有跨字段校验，一个开了 `research.enabled`、key 在文件里的
部署，会因为「校验时还没有 key」被拒绝启动。值必须在校验跑的时候就在场，而这就是
source 的用途。

## 5. 拒掉的几个做法

| 做法 | 为什么不 |
|---|---|
| 存进数据库 | key 会进备份、进 dump、进任何一份 `pg_dump`。文件在 checkout 之外、`0600`、且不参与任何一条数据路径 |
| 存进 TOML | `_reject_sensitive_toml_fields` 直接拒绝 `[secrets]` 表。这条规矩早于本 ADR，且是对的 |
| 复用 `AW_SECRETS_DIR` | 已有的挂载密钥机制确实能读 checkout 之外的文件，且零新机制。但它的语义是「部署方挂进来的」，而这条路径要的是「这台机器上的人自己存的」，两者在冲突检测上的期望相反（那边不一致就拒绝，这边环境变量本就该压过文件） |
| 每个用户一把 key | 需要新的存储模型、静态加密、按请求解析 key，并推翻「key 在组装期冻进进程」这条现有不变量。它是一个产品决定，不是这一条的延伸——[已知缺口](../known-gaps.md)里另记 |
| 让它热生效 | §3.2 |

## 6. 这条决定什么时候失效

**一旦这个 API 不再只绑 loopback，或者 Identity Adapter 不再只信请求头，本条就作废，
必须先写替代 ADR。** 这不是提醒，是判据：上面每一条「在没有认证的前提下仍然守得住」
的推理，前提都是「能碰到这个端口的只有坐在这台机器前的人」。前提没了，结论一条都不剩。

那时至少需要：这个端点的写权限要有真正的授权；`restart_required` 之外还要回答「谁存的」；
以及一条审计轨迹——目前一次存 key 不产生任何事件，因为这台部署的事件流是按 run 与 task
组织的，而这次写入不属于任何一次 run。那也是一条真实的缺口，登记在案。
