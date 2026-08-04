import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ArrowRight,
  Check,
  ChevronDown,
  FileCheck2,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  decideApproval,
  getArtifactJson,
  getTaskTimeline,
  listApprovals,
} from "../../api/client";
import type {
  ApprovalStatus,
  ApprovalView,
  PrincipalIdentity,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  EmptyState,
  ErrorNotice,
  IconButton,
  LoadingLine,
  StatusPill,
  formatDateTime,
} from "../../components/ui";
import {
  findTaskInputRef,
  parseTaskInputArtifact,
} from "../work/workTimeline";

type ApprovalFilter = "pending" | "handled";
type ApprovalDecision = "approved" | "rejected";

interface DecisionIntent {
  approval: ApprovalView;
  value: ApprovalDecision;
  objective: string | null;
}

const FILTERS: ReadonlyArray<{ value: ApprovalFilter; label: string }> = [
  { value: "pending", label: "待我确认" },
  { value: "handled", label: "已处理" },
];

export function ApprovalsPage() {
  const { identity } = useIdentity();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ApprovalFilter>("pending");
  const [decisionIntent, setDecisionIntent] = useState<DecisionIntent | null>(null);
  const scopeKey = [...identity.scopes].sort().join(",");

  const approvalsQuery = useInfiniteQuery({
    queryKey: [
      "approvals",
      identity.tenantId,
      identity.principalId,
      scopeKey,
      filter,
    ],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      listApprovals(identity, {
        statuses: approvalStatuses(filter),
        ...(pageParam === undefined ? {} : { cursor: pageParam }),
        limit: 25,
      }),
    getNextPageParam: (lastPage) => lastPage.cursor ?? undefined,
  });

  const decision = useMutation({
    mutationFn: ({ approval, value }: DecisionIntent) =>
      decideApproval(identity, approval, value),
    onSuccess: async () => {
      setDecisionIntent(null);
      await queryClient.invalidateQueries({ queryKey: ["approvals"] });
    },
  });

  const approvals =
    approvalsQuery.data?.pages.flatMap((page) => page.approvals) ?? [];
  const pending = filter === "pending";

  return (
    <main className="aw-utility-page">
      <header className="aw-page-header">
        <div>
          <span className="aw-eyebrow">需要你的决定</span>
          <h1>{pending ? "待我确认" : "已处理"}</h1>
          <p>
            {pending
              ? "任务报告已经走到导出前的最后一步，请确认是否允许继续。"
              : "这里保留你已经做出的导出决定，方便回到对应任务核对结果。"}
          </p>
        </div>
        <button
          className="aw-button is-ghost"
          disabled={approvalsQuery.isFetching}
          onClick={() => void approvalsQuery.refetch()}
          type="button"
        >
          <RefreshCw aria-hidden="true" size={15} />
          刷新
        </button>
      </header>

      <div className="aw-notice">
        <ShieldCheck aria-hidden="true" size={16} />
        <span>
          当前版本只有一种确认：<strong>允许生成并导出任务报告</strong>。批准会让任务继续导出；
          拒绝会结束任务且不导出报告。
        </span>
      </div>

      <div className="aw-toolbar">
        <div className="aw-segmented" aria-label="确认记录筛选">
          {FILTERS.map((item) => (
            <button
              aria-pressed={filter === item.value}
              className={filter === item.value ? "is-active" : ""}
              key={item.value}
              onClick={() => setFilter(item.value)}
              type="button"
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="aw-page-note">当前已加载 {approvals.length} 条</span>
      </div>

      {approvalsQuery.isPending && <LoadingLine label="正在读取确认记录" />}
      {approvalsQuery.error !== null && (
        <ErrorNotice message={errorMessage(approvalsQuery.error)} />
      )}

      {!approvalsQuery.isPending &&
        approvalsQuery.error === null &&
        approvals.length === 0 && (
          <EmptyState
            description={
              pending
                ? "现在没有需要你确认的任务。"
                : "还没有已经处理的确认记录。"
            }
            icon={<ShieldCheck aria-hidden="true" size={20} />}
            title={pending ? "全部处理完了" : "暂无处理记录"}
          />
        )}

      {approvals.length > 0 && (
        <section className="aw-list" aria-label={pending ? "待我确认" : "已处理"}>
          {approvals.map((approval) => (
            <ApprovalCard
              approval={approval}
              identity={identity}
              key={approval.approval_id}
              onDecide={(value, objective) => {
                decision.reset();
                setDecisionIntent({ approval, value, objective });
              }}
            />
          ))}
        </section>
      )}

      {approvalsQuery.hasNextPage && (
        <button
          className="aw-button is-ghost"
          disabled={approvalsQuery.isFetchingNextPage}
          onClick={() => void approvalsQuery.fetchNextPage()}
          type="button"
        >
          {approvalsQuery.isFetchingNextPage ? "正在加载" : "加载更早记录"}
        </button>
      )}

      {decisionIntent !== null && (
        <DecisionDialog
          intent={decisionIntent}
          error={decision.error}
          pending={decision.isPending}
          onClose={() => {
            if (!decision.isPending) setDecisionIntent(null);
          }}
          onConfirm={() => decision.mutate(decisionIntent)}
        />
      )}
    </main>
  );
}

function ApprovalCard({
  approval,
  identity,
  onDecide,
}: {
  approval: ApprovalView;
  identity: PrincipalIdentity;
  onDecide: (value: ApprovalDecision, objective: string | null) => void;
}) {
  const objective = useQuery({
    queryKey: [
      "approvals",
      "task-objective",
      identity.tenantId,
      identity.principalId,
      [...identity.scopes].sort().join(","),
      approval.task_id,
    ],
    queryFn: () => loadTaskObjective(identity, approval.task_id),
    staleTime: Number.POSITIVE_INFINITY,
  });

  return (
    <article className="aw-list-row">
      <div className="aw-list-row-main">
        <div className="aw-section-header">
          <div>
            <strong>允许生成并导出任务报告</strong>
            <span>请求于 {formatDateTime(approval.created_at)}</span>
          </div>
          <StatusPill status={approval.status} />
        </div>

        <div className="aw-notice">
          <FileCheck2 aria-hidden="true" size={16} />
          <span>
            {objective.isPending && "正在读取这个任务的目标…"}
            {objective.isSuccess && objective.data}
            {objective.isError &&
              "暂时无法读取任务目标。请先打开关联任务核对内容，再决定是否导出。"}
          </span>
        </div>

        <Link className="aw-inline-link" to={`/work/${approval.task_id}`}>
          打开关联任务
          <ArrowRight aria-hidden="true" size={14} />
        </Link>

        {approval.decided_at !== null && (
          <span className="aw-page-note">
            处理时间：{formatDateTime(approval.decided_at)}
          </span>
        )}

        <details>
          <summary>
            <ChevronDown aria-hidden="true" size={14} />
            工程信息
          </summary>
          <dl className="aw-definition-list">
            <div>
              <dt>审批 ID</dt>
              <dd><code>{approval.approval_id}</code></dd>
            </div>
            <div>
              <dt>任务 ID</dt>
              <dd><code>{approval.task_id}</code></dd>
            </div>
            <div>
              <dt>决定版本</dt>
              <dd>{approval.decision_version}</dd>
            </div>
          </dl>
        </details>
      </div>

      {approval.status === "pending" && (
        <div className="aw-page-actions">
          <button
            className="aw-button is-ghost"
            disabled={objective.isPending}
            onClick={() => onDecide("rejected", objective.data ?? null)}
            type="button"
          >
            <X aria-hidden="true" size={15} />
            拒绝导出
          </button>
          <button
            className="aw-button is-primary"
            disabled={objective.isPending}
            onClick={() => onDecide("approved", objective.data ?? null)}
            type="button"
          >
            <Check aria-hidden="true" size={15} />
            允许导出
          </button>
        </div>
      )}
    </article>
  );
}

function DecisionDialog({
  intent,
  error,
  pending,
  onClose,
  onConfirm,
}: {
  intent: DecisionIntent;
  error: Error | null;
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const approves = intent.value === "approved";
  return (
    <div className="aw-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        aria-labelledby="approval-decision-title"
        aria-modal="true"
        className="aw-dialog"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header>
          <div>
            <h2 id="approval-decision-title">
              {approves ? "确认允许导出？" : "确认拒绝导出？"}
            </h2>
            <p>{intent.objective ?? "任务目标当前无法读取，请确认你已经在关联任务中核对过内容。"}</p>
          </div>
          <IconButton disabled={pending} label="关闭" onClick={onClose}>
            <X aria-hidden="true" size={17} />
          </IconButton>
        </header>
        <div className={`aw-notice ${approves ? "" : "is-warning"}`}>
          <span>
            {approves
              ? "确认后，任务会继续生成并导出最终报告。"
              : "确认后，任务会结束，并且不会导出报告。"}
          </span>
        </div>
        {error !== null && <ErrorNotice message={errorMessage(error)} />}
        <footer>
          <button
            className="aw-button is-ghost"
            disabled={pending}
            onClick={onClose}
            type="button"
          >
            返回检查
          </button>
          <button
            className={approves ? "aw-button is-primary" : "aw-button is-ghost"}
            disabled={pending}
            onClick={onConfirm}
            type="button"
          >
            {pending
              ? "正在提交…"
              : approves
                ? "确认允许"
                : "确认拒绝"}
          </button>
        </footer>
      </section>
    </div>
  );
}

async function loadTaskObjective(
  identity: PrincipalIdentity,
  taskId: string,
): Promise<string> {
  const timeline = await getTaskTimeline(identity, taskId);
  const inputRef = findTaskInputRef(timeline.events);
  if (inputRef === null) throw new Error("任务时间线没有输入引用");
  const artifact = await getArtifactJson<unknown>(identity, inputRef);
  const parsed = parseTaskInputArtifact(artifact);
  if (parsed === null) throw new Error("任务输入格式无法确认");
  return parsed.objective;
}

function approvalStatuses(filter: ApprovalFilter): ApprovalStatus[] {
  return filter === "pending" ? ["pending"] : ["approved", "rejected"];
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
