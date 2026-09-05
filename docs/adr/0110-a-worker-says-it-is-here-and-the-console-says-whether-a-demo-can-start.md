# ADR-0110：Worker 自己登记在不在，控制台据此说「可以开始演示」

- 状态：Accepted
- 日期：2026-09-05
- 关联：ADR-0102（一台部署要说得出自己没装配起什么——它把 `task.worker` 那一行留成
  `unknown`，本 ADR 把那个 unknown 变成两行事实）、ADR-0041（迟到的心跳不得续租——
  本 ADR 的过期判定用同一个钟、同一个「三次」）、ADR-0044（只绑 loopback、只信请求头
  ——登记表里没有地址，读它的路由照旧过身份适配器）、评审 2026-09-04 D 项

## 1. 背景

运行状态页最上面三格：API 可响应、数据库已就绪、任务与文档 Worker **状态未知**。第三格
从写下那天起就是未知，而且是诚实的未知：`task_runs.heartbeat_at` 只在一个任务被领走之后
才跳，一个空闲的 Task Worker 和一个挂掉的 Task Worker 从 API 这一侧看完全一样；摄取
Worker 的租约挂在 outbox token 上，哪张表里都没有它自己。于是能力清单（ADR-0102）里
`task.worker` 那一行只能答 `unknown`，并写明「从这里看不出来，去看进程」。

对演示这是最坏的失败时刻：API 绿、数据库绿、提交成功，任务停在「排队中」不动——页面上
没有一处说 Worker 根本没起。2026-09-04 的外部评审把它列为 D 项：「不能把 API 能响应等同于
任务可执行」，并给了边界：若加 Worker 登记，要带新鲜度/TTL、部署标识和能力快照，超时显示
离线或未知；先限定为现有本地部署，不扩成监控平台。

## 2. 决定

**每个 Worker 进程自己登记。** 一张表 `worker_presence`，一行一个进程：`worker_id`、
`kind`（task / ingestion）、`deployment`（装配它的 profile 名，如 `demo-local`）、
`capabilities`（装配时的能力快照，JSONB）、`started_at`、`heartbeat_at`、`expires_at`。
进程起来先写一行再开始领活，此后每个心跳间隔 upsert 一次；有序退出删行。

**过期由读侧按数据库时钟判。** 写侧只说「我的话到 `expires_at` 为止有效」，读侧拿
`now()` 比——和任务租约用的是同一个钟（`coordination.lease_time_source = postgresql_clock`），
一台机器时钟不准，它的登记和它的租约被同一把尺量。TTL 是心跳间隔的三倍，和租约允许迟到的
「三次」同一条规矩，不另开旋钮。

**过期的行留着，不删。** 「10:42 之后没再登记」比「没有这一行」有用：控制台把它画成失联并
说出最近一次心跳在几秒前；有序退出删掉的行画成「没有登记」。一次崩溃和一次干净停止在页面
上长得不一样。

**登记是读数，不是第二个活性来源。** 运行路径一概不读这张表：没登记的 Worker 照样领活，
过期的行拦不住任何人，租约与 epoch 栅栏一字不动。写不进去（迁移没打、库暂时不通）只记一条
警告、下一拍再试，绝不让 Worker 停。这条窄度是有意的：一旦有第二个地方能回答「谁活着」，
就会有第二个答案。

**API 只读它。** `GET /v1/system/workers` 返回每一行加上 `fresh` 与
`seconds_since_heartbeat`——两个都在服务端按同一个 `now()` 算好，浏览器的钟是第三个钟，
控制台不拿它减。没有登记表的 API（早于本 ADR 的进程，或手工拼的测试容器）答
`available: false`，控制台画回原来那格「状态未知」，而不是「没有 Worker」。

**控制台据此下一个判断。** 运行状态页顶多一块「演示前自检」：API、数据库、任务 Worker、
模型四项必须，文档 Worker 与知识库问答只降级（缺了资料问答那条演示降级，另外两条不受影响）。
每一条缺失带着补法，补法要么来自能力清单自己的 `remedy`，要么是起那个进程的那条命令。
任务 Worker 是 `--demo` 起的合成 Worker 时，结论把这一点说出来。

## 3. 不做的

- **不做 Worker 注册中心。** 没有服务发现、没有把任务路由给某个 Worker、没有从登记里读
  能力去决定授权——授权信封仍在提交时冻结（ADR-0102 说的「信封，不是猜测」没有变）。
- **不监控 Qdrant。** 它没有登记，也不在 `/health/ready` 里；页面上那句「Qdrant 仍然从这里
  看不出来」留着，并指向文档 Worker 那一格（上传停在处理中时先看它）。
- **不做跨机器。** 登记写进这台部署自己的 PostgreSQL，`deployment` 是 profile 名不是主机名；
  两台机器各有各的库，互相看不见——评审说的「先限定为现有本地部署」。
- **不把登记接进 Compose 的健康检查。** 容器的 healthcheck 仍是各自进程的；这张表是给人看的。

## 4. 证据

- 契约：`tests/contracts/test_worker_presence.py`，内存与 PostgreSQL 各 4 条——登记即可见且
  新鲜、第二拍替换而不是新增、过期的行仍列出但读作失联、`forget` 删行。
- 路由：`tests/api/test_system_workers_api.py` 2 条——没有存储答 `available: false` 而不是空
  名单；新鲜与失联按存储的钟分得开。
- 心跳：`tests/application/test_worker_presence_beacon.py` 3 条——先登记再开始、按间隔续拍、
  退出即删；一个写不进去的存储只换来两条警告，进程照跑。
- 控制台：`SystemPage.test.tsx` 新增 3 条——在线的说合成与否、失联的说最近一次心跳；没有
  任务 Worker 时自检说还不能开始并给出命令；API 没有登记表时不下结论。
- 迁移：`migrations/versions/0033_worker_presence.py`。
