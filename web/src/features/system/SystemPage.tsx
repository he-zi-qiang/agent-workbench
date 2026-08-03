import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Braces,
  Database,
  KeyRound,
  Radio,
  RefreshCw,
  Settings2,
} from "lucide-react";
import type { ReactNode } from "react";
import { checkHealth } from "../../api/client";
import { useIdentity } from "../../app/IdentityContext";
import { ErrorNotice, LoadingLine } from "../../components/ui";

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
          <span className="aw-eyebrow">Runtime surface</span>
          <h1>系统</h1>
          <p>只展示可以从公开接口确认的状态，以及本地演示环境的装配边界。</p>
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

      {health.isPending && <LoadingLine label="正在检查 API" />}
      {health.error !== null && <ErrorNotice message={errorMessage(health.error)} />}

      <section className="aw-metric-grid" aria-label="服务状态">
        <HealthMetric
          detail="/health/live 只证明 API 进程能够回应。"
          icon={<Activity aria-hidden="true" size={18} />}
          label="Process"
          ok={health.data?.live.ok}
          value={health.data?.live.status ?? "检查中"}
        />
        <HealthMetric
          detail="/health/ready 是后端定义的有界依赖检查。"
          icon={<Database aria-hidden="true" size={18} />}
          label="Dependencies"
          ok={health.data?.ready.ok}
          value={health.data?.ready.status ?? "检查中"}
        />
        <div className="aw-metric">
          <KeyRound aria-hidden="true" size={18} />
          <span>Identity adapter</span>
          <strong>开发 Header</strong>
          <small>仅适用于 loopback 演示，不是生产认证。</small>
        </div>
      </section>

      <div className="aw-card-grid">
        <section className="aw-card aw-section" aria-labelledby="identity-title">
          <div className="aw-card-header">
            <div>
              <span className="aw-eyebrow">Development identity</span>
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
            <span>
              浏览器发送的身份字段会由服务端重新用于对象级授权。任何人都不应把这个开发适配器当作登录系统。
            </span>
          </div>
          <button
            className="aw-button is-ghost"
            onClick={() => setEditorOpen(true)}
            type="button"
          >
            编辑本地身份
          </button>
        </section>

        <section className="aw-card aw-section" aria-labelledby="capability-title">
          <div className="aw-card-header">
            <div>
              <span className="aw-eyebrow">Capability contract</span>
              <h2 id="capability-title">界面与后端边界</h2>
            </div>
            <Braces aria-hidden="true" size={20} />
          </div>
          <ul className="aw-capability-list">
            <li>
              <Radio aria-hidden="true" size={16} />
              <div>
                <strong>Chat events</strong>
                <span>使用可附加身份 Header 的 fetch SSE，并通过 Last-Event-ID 恢复。</span>
              </div>
            </li>
            <li>
              <RefreshCw aria-hidden="true" size={16} />
              <div>
                <strong>Work timeline</strong>
                <span>当前 Task 没有 SSE；界面按服务端 cursor 轮询，不伪装实时推送。</span>
              </div>
            </li>
            <li>
              <Database aria-hidden="true" size={16} />
              <div>
                <strong>Optional assembly</strong>
                <span>
                  health ready 不代表 Chat 或 Search 一定已挂载；它们取决于模型和检索栈装配。
                </span>
              </div>
            </li>
          </ul>
          <p className="aw-page-note">
            当前没有公开的 capabilities、配置快照或 Trace 查询接口，因此本页不猜测模型、索引和 Worker 状态，也不读取 Secret。
          </p>
        </section>
      </div>
    </main>
  );
}

function HealthMetric({
  icon,
  label,
  value,
  ok,
  detail,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  ok: boolean | undefined;
  detail: string;
}) {
  return (
    <div className="aw-metric">
      {icon}
      <span>{label}</span>
      <strong>
        {value}
        <i
          aria-label={ok === undefined ? "检查中" : ok ? "正常" : "异常"}
          className={`aw-health-dot ${ok === undefined ? "" : ok ? "is-success" : "is-danger"}`}
        />
      </strong>
      <small>{detail}</small>
    </div>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "健康检查失败。";
}
