import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  CircleHelp,
  Database,
  RefreshCw,
} from "lucide-react";
import type { ReactNode } from "react";
import { checkHealth } from "../../api/client";
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
          onClick={() => void health.refetch()}
          type="button"
        >
          <RefreshCw aria-hidden="true" size={15} />
          重新检查
        </button>
      </header>

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
        <UnknownMetric
          detail="当前没有 Worker heartbeat 或状态查询 API。"
          icon={<CircleHelp aria-hidden="true" size={18} />}
          label="任务与文档 Worker"
        />
      </section>

      <div className="aw-notice is-warning">
        <CircleHelp aria-hidden="true" size={16} />
        <span>
          数据库已就绪不代表模型、Qdrant、Task Worker 或文档处理 Worker 都正常；现有公开接口无法验证这些状态。
        </span>
      </div>

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
