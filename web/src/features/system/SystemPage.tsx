import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ChevronDown,
  CircleHelp,
  Database,
  KeyRound,
  RefreshCw,
  Settings2,
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
  const { identity, setEditorOpen } = useIdentity();
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

      <details className="aw-card aw-section">
        <summary>
          <ChevronDown aria-hidden="true" size={14} />
          查看工程信息
        </summary>
        <div className="aw-card-grid">
          <section aria-labelledby="identity-title">
            <div className="aw-card-header">
              <div>
                <span className="aw-eyebrow">开发身份</span>
                <h2 id="identity-title">当前请求身份</h2>
              </div>
              <Settings2 aria-hidden="true" size={20} />
            </div>
            <dl className="aw-definition-list">
              <div>
                <dt>Tenant</dt>
                <dd><code>{identity.tenantId}</code></dd>
              </div>
              <div>
                <dt>Principal</dt>
                <dd><code>{identity.principalId}</code></dd>
              </div>
              <div>
                <dt>Scopes</dt>
                <dd>{identity.scopes.length > 0 ? identity.scopes.join(", ") : "无"}</dd>
              </div>
            </dl>
            <div className="aw-notice is-warning">
              <KeyRound aria-hidden="true" size={16} />
              <span>Header 身份只用于本机演示，不是生产登录系统。</span>
            </div>
            <button
              className="aw-button is-ghost"
              onClick={() => setEditorOpen(true)}
              type="button"
            >
              编辑本地身份
            </button>
          </section>

          <section aria-labelledby="boundary-title">
            <div className="aw-card-header">
              <div>
                <span className="aw-eyebrow">公开能力边界</span>
                <h2 id="boundary-title">为什么有些状态是未知？</h2>
              </div>
              <CircleHelp aria-hidden="true" size={20} />
            </div>
            <ul className="aw-capability-list">
              <li>
                <Activity aria-hidden="true" size={16} />
                <div>
                  <strong>Live 不是完整健康检查</strong>
                  <span>它只证明 API 进程还可以响应。</span>
                </div>
              </li>
              <li>
                <Database aria-hidden="true" size={16} />
                <div>
                  <strong>Ready 当前只检查数据库</strong>
                  <span>它没有查询模型、向量库或后台 Worker。</span>
                </div>
              </li>
              <li>
                <CircleHelp aria-hidden="true" size={16} />
                <div>
                  <strong>没有 capabilities 与 heartbeat API</strong>
                  <span>因此本页不会猜测 Chat、Search 或 Worker 是否已经装配。</span>
                </div>
              </li>
            </ul>
          </section>
        </div>
      </details>
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
