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

export function SystemPage() {
  const { setEditorOpen } = useIdentity();
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
    <main className="aw-utility-page">
      <header className="aw-page-header">
        <div>
          <span className="aw-eyebrow">本地运行环境</span>
          <h1>运行状态</h1>
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
          数据库已就绪不代表模型、Qdrant、Task Worker 或文档处理 Worker 都正常；
          现有公开接口无法验证这些状态。
        </span>
      </div>

      {health.data !== undefined && (
        <p className="aw-page-note">
          最近检查：{formatDateTime(health.data.checkedAt)}（每 15 秒自动更新）
        </p>
      )}

      {/* One button, not a fold.

          What was here was a `<details>` holding two sections, and both were
          duplicates: the identity block restates `EnvironmentDialog`, which the
          button below opens, and "why are some states unknown" restated in three
          bullets the warning directly above it. A page whose live content is two
          booleans should not be mostly prose about the two booleans. */}
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
