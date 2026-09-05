import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  CheckCircle2,
  CircleDashed,
  CircleHelp,
  Database,
  RefreshCw,
  XCircle,
} from "lucide-react";
import type { ReactNode } from "react";
import {
  ApiError,
  checkHealth,
  getDeploymentCapabilities,
  getSystemWorkers,
  setDeploymentSwitch,
} from "../../api/client";
import type {
  DeploymentCapabilitiesResponse,
  DeploymentCapability,
  DeploymentSwitch,
  WorkerPresenceView,
  WorkersResponse,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  ErrorNotice,
  LoadingLine,
  formatDateTime,
} from "../../components/ui";

/**
 * 健康检查那几格，抽出来给设置面板里那一格用。
 *
 * 抽的是「此刻这几个进程还在不在」，**不含**这一页那三行只读的身份事实：在设置
 * 面板里，身份是隔壁一整类，把它在这里再复述一遍，等于让同一个事实在一个框里出
 * 现两次、而其中一次还是只读的。
 */
export function HealthReport({ heading }: { heading?: ReactNode }) {
  const queryClient = useQueryClient();
  const { identity } = useIdentity();
  // Worker 自己每 20 秒登记一次（ADR-0110）；这里 15 秒问一次，和健康检查同一节奏。
  // 查不到不重试：一个没有登记表的旧 API 答 404，重试三次也还是 404。
  const workers = useQuery({
    queryKey: ["system-workers", identity.tenantId, identity.principalId],
    queryFn: () => getSystemWorkers(identity),
    refetchInterval: 15_000,
    retry: false,
  });
  // 和 `CapabilityReport` 同一个 key、同一份选项，所以是同一份缓存：自检要
  // 读它，但不该为此多发一次请求。
  const capabilities = useQuery({
    queryKey: ["deployment-capabilities", identity.tenantId, identity.principalId],
    queryFn: () => getDeploymentCapabilities(identity),
    staleTime: Infinity,
    refetchOnWindowFocus: "always",
  });
  const health = useQuery({
    queryKey: ["system-health"],
    queryFn: async () => {
      const [live, ready] = await Promise.all([
        checkHealth("/health/live"),
        checkHealth("/health/ready"),
      ]);
      return { live, ready, checkedAt: new Date().toISOString() };
    },
    refetchInterval: 15_000,
  });

  return (
    <>
      <header className="aw-page-header">
        <div>
          {heading}
          <p>这里只回答公开接口能够确认的事情；无法确认的组件会明确显示为“未知”。</p>
        </div>
        <button
          className="aw-button is-ghost"
          disabled={health.isFetching}
          onClick={() => {
            void health.refetch();
            // 能力清单也重读：一个刚重启过 API 的人按的就是这个按钮，而那份清单
            // 除此之外只在回到标签页时才刷新（见 CapabilityReport）。
            void queryClient.invalidateQueries({
              queryKey: ["deployment-capabilities"],
            });
            void workers.refetch();
          }}
          type="button"
        >
          <RefreshCw aria-hidden="true" size={15} />
          重新检查
        </button>
      </header>

      <DemoReadiness
        capabilities={capabilities.data?.capabilities ?? null}
        live={health.data?.live.ok}
        ready={health.data?.ready.ok}
        workers={workers.data ?? null}
        workersError={workers.isError}
      />

      {health.isPending && <LoadingLine label="正在检查服务" />}
      {health.error !== null && <ErrorNotice message={errorMessage(health.error)} />}

      <section className="aw-metric-grid" aria-label="服务状态">
        <HealthMetric
          detail="来自 /health/live，只说明 API 进程能够回应请求。"
          icon={<Activity aria-hidden="true" size={18} />}
          label="API 服务"
          ok={health.data?.live.ok}
          unavailable={health.isError}
          value={healthValue(
            health.data?.live.ok,
            "可响应",
            "不可响应",
            health.isError,
          )}
        />
        <HealthMetric
          detail="来自 /health/ready；当前后端只在这里检查 PostgreSQL。"
          icon={<Database aria-hidden="true" size={18} />}
          label="数据库"
          ok={health.data?.ready.ok}
          unavailable={health.isError}
          value={healthValue(
            health.data?.ready.ok,
            "已就绪",
            "未就绪",
            health.isError,
          )}
        />
        <WorkerMetrics error={workers.isError} report={workers.data} />
      </section>

      {/* 这段话此前说「Worker 在另一个进程里，它是不是 --demo 合成 Worker，从这里
          看不出来」。**Worker 那半句从 ADR-0110 起不成立**：进程自己每 20 秒登记
          一次，上面那两格读的就是登记，合成与否也写在登记里。还看不出来的只剩
          Qdrant——它没有登记，也不在 /health/ready 里。压成一句，按评审第 2 条
          「把诊断说明压成一句，并给相应操作」。 */}
      <div className="aw-notice is-warning">
        <CircleHelp aria-hidden="true" size={16} />
        <span>
          数据库已就绪不代表 Qdrant 与两个 Worker 都正常：Worker 现在自己登记（上面两格），Qdrant 仍然从这里看不出来——上传后停在「处理中」时先看文档 Worker 那一格。
        </span>
      </div>

      <CapabilityReport />

      {health.data !== undefined && (
        <p className="aw-page-note">
          最近检查：{formatDateTime(health.data.checkedAt)}（每 15 秒自动更新）
        </p>
      )}

    </>
  );
}

export function SystemPage() {
  const { identity, setEditorOpen } = useIdentity();

  return (
    <main className="aw-utility-page">
      <HealthReport
        heading={
          <>
            <span className="aw-eyebrow">本地运行环境</span>
            <h1>运行状态</h1>
          </>
        }
      />

      {/* 身份回到了这一页，但只作为事实，不作为表单。
       *
       * 它此前被删过，理由是「复述那个身份编辑框」——那条理由对的是当时那个
       * 版本：一个装在 `<details>` 里、带编辑入口的块，确实是把下面那个按钮又画
       * 了一遍。
       *
       * 现在这三行是只读的。这一页回答的问题是「此刻什么是真的」，而"这些数字是
       * 以谁的身份取到的"正是其中一条：同一个 /health/ready 对任何身份都一样，
       * 但下面那些页面的内容不是。看见和修改是两件事，修改仍然只有一个入口，就
       * 在这三行下面。 */}
      {/* dl，不是 section：dt/dd 只有在定义列表里才是合法的，而这三行正是
          「名字 → 值」。 */}
      <dl className="aw-identity-facts" aria-label="当前本地身份">
        <div>
          <dt>tenant</dt>
          <dd>{identity.tenantId}</dd>
        </div>
        <div>
          <dt>principal</dt>
          <dd>{identity.principalId}</dd>
        </div>
        <div>
          <dt>scopes</dt>
          <dd>
            {identity.scopes.length === 0 ? (
              // 空不是「全部」，也不是「没设」。写成 — 会让读者自己去猜是哪一种。
              <span className="aw-identity-empty">没有任何 scope</span>
            ) : (
              [...identity.scopes].sort().join(" · ")
            )}
          </dd>
        </div>
      </dl>

      <button
        className="aw-button is-ghost"
        onClick={() => {
          setEditorOpen(true);
        }}
        type="button"
      >
        编辑本地身份
      </button>
    </main>
  );
}


/**
 * 「这套部署能做什么」，以及每一处缺失的原因与补法（ADR-102）。
 *
 * 这块是被一次真实的误判逼出来的：Docker 默认栈起来以后，控制台里 Chat 能答、
 * 任务能提交、页面一页不少，而外部搜索、Word/web MCP、沙箱一件都不在——因为进程
 * 启动时就没装配它们。从界面上看不出来任何一处，于是人会去查 key、查网络、查
 * 模型，唯独查不到「这个进程从一开始就没有这件工具」。
 *
 * **不轮询，但回到这个标签页时重读一次。** 上面那几格问的是「此刻还在不在」，会变，
 * 所以 15 秒一次；这一份问的是「启动那一刻装配成了什么」，只有重启才会变，轮询它只是
 * 在重复同一个答案。而重启恰恰是这一页现在会让人去做的事（ADR-103）：拨了开关、去
 * 终端里 `stack.cmd restart`、回到这个标签页——回来时看到的必须是新进程的答案，不是
 * 走之前那份。`staleTime: Infinity` 曾经把 `refetchOnWindowFocus` 一起挡掉了，实测
 * 就是这样发现的：同一个标签页里 API 已经带着联网搜索起来了，页面还写着「这次启动：关」。
 * 「重新检查」那个按钮同理，也重读这一份。
 */
export function CapabilityReport() {
  const { identity } = useIdentity();
  const report = useQuery({
    queryKey: ["deployment-capabilities", identity.tenantId, identity.principalId],
    queryFn: () => getDeploymentCapabilities(identity),
    staleTime: Infinity,
    refetchOnWindowFocus: "always",
  });

  const rows = report.data?.capabilities ?? [];
  const core = rows.filter((row) => row.tier === "core");
  const optional = rows.filter((row) => row.tier === "optional");

  return (
    <section className="aw-card aw-section" aria-labelledby="capabilities-title">
      <div className="aw-card-header">
        <div>
          <span className="aw-eyebrow">这套部署</span>
          <h2 id="capabilities-title">能做什么，缺什么</h2>
        </div>
      </div>
      {/* 一行写完，不折行：JSX 会把换行折成一个空格，中文里那个空格是可见的
          （`src/test/jsxChineseWrap.test.ts` 守着这条）。 */}
      <p className="aw-page-note">
        核心是这个产品自称是什么，附加是它还能被要求做什么。每一条缺失都写明它是怎么来的、以及补上它要动什么——这些答案来自进程启动时的那份装配，不是这个页面的推测。
      </p>

      {report.isPending && <LoadingLine label="正在读取能力清单" />}
      {report.error !== null && report.error !== undefined && (
        <ErrorNotice message={capabilityErrorMessage(report.error)} />
      )}

      {rows.length > 0 && (
        <>
          <h3 className="aw-eyebrow">核心</h3>
          <ul className="aw-capability-list">
            {core.map((row) => (
              <CapabilityRow key={row.id} row={row} />
            ))}
          </ul>
          <h3 className="aw-eyebrow">附加</h3>
          <ul className="aw-capability-list">
            {optional.map((row) => (
              <CapabilityRow key={row.id} row={row} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function CapabilityRow({ row }: { row: DeploymentCapability }) {
  // 开关是这一行的第三列，不是文字下面的又一段。此前它排在原因和补法之后，
  // 于是五个带开关的行各自高出一截，一屏里同一组三个按钮出现五次，读者要
  // 用眼睛把每组按钮和它上面第几行标题配对。放到右边之后，标题、状态和
  // 开关在同一条水平线上，往下扫一列就知道哪几行是能拨的。窄屏上三列排不下，
  // 它回到文字下面（app.css 里那条断点）。
  return (
    <li className={`is-${row.state} is-${row.tier}`}>
      {stateIcon(row)}
      <div>
        <strong>
          {row.title}
          <em className="aw-capability-state">{stateLabel(row.state)}</em>
          {/* 「需要安装」和「需要模型密钥」是零件的来路，不是状态：一个可用的零件
              也仍然是装出来的。只在缺失时标，可用时这行字只会让人去找不存在的活。 */}
          {row.state === "absent" && row.provision === "install" && (
            <em className="aw-capability-state is-install">需要安装</em>
          )}
          {row.state === "absent" && row.provision === "key" && (
            <em className="aw-capability-state is-install">需要模型密钥</em>
          )}
        </strong>
        {row.reason !== "" && <span>{row.reason}</span>}
        {row.remedy !== "" && <span>要补上它：{row.remedy}</span>}
        {row.detail.length > 0 && <span>{row.detail.join(" · ")}</span>}
      </div>
      {row.switch !== null && <SwitchControl row={row} control={row.switch} />}
    </li>
  );
}

/**
 * 一个零件的开关（ADR-103）。
 *
 * **拨的是下次启动，不是这个进程。** 这个进程在组装时读了一次配置，之后什么也不会
 * 变，所以这里没有「已开启」这种词——有的是「这次启动」和「下次启动」两个答案，以及
 * 一条说明它们何时会一样的话。把两者并成一个绿点，正是一个设置页会声称刚拨的开关
 * 已经生效、而用户回头发现什么也没变的那条路。
 *
 * **三个位置，不是两个。** 「不指定」是一个真实的状态：下次启动照环境变量和配置文件
 * 走——在两条启动路径上（Compose 的容器启动脚本，以及 ADR-104 之后的 `dev.sh demo-api`）
 * 那都意味着「有 key 就开」的同一个探针继续决定联网搜索。把它画成「关」，等于让页面替
 * 启动脚本作了它没作的决定。
 *
 * **服务端回的是整份清单，这里就整份替换。** 一个开关可能管两行（联网搜索管对话和
 * 任务两行），自己只改一行的 cache 会让另一行说谎。
 */
function SwitchControl({
  row,
  control,
}: {
  row: DeploymentCapability;
  control: DeploymentSwitch;
}) {
  const { identity } = useIdentity();
  const queryClient = useQueryClient();
  const flip = useMutation({
    mutationFn: (enabled: boolean | null) =>
      setDeploymentSwitch(identity, control.id, enabled),
    onSuccess: (report: DeploymentCapabilitiesResponse) => {
      queryClient.setQueryData(
        ["deployment-capabilities", identity.tenantId, identity.principalId],
        report,
      );
    },
  });
  const nextStart =
    control.stored === null ? "按启动环境与配置" : control.stored ? "开" : "关";

  return (
    <div className="aw-capability-switch">
      <div
        className="aw-settings-choices"
        role="radiogroup"
        aria-label={`${row.title}：下次启动`}
      >
        <button
          aria-checked={control.stored === true}
          className={`aw-settings-choice${control.stored === true ? " is-on" : ""}`}
          disabled={flip.isPending}
          onClick={() => flip.mutate(true)}
          role="radio"
          type="button"
        >
          打开
        </button>
        <button
          aria-checked={control.stored === false}
          className={`aw-settings-choice${control.stored === false ? " is-on" : ""}`}
          disabled={flip.isPending}
          onClick={() => flip.mutate(false)}
          role="radio"
          type="button"
        >
          关闭
        </button>
        <button
          aria-checked={control.stored === null}
          className={`aw-settings-choice${control.stored === null ? " is-on" : ""}`}
          disabled={flip.isPending}
          onClick={() => flip.mutate(null)}
          role="radio"
          type="button"
        >
          不指定
        </button>
      </div>
      <small>
        这次启动：{control.active ? "开" : "关"} · 下次启动：{nextStart}
      </small>
      {control.restart_required && (
        <small className="is-warning">{control.restart_hint}</small>
      )}
      {control.held !== "" && <small className="is-warning">{control.held}</small>}
      {control.overridden && (
        <small className="is-warning">启动环境里显式给了这个值，压过了这里的选择；改这里不会生效，先去掉那个环境变量。</small>
      )}
      {control.blocked !== "" && !control.overridden && (
        <small>{control.blocked}</small>
      )}
      {flip.isError && (
        <ErrorNotice message={errorMessage(flip.error)} />
      )}
    </div>
  );
}

/**
 * 四个图标，三个状态——多出来的那一个是「核心缺失」。
 *
 * `unknown` 有自己的图标而不是画成灰色的缺失：它说的是「这个进程看不见那个进程」，
 * 把它画成缺失，就是替另一个进程作了它没作过的证。而核心缺失与附加缺失分成两个
 * 图标（配色也不同），是因为附加项缺席是一个选择、核心项缺席是半个产品不在——
 * 一页把两者画成同一个红点，就又变回了一份要人挨个去查的清单。
 */
function stateIcon(row: DeploymentCapability) {
  // 返回元素而不是组件：`react-hooks/static-components` 拦的是把组件赋给一个
  // 每次渲染都会重新算出来的变量。
  if (row.state === "available") return <CheckCircle2 aria-hidden="true" size={16} />;
  if (row.state === "unknown") return <CircleHelp aria-hidden="true" size={16} />;
  if (row.tier === "core") return <XCircle aria-hidden="true" size={16} />;
  return <CircleDashed aria-hidden="true" size={16} />;
}

function stateLabel(state: DeploymentCapability["state"]): string {
  if (state === "available") return "可用";
  if (state === "unknown") return "未知";
  return "缺失";
}

function capabilityErrorMessage(error: unknown): string {
  // 404 有它自己的意思，而且是一个人真的会遇到的意思：这个 API 比控制台旧，旧到
  // 还没有这条路由。说成「读取失败」会让人去查网络。
  if (error instanceof ApiError && error.status === 404) {
    return "这个 API 进程还没有能力清单接口（/v1/system/capabilities），它比当前控制台旧。";
  }
  return error instanceof Error ? error.message : "读取能力清单失败。";
}

function HealthMetric({
  icon,
  label,
  value,
  ok,
  unavailable,
  detail,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  ok: boolean | undefined;
  unavailable: boolean;
  detail: string;
}) {
  return (
    <div className="aw-metric">
      {icon}
      <span>{label}</span>
      <strong>
        {value}
        <i
          aria-label={
            unavailable
              ? "无法确认"
              : ok === undefined
                ? "尚未检查"
                : ok
                  ? "正常"
                  : "异常"
          }
          className={`aw-health-dot ${ok === undefined ? "" : ok ? "is-success" : "is-danger"}`}
        />
      </strong>
      <small>{detail}</small>
    </div>
  );
}

/**
 * 两格：任务 Worker、文档 Worker（ADR-0110）。
 *
 * 三种答案要长得不一样：**在线**（有一行还没过期）、**失联**（有行、都过期了——
 * 「最近一次心跳在 N 秒前」比一个红点有用）、**没有登记**（一行都没有：从没起过，
 * 或者起在一个没打 0033 迁移的库上）。API 没有登记表时退回上一版那一格「状态未知」
 * ——那是「这个 API 看不见」，不是「没有 Worker」。
 */
function WorkerMetrics({
  error,
  report,
}: {
  error: boolean;
  report: WorkersResponse | undefined;
}) {
  if (error || report === undefined || !report.available) {
    return (
      <UnknownMetric
        detail={
          error
            ? "读不到 Worker 登记；API 没在跑或比控制台旧。"
            : report === undefined
              ? "正在读取 Worker 登记。"
              : "这个 API 没有 Worker 登记表（早于 ADR-0110）。"
        }
        icon={<CircleHelp aria-hidden="true" size={18} />}
        label="任务与文档 Worker"
      />
    );
  }
  return (
    <>
      <WorkerMetric
        icon={<CircleDashed aria-hidden="true" size={18} />}
        kind="task"
        label="任务 Worker 进程"
        rows={report.workers}
      />
      <WorkerMetric
        icon={<CircleDashed aria-hidden="true" size={18} />}
        kind="ingestion"
        label="文档 Worker 进程"
        rows={report.workers}
      />
    </>
  );
}

function WorkerMetric({
  icon,
  kind,
  label,
  rows,
}: {
  icon: ReactNode;
  kind: WorkerPresenceView["kind"];
  label: string;
  rows: WorkerPresenceView[];
}) {
  const mine = rows.filter((row) => row.kind === kind);
  const fresh = mine.filter((row) => row.fresh);
  const demo = fresh.some((row) => row.capabilities["demo"] === true);
  const latest = mine.reduce<WorkerPresenceView | null>(
    (held, row) =>
      held === null || row.seconds_since_heartbeat < held.seconds_since_heartbeat
        ? row
        : held,
    null,
  );
  const value =
    fresh.length > 0
      ? `在线${fresh.length > 1 ? ` · ${String(fresh.length)} 个` : ""}${demo ? " · 合成演示" : ""}`
      : latest !== null
        ? "失联"
        : "没有登记";
  const detail =
    fresh.length > 0
      ? `${fresh.map((row) => row.deployment).join("、")} 装配；${demo ? "--demo 起的，不会调用真实模型。" : "每 20 秒登记一次。"}`
      : latest !== null
        ? `最近一次心跳在 ${String(Math.round(latest.seconds_since_heartbeat))} 秒前，之后没有再登记。`
        : kind === "task"
          ? "从没有 Worker 登记过；起它：scripts/dev.sh demo-worker。"
          : "从没有 Worker 登记过；起它：scripts/dev.sh ingest。";
  return (
    <HealthMetric
      detail={detail}
      icon={icon}
      label={label}
      ok={fresh.length > 0}
      unavailable={false}
      value={value}
    />
  );
}

/**
 * 演示前自检（2026-09-04 评审 D 项）。
 *
 * 一个「可以开始演示」的判断，而不是让人自己把五格状态和一份清单在脑子里合起来。
 * 必须项：API、数据库、任务 Worker、模型（`chat.direct`）。文档 Worker 与知识库问答
 * 是「资料问答」那条演示才需要的，缺了只降级、不拦。每一条缺失都带着补法，补法
 * 来自能力清单自己的 remedy 或登记格里那条命令。
 *
 * **API 能响应不等于任务可执行**——这正是这块存在的理由：此前这一页最上面的绿灯
 * 只说 API，而任务停在排队里的时候 API 一直是绿的。
 */
function DemoReadiness({
  capabilities,
  live,
  ready,
  workers,
  workersError,
}: {
  capabilities: DeploymentCapability[] | null;
  live: boolean | undefined;
  ready: boolean | undefined;
  workers: WorkersResponse | null;
  workersError: boolean;
}) {
  const row = (id: string) =>
    capabilities?.find((held) => held.id === id) ?? null;
  const freshOf = (kind: WorkerPresenceView["kind"]) =>
    workers?.workers.filter((held) => held.kind === kind && held.fresh) ?? [];
  const taskWorkers = freshOf("task");
  const ingestWorkers = freshOf("ingestion");
  const workersKnown = workers !== null && workers.available && !workersError;
  const direct = row("chat.direct");
  const rag = row("chat.knowledge_base");
  const checks: {
    key: string;
    label: string;
    state: "ok" | "missing" | "unknown";
    required: boolean;
    hint: string;
  }[] = [
    {
      key: "api",
      label: "API 可响应",
      state: live === undefined ? "unknown" : live ? "ok" : "missing",
      required: true,
      hint: "起它：scripts/dev.sh demo-api。",
    },
    {
      key: "db",
      label: "数据库就绪",
      state: ready === undefined ? "unknown" : ready ? "ok" : "missing",
      required: true,
      hint: "起服务并迁移：scripts/dev.sh services，再 scripts/dev.sh migrate。",
    },
    {
      key: "task-worker",
      label: "任务 Worker 在线",
      state: !workersKnown
        ? "unknown"
        : taskWorkers.length > 0
          ? "ok"
          : "missing",
      required: true,
      hint: workersKnown
        ? "起它：scripts/dev.sh demo-worker（Windows 容器栈由 stack.cmd 一起带起）。"
        : "这个 API 看不见 Worker 登记；只能去看进程。",
    },
    {
      key: "model",
      label: "模型可用",
      state:
        direct === null
          ? "unknown"
          : direct.state === "available"
            ? "ok"
            : "missing",
      required: true,
      hint:
        direct !== null && direct.remedy !== ""
          ? direct.remedy
          : "在设置的「模型密钥」里存一把 key，然后重启 API。",
    },
    {
      key: "ingest-worker",
      label: "文档 Worker 在线",
      state: !workersKnown
        ? "unknown"
        : ingestWorkers.length > 0
          ? "ok"
          : "missing",
      required: false,
      hint: "没有它，上传的资料会一直停在「处理中」；起它：scripts/dev.sh ingest。",
    },
    {
      key: "rag",
      // 不叫「知识库问答（RAG）」：那是下面能力清单里那一行的标题，两处同名会让
      // 按文字找那一行的人（和测试）先撞上这里。
      label: "知识库问答可用",
      state:
        rag === null ? "unknown" : rag.state === "available" ? "ok" : "missing",
      required: false,
      hint:
        rag !== null && rag.remedy !== "" ? rag.remedy : "资料问答那条演示需要它。",
    },
  ];
  const blocking = checks.filter(
    (check) => check.required && check.state === "missing",
  );
  const unknown = checks.filter(
    (check) => check.required && check.state === "unknown",
  );
  const degraded = checks.filter(
    (check) => !check.required && check.state !== "ok",
  );
  const synthetic = taskWorkers.some(
    (held) => held.capabilities["demo"] === true,
  );
  const verdict =
    blocking.length > 0
      ? `还不能开始演示：${String(blocking.length)} 项要先处理`
      : unknown.length > 0
        ? "还不能下结论：有一项答不出"
        : synthetic
          ? "可以开始演示，但任务由合成 Worker 执行，不调用真实模型"
          : "可以开始演示";
  const tone =
    blocking.length > 0
      ? "is-blocked"
      : unknown.length > 0
        ? "is-unknown"
        : "is-ready";
  return (
    <section
      aria-labelledby="aw-readiness-title"
      className={`aw-readiness ${tone}`}
    >
      <h2 id="aw-readiness-title">{verdict}</h2>
      <ul className="aw-readiness-list">
        {checks.map((check) => (
          <li
            className={`is-${check.state}${check.required ? "" : " is-optional"}`}
            key={check.key}
          >
            <strong>
              {check.label}
              {check.required ? null : <small>可降级</small>}
            </strong>
            <span>
              {check.state === "ok"
                ? "就绪"
                : check.state === "unknown"
                  ? "答不出"
                  : `缺：${check.hint}`}
            </span>
          </li>
        ))}
      </ul>
      {degraded.length > 0 && blocking.length === 0 ? (
        <p className="aw-readiness-note">
          {degraded.map((check) => check.label).join("、")}没就绪：资料问答那条演示会降级，其余两条不受影响。
        </p>
      ) : null}
    </section>
  );
}

function UnknownMetric({
  icon,
  label,
  detail,
}: {
  icon: ReactNode;
  label: string;
  detail: string;
}) {
  return (
    <div className="aw-metric">
      {icon}
      <span>{label}</span>
      <strong>状态未知</strong>
      <small>{detail}</small>
    </div>
  );
}

function healthValue(
  ok: boolean | undefined,
  healthy: string,
  unhealthy: string,
  unavailable: boolean,
): string {
  if (unavailable) return "无法确认";
  if (ok === undefined) return "检查中";
  return ok ? healthy : unhealthy;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "健康检查失败。";
}
