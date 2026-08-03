import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ArrowRight,
  Check,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  decideApproval,
  listApprovals,
} from "../../api/client";
import type {
  ApprovalStatus,
  ApprovalView,
} from "../../api/types";
import { useIdentity } from "../../app/IdentityContext";
import {
  EmptyState,
  ErrorNotice,
  LoadingLine,
  StatusPill,
  formatDateTime,
  shortId,
} from "../../components/ui";

type ApprovalFilter = ApprovalStatus | "all";

const FILTERS: ReadonlyArray<{ value: ApprovalFilter; label: string }> = [
  { value: "pending", label: "待处理" },
  { value: "all", label: "全部" },
  { value: "approved", label: "已批准" },
  { value: "rejected", label: "已拒绝" },
];

export function ApprovalsPage() {
  const { identity } = useIdentity();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<ApprovalFilter>("pending");

  const approvalsQuery = useInfiniteQuery({
    queryKey: ["approvals", identity.tenantId, identity.principalId, filter],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      listApprovals(identity, {
        statuses: filter === "all" ? [] : [filter],
        ...(pageParam === undefined ? {} : { cursor: pageParam }),
        limit: 25,
      }),
    getNextPageParam: (lastPage) => lastPage.cursor ?? undefined,
  });

  const decision = useMutation({
    mutationFn: ({
      approval,
      value,
    }: {
      approval: ApprovalView;
      value: "approved" | "rejected";
    }) => decideApproval(identity, approval, value),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["approvals"] });
    },
  });

  const approvals =
    approvalsQuery.data?.pages.flatMap((page) => page.approvals) ?? [];

  return (
    <main className="aw-utility-page">
      <header className="aw-page-header">
        <div>
          <span className="aw-eyebrow">Human in the loop</span>
          <h1>审批队列</h1>
          <p>这里显示服务端审批账本返回的权威状态，不从 Task 时间线猜测结果。</p>
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
          当前接口只公开审批 ID、Task、状态和版本；它没有返回理由、提示词或 Policy
          revision，本页不会补写这些字段。
        </span>
      </div>

      <div className="aw-toolbar">
        <div className="aw-segmented" aria-label="审批状态筛选">
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

      {approvalsQuery.isPending && <LoadingLine label="正在读取审批账本" />}
      {approvalsQuery.error !== null && (
        <ErrorNotice message={errorMessage(approvalsQuery.error)} />
      )}
      {decision.error !== null && <ErrorNotice message={errorMessage(decision.error)} />}

      {!approvalsQuery.isPending && approvalsQuery.error === null && approvals.length === 0 && (
        <EmptyState
          description={
            filter === "pending"
              ? "服务端没有返回属于当前身份的待处理审批。"
              : "服务端没有返回符合当前筛选条件的审批。"
          }
          icon={<ShieldCheck aria-hidden="true" size={20} />}
          title="审批队列为空"
        />
      )}

      {approvals.length > 0 && (
        <section className="aw-list" aria-label="审批列表">
          {approvals.map((approval) => (
            <article className="aw-list-row" key={approval.approval_id}>
              <div className="aw-list-row-main">
                <div className="aw-section-header">
                  <div>
                    <strong>审批 {shortId(approval.approval_id, 18)}</strong>
                    <span>
                      创建于 {formatDateTime(approval.created_at)} · 决定版本 {approval.decision_version}
                    </span>
                  </div>
                  <StatusPill status={approval.status} />
                </div>
                <Link className="aw-inline-link" to={`/work/${approval.task_id}`}>
                  打开关联 Work
                  <code>{shortId(approval.task_id, 18)}</code>
                  <ArrowRight aria-hidden="true" size={14} />
                </Link>
                {approval.decided_at !== null && (
                  <span className="aw-page-note">
                    服务端决定时间：{formatDateTime(approval.decided_at)}
                  </span>
                )}
              </div>
              {approval.status === "pending" && (
                <div className="aw-page-actions">
                  <button
                    className="aw-button is-ghost"
                    disabled={decision.isPending}
                    onClick={() => decision.mutate({ approval, value: "rejected" })}
                    type="button"
                  >
                    <X aria-hidden="true" size={15} />
                    拒绝
                  </button>
                  <button
                    className="aw-button is-primary"
                    disabled={decision.isPending}
                    onClick={() => decision.mutate({ approval, value: "approved" })}
                    type="button"
                  >
                    <Check aria-hidden="true" size={15} />
                    批准
                  </button>
                </div>
              )}
            </article>
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
    </main>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后重试。";
}
