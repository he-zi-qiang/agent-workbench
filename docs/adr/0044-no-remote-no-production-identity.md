# ADR-044：先有远端部署，才谈得上生产身份与远程对象存储

- 决策点：生产身份（替换三个自述 header）与 S3 后端这一批做不做；今天那三处
  `backend != local` 的拒绝装配算不算完成；presigned 上传形状与既有
  `ArtifactStorePort` 的语义能不能共存；`config/ownership.yaml` 里两处登记给
  「不存在的模块」与「不读它的所有者」的字段怎么办；ADR-012 关于 `scopes`
  的表述已经被 ADR-015 事实性推翻这件事记在哪里
- 状态：**接受**。这一批**明确不做** S3 适配器、不做真实 IdentityAdapter、不做
  presigned 上传；唯一落地的是给三道既有护栏补回归测试。形状沿用
  [ADR-041](./0041-a-late-heartbeat-may-not-renew.md)（明确不做，并说清什么条件
  下重开）
- 日期：2026-08-11
- 影响：新增四条测试——`apps/api/dependencies.py:304`、
  `apps/task_worker/composition.py:243`、`apps/ingestion_worker/composition.py:110`
  三处 `backend != local` 的拒绝各一条，外加一条对照（`backend=local` 能正常
  装配）。**零生产代码变更、零新增配置叶子、零 `ownership.yaml` 变动、配置
  schema 保持 `1.14`、零迁移、零 `Literal` 变动。**能力表继续写 Planned。
  `config/config.production.toml` 继续每次 CI 被校验（`.github/workflows/ci.yml:107`）
  却装不出任何 API 进程——**这个别扭形状是被选择的，不是待办事项漏掉的**。
- 依赖：[ADR-041](./0041-a-late-heartbeat-may-not-renew.md)（上一批刚立的
  「明确不做，并说清什么条件下重开」的先例：本条是同一形状的第二次适用）、
  [ADR-012](./0012-identity-boundary.md)（身份边界；token/cookie 不越过 `apps/`
  边界；`scopes` 不是权限来源：本条记录它已被 ADR-015 事实性推翻，见 §6.1）、
  [ADR-015](./0015-export-authorization.md) §83-86（`export_artifact` 要求
  `artifact:export` scope：推翻发生的确切位置）、P1-3（内容不变也要换 ACL、
  推 revision、发 `acl_changed`，让撤销立刻生效：与 presigned 的 TTL 窗口正面
  冲突，见 §5）、[ADR-042](./0042-blocking-belongs-to-the-adapter.md) §13.2
  （抬版判据：本条复用它得出「零新增叶子 ⇒ 不抬版」）

## 1. 背景：两件事是同一件事的两半，而驱动它们的东西不存在

### 1.1 身份是三个 header，零校验

`src/agent_workbench/apps/api/identity.py:38` 的 `HeaderPrincipalResolver.resolve()`
读 `x-tenant-id` / `x-principal-id` / `x-principal-scopes`，只做「非空」与
「逗号切分」，然后直接构造 `PrincipalContext`。所以不是「信任一个 header」，
是**信任三个**：租户、身份、权限位全部由调用方自述。控制台把这份身份存在
`localStorage`（`web/src/app/IdentityContext.tsx:16`）并给了编辑对话框。

### 1.2 S3 是配置齐全、适配器零行

`bootstrap/settings.py:767` 的 `backend: Literal["local", "s3"] = "local"`；
`src/agent_workbench/adapters/artifacts/` 下**只有 `local.py`**。三处装配在
`backend != local` 时拒绝启动：`apps/api/dependencies.py:304`、
`apps/task_worker/composition.py:243`、`apps/ingestion_worker/composition.py:110`。

### 1.3 remote 部署不存在——实跑核过

仓库里**没有任何 remote 部署**：只有 `compose.yaml`（单机、`:134` 只发布
`127.0.0.1:8000:8000`、四个服务共享一个 `artifact_data` volume，见 `:136` /
`:162` / `:187` / `:205` / `:209`）、`Dockerfile`、`docker/loopback_proxy.py`。
**没有 k8s、没有 helm、没有 terraform、没有任何远程主机定义**（在仓库根实跑查过）。

`LocalArtifactStore` 能跨四个进程工作，**全部原因就是那个共享 volume**。反过来
说这件事更清楚：

- **没有多主机，S3 一点收益都没有**——今天四个进程读写的是同一个文件系统；
- **没有 S3 与 remote，生产身份也没有消费者**——loopback 上的调用方是本机。

### 1.4 今天的状态是自洽的 fail closed

三道护栏互相独立，并且都不是靠约定：

1. `ApiSettings.host` **无条件**只接受 loopback（`settings.py:167-181`），装配层
   再查一次（`dependencies.py:293`）。那段注释自己写明规则为什么是无条件的：
   「the scope is a label, and a label is not what decides who resolves identity」，
   并预告「When a real identity provider lands, this validator gains a condition;
   it does not go away」；
2. `deployment_scope == "remote"` 时 `build_dependencies()` 抛
   `InsecureDeploymentError`（`dependencies.py:285-291`）——这是生产身份的**唯一
   闸门**；
3. `Settings` 强制 `production ⇒ remote`（`settings.py:1254-1258`）。

净效果：`config/config.production.toml` 每次 CI 都被校验，**却装不出任何进程**。

### 1.5 真正的问题：这个「明确不做」从没被写下来，而支撑它的三处拒绝一条测试都没有

**实跑 `grep -rni "s3" tests/` 是零命中**（`grep` 返回码 1）。

而三处文档都把这个行为当成结论在引用：`README.md:232`、
`docs/architecture-baseline.md:1576`，以及 `docs/status.md` 多处，措辞一致——
**「是 fail closed，不是能力」**。

按本仓库的纪律，**被文档引用却没有回归测试的行为不算完成**。今天那三行拒绝是
三行没人验证过的代码，删掉任何一行 CI 都是绿的。

## 2. 决策：这一批明确不做

- **不写 `S3ArtifactStore`。**
- **不写真实 `IdentityAdapter`。**
- **不做 presigned 上传。**

维持现有三道护栏，一行生产代码都不改。

理由不是「没时间」，是 §1.3：**这两件事唯一的驱动力都是 remote 部署，而 remote
部署不存在。**在没有消费者的情况下把它们做出来，得到的是一个没人验证过的实现，
而它会看起来像一件已完成的能力。

## 3. 唯一落地的东西：让那句话变成有牙的

补四条测试：

| 测试 | 钉住什么 |
|---|---|
| `apps/api/dependencies.py:304` | `backend != local` ⇒ `InsecureDeploymentError` |
| `apps/task_worker/composition.py:243` | `backend != local` ⇒ 拒绝装配 |
| `apps/ingestion_worker/composition.py:110` | `backend != local` ⇒ 拒绝装配 |
| 对照 | `backend = local` 能正常装配 |

**对照那条不是凑数。**没有它，三条拒绝可以被一个「什么都拒绝」的实现满足，而那
样的测试证明不了拒绝是**针对 S3** 的。

这一步把「我们明确不做」从三行没人验证过的代码，变成一条**有牙的断言**。

## 4. 重开条件：三条缺一不可

写在这里，因为「什么时候重开」是本 ADR 的主要内容之一：

1. **真的存在一个 remote 部署。**不是计划里有，是仓库里有一份能把这套东西部署
   到多台主机上的东西。在那之前，S3 与生产身份都没有消费者（§1.3）。
2. **用户拍板 `scopes` 与 `tenant` 的权威来源**——token claim 还是服务端授权表 /
   成员表。今天 tenant、principal、scopes 全部由调用方自述，而**自述的 scopes
   已经是授权门**（`adapters/policy/envelope.py:41` 的 `missing_permission_scope`）。
   这一条见 §11，本 ADR 不替它作答。
3. **补上 ADR-012 自己承认基线 13.1 未覆盖的令牌层威胁模型。**ADR-012 写明了这
   个缺口；在它被补上之前，「换个 IdP」这件事没有可验收的标准。

## 5. 被否决的方案：presigned 与现有 Port 的四条硬冲突

三份文档承诺过 presigned 形状（`docs/architecture-baseline.md:827` 的上传数据面
图、`docs/configuration.md:433`、`docs/implementation-plan.md:595/608`）。本条把
它记成**被否决**，理由是它与已经生效的四条性质正面冲突，而不是「工作量大」：

1. **`ports/artifact_store.py:21-24` 明写「id 不是秘密」。**原文是
   「Artifact ids appear in tool results, event payloads and URLs, and a store
   that answered any id belonging to the right tenant would make every one of
   those places a capability. "Hard to guess" is not an authorization rule.」
   **presigned URL 恰恰是「难猜即授权」**，只是加了 TTL：一旦发出，S3 不再问
   principal。
2. **撤销。**P1-3 刚建立「撤销必须立刻生效」（内容不变也要换 ACL、推 revision、
   发 `acl_changed`；`adapters/persistence/documents.py:607` 是发它的地方）。
   一条尚在 `presigned_url_ttl_seconds`（`settings.py:773`，默认 300）窗口内的
   URL **撤不回来**。
3. **`complete` 的双向校验。**今天读已存对象自身的 size/digest，与传输之前客户端
   声明的值比对。presigned PUT 之后**服务端没见过字节**；S3 的 ETag 单段是 MD5、
   多段是复合值，**都不是 SHA-256**。要保住这条，要么开
   `x-amz-checksum-sha256`，要么 `complete` 时整个读一遍——而后者抵消了 presigned
   的全部收益。
4. **quarantine。**`LocalArtifactStore` 靠「临时名 → rename」保证读不到半成品，
   **S3 没有 rename**。等价物是 staging prefix + server-side copy，或者靠
   「元数据未写 ⇒ 视为不存在」。

## 6. 两件已经知道的事，本刀不修但不许再被忘掉

### 6.1 ADR-012 按自身条款已经该被取代

ADR-012 `:101-102` 写着：

> `scopes` 目前由调用方在请求头里自述，因此它**不是权限来源**，只是一个形状占位
> ……任何依赖 scopes 做授权的代码都属于越界。

而 `adapters/policy/envelope.py:41` 已经拿 `context.principal.scopes` 当授权门，
`ADR-015:83-86` **明确依赖它**：

> **principal scope 仍然要求。** `export_artifact` 声明 `artifact:export`，
> `EnvelopePolicyEngine` 要求调用方**持有**该 scope，否则 `missing_permission_scope`。

ADR-012 自己在「何时重新决策」里列的第三条触发条件——「有任何功能需要把 `scopes`
当作真实的授权依据」——**已经发生**。按它自身条款，它就该被一份新的取代。

**本 ADR 不订正 ADR-012**，只把这件事记下来。订正它是一份纯文档的 PR，属于另一
刀；但不记下来，这一批就会把一处既有矛盾留成一处没人知道的矛盾。

今天这不构成额外提权（身份本来就自述），但它是「对象级授权里有一条边的输入未经
认证」。

### 6.2 `ownership.yaml` 那道闸门管的是覆盖率，不是真实性

- `config/ownership.yaml:499` 把 `artifact_store.bucket` / `endpoint` / `region`
  登记给 owner `adapters.artifacts.factory`——**该模块不存在**
  （`adapters/artifacts/` 下只有 `__init__.py` 与 `local.py`）；
- `:510-514` 把 `presigned_url_ttl_seconds` 登记给
  `application.artifacts.upload_service`、lifecycle=`live`——**`UploadService`
  从不读它**：`bootstrap/projections.py:191-197` 的 `ArtifactStoreConfig` 只投
  `backend` / `local_root` / `max_artifact_bytes`。

owner 字符串只受正则校验，不校验模块是否存在。所以结论要写清楚：**「登记进
ownership.yaml」这道闸门管的是覆盖率，不是真实性。**

本条**只点名不改**（理由见 §8）。

## 7. 后果

### 7.1 为什么「做一半」比「明说没有」更糟

`docs/status.md:4694` 那段推理讲的原本是 outbox 缺 lease：

> 在这里做一半会得到一个看起来可恢复、实际不可恢复的东西，比一个明显的缺口更糟。

**它逐字适用于这里。**一个能启动、能存能取，但撤销有 TTL 窗口（§5 第 2 条）、
digest 校验悄悄退化（§5 第 3 条）的 S3 后端，比今天「三处明说没有适配器并拒绝
启动」更糟——**今天的拒绝是 fail closed 且可见**。

身份侧同理：一个 token 验签通过、但 `scopes` 仍然照抄 claim 而无人决定谁能签发
它的 IdP，会把「认证做完了」和「权限依然是自述的」这两件事**一起藏在一个更可信
的外观后面**。

### 7.2 代价，写在正面并明确接受

1. `config/config.production.toml` 继续每次 CI 被校验
   （`.github/workflows/ci.yml:107` 的 `agent-config-check --profile production`），
   **却装不出任何 API 进程**；
2. 能力表继续写 Planned（`docs/architecture-baseline.md:1550` 的表格行与 `:1576`
   的注解）。这些地方今天的口径是准的，本条不让它变得不准；
3. ADR-012 与 ADR-015 的矛盾继续挂着（§6.1）——**只是从此有了记录**；
4. `ownership.yaml` 那两处失真继续在（§6.2）——同样只是有了记录。

**这个别扭形状是被选择的，不是待办事项漏掉的。**

## 8. 配置影响：零

- **零新增配置叶子**，**不抬 schema 版本**（仍 `1.14`）。复用
  [ADR-042](./0042-blocking-belongs-to-the-adapter.md) §13.2 的判据：既然一个叶子
  都不加、一个 `Literal` 取值域都不动，抬版判据根本用不上；
- **零迁移。**当前 head 是 `migrations/versions/0025_agent_invocation_count.py`，
  本条不新增 0026；
- **`ownership.yaml` 那两处失真本 ADR 只点名不改。**修它属于另一刀，是纯文档
  变更，按「一个 PR 一项主要行为变化」不该塞进这一刀。**但必须留痕**，否则这
  一批会把一处既有失真，变成一处有人依赖的谎。

## 9. 另记两条缺口，本刀不修

### 9.1 对象存储端点是三个服务端点里唯一不要求 HTTPS 的

`artifact_store.endpoint` 走 `_validate_service_endpoint(allow_empty=True)`
（`settings.py:776-782`），只禁 userinfo / query / fragment，**不要求 HTTPS**。

而 `model.base_url` 无条件要求 HTTPS（非 loopback），remote 的 `qdrant.url`
也要求。**对象存储端点是三者中唯一不要求 HTTPS 的，也是唯一会把 URL 交给浏览器
的那个**（presigned 形状下）。

**重开时第一件事就是它。**

### 9.2 同一个仓库对同一类问题有两种形状

- `settings.py:1302-1309`：`backend == "s3"` 时**要求** bucket + endpoint + 两把
  凭据齐全（`secrets.artifact_access_key` / `artifact_secret_key`，
  `settings.py:1101-1102`）。即：Settings 帮你把 S3 配好，然后三个进程各自拒绝
  启动。
- `settings.py:843-862` 的 `reject_enabling_an_absent_runner` 采取**相反做法**
  ——直接拒绝，理由写着：

  > There is no code path behind it, so the only honest response to `true` is to
  > refuse the configuration and say what is missing.

**同一个仓库对同一类问题有两种形状。**本 ADR **不统一它**，只指出哪一种更诚实：
后者。统一它意味着让 `backend = "s3"` 在配置加载阶段就失败，那是一次不兼容收窄，
要它自己的一刀，并且要面对 `config.production.toml` 里今天写了什么。

## 10. 明确不做

- **不写任何 S3 适配器代码**；
- **不写 JWKS 客户端、不引入 OIDC 或 mTLS、不动 `apps/api/identity.py` 的
  `HeaderPrincipalResolver`**；
- **不改 `tests/architecture/test_identity_boundary.py:27-40` 的
  `IDENTITY_BOUNDARY_MODULES` 白名单**（今天是 5 个模块）；
- **不把 loopback 强制从无条件改成有条件**（`settings.py:167-181`）；
- **不做 ACL 反查**（`document_versions.artifact_id` 索引）——
  `tests/api/test_upload_authorization.py:397` 的
  `test_a_read_grant_does_not_yet_reach_the_bytes` 已经把这个缺口钉住了，本条不
  动它；
- **不给 `artifacts` 表加 owner 列**；
- **不服务端铸造 `document_id`**；
- **不订正 ADR-012**（只记下它按自身条款该被取代，§6.1）；
- **不修 `ownership.yaml` 的两处失真**（只点名，§6.2）；
- **不加任何数据库迁移**。

## 11. 待定：必须由用户拍板，本 ADR 不给答案

以下问题原样记在这里。它们不是实现细节，**本 ADR 不替任何一个作答**。

1. **remote 到底要不要存在。**打算让这套东西真的跑在多主机上吗？没有 remote
   部署，S3 与生产身份都没有消费者——这是本条判「明确不做」的**全部依据**
   （§1.3）。如果答案是「今年之内会有」，那本 ADR 应该改写成一份**带时间条件**的
   ADR，而不是纯粹的不做。
2. **`scopes` 的权威来源是 token claim 还是服务端授权表？**一旦写进 token，
   **签发者就是发权限的人**。今天 scopes 自述且已经是授权门（§6.1）。
3. **`tenant` 从 claim 来，还是用 subject 查服务端成员表？**这决定整套租户隔离
   叙事**能不能被证明**——今天 tenant 也是自述的。
4. **一个跑了 20 分钟的 Task，提交者 token 已过期，还能不能继续？**今天由不可变
   提交快照（`apps/task_worker/identity.py` 的 `restore_submitted_principal`）
   给出「能」。这个答案**不能被「每次工具调用重验 token」悄悄推翻**——那会把
   「提交时授权 + 执行时重算 ACL」的两段式变成三段式。

**顺带说一句（不是待定项，是勘察结论）**：OIDC vs mTLS 这个问题其实基本已定，
不需要拍板。mTLS 认证的是**客户端进程**而不是人，给不出
`principal_id` / `tenant_id` / `scopes`，浏览器控制台也基本用不了；而
`apps/api/web.py` 的模块 docstring 已经把话说死——控制台同源挂在 `/ui` 就是为了
不引入 CORS，SSE 用 `fetch` 手工解帧而不是 `EventSource`，正因为 `EventSource`
带不了身份 header。cookie session 会把 CSRF 重新拉进来并推翻那个已经做过的判断。
**所以真候选是「带 token 的 header」，真正的问题是上面第 2、3、4 条。**

## 12. 什么会让这条决定重来

**§4 的三条重开条件同时成立。**缺一不可，尤其是第一条：仓库里真的出现一个 remote
部署。

**`LocalArtifactStore` 之外出现第二个进程边界。**今天四个进程共用一个 docker
volume（§1.3）。任何让它们不再共享文件系统的改动——不只是 S3——都会立刻把
「本地存储够用」这个前提推翻。

**有人要把 §9.2 的两种形状统一掉。**那一刀会让 `backend = "s3"` 在配置加载阶段
失败，是一次不兼容收窄（对照 ADR-039 §5：`1.13` → `1.14` 就是为这类收窄抬的
版）。它会连带决定 `config.production.toml` 该怎么写，届时本条 §8「零配置影响」
的结论不再适用。
