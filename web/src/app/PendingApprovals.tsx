import { useQuery } from "@tanstack/react-query";
import { Inbox, X } from "lucide-react";
import { useEffect, useRef, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";

import { listApprovals } from "../api/client";
import type { ApprovalView, PrincipalIdentity } from "../api/types";
import { formatDateTime, shortId } from "../components/ui";

/**
 * 待处理：等你批准的任务，从任何一页都能看到、一步回到它们。
 *
 * 2026-09-04 评审第 4 条与 C 项：Task 与 Code 页内的审批都在，但一个人在
 * 别的页面上时不知道有任务停着等他，回来也要先在列表里找。ADR-048 删掉的是
 * 「待我确认」那一整页——那一页**在**列表里做决定，而草稿不在眼前；这里只是
 * 一份链接，决定仍然在任务自己的页面上、在要决定的内容下面。
 *
 * **只列 Task 的审批。** Code 的命令审批是另一种会话级对象，挂在每个会话的
 * `/approvals` 上，没有一个跨会话的列表；要列它得逐个会话去问，而问出来的
 * 「还在等」也只对正在跑的那一段成立。所以对话框底下写明范围，不假装覆盖。
 *
 * 每 15 秒问一次，和运行状态页的健康检查同一节奏；查不到不当错误——一台
 * 没起 API 的机器上这一行照样在，只是没有数字。
 */
export function usePendingApprovals(identity: PrincipalIdentity) {
  return useQuery({
    queryKey: ["approvals", "pending", identity.tenantId, identity.principalId],
    queryFn: () => listApprovals(identity, { statuses: ["pending"], limit: 50 }),
    refetchInterval: 15_000,
    retry: false,
  });
}

/** 侧栏里的那一行：名字、以及等着的数目。 */
export function PendingApprovalsLink({
  count,
  onOpen,
  open,
}: {
  count: number | null;
  onOpen: () => void;
  open: boolean;
}) {
  return (
    <button
      aria-expanded={open}
      aria-haspopup="dialog"
      aria-label={count === null || count === 0 ? "待处理" : `待处理，${String(count)} 项`}
      className={`aw-global-link ${count !== null && count > 0 ? "has-pending" : ""}`}
      onClick={onOpen}
      title="等你批准的任务"
      type="button"
    >
      <span className="aw-global-link-icon">
        <Inbox aria-hidden="true" size={18} />
      </span>
      <span className="aw-global-link-copy">
        待处理
        {count !== null && count > 0 ? (
          <span aria-hidden="true" className="aw-rail-badge">
            {count}
          </span>
        ) : null}
      </span>
    </button>
  );
}

export function PendingApprovalsDialog({
  approvals,
  error,
  loading,
  onClose,
}: {
  approvals: readonly ApprovalView[];
  error: boolean;
  loading: boolean;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    // 落在第一条链接上；没有链接就落在关闭键上。读者按 Enter 就走。
    const first = dialogRef.current?.querySelector<HTMLElement>(
      ".aw-pending-item, .aw-command-close",
    );
    first?.focus();
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab" || dialogRef.current === null) return;
    const focusable = Array.from(
      dialogRef.current.querySelectorAll<HTMLElement>(
        "a[href], button:not([disabled])",
      ),
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="aw-command-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section
        aria-labelledby="aw-pending-title"
        aria-modal="true"
        className="aw-command-dialog aw-pending-dialog"
        onKeyDown={handleKeyDown}
        ref={dialogRef}
        role="dialog"
      >
        <header className="aw-pending-head">
          <h2 id="aw-pending-title">待处理</h2>
          <button
            aria-label="关闭待处理"
            className="aw-command-close"
            onClick={onClose}
            type="button"
          >
            <X aria-hidden="true" size={17} />
          </button>
        </header>
        {loading ? <p className="aw-pending-note">正在读取…</p> : null}
        {error ? (
          <p className="aw-pending-note">读不到审批列表；API 没在跑的时候就是这样。</p>
        ) : null}
        {!loading && !error && approvals.length === 0 ? (
          <p className="aw-pending-note">没有等你批准的任务。</p>
        ) : null}
        {approvals.length > 0 ? (
          <ul className="aw-pending-list">
            {approvals.map((approval) => (
              <li key={approval.approval_id}>
                <Link
                  className="aw-pending-item"
                  onClick={onClose}
                  to={`/work/${encodeURIComponent(approval.task_id)}`}
                >
                  <strong>任务 {shortId(approval.task_id, 20)}</strong>
                  <span>等待批准 · {formatDateTime(approval.created_at)} 起</span>
                </Link>
              </li>
            ))}
          </ul>
        ) : null}
        <footer className="aw-pending-scope">
          只列任务的审批。编码会话里的命令审批在它自己的会话页上，输入框上方。
        </footer>
      </section>
    </div>
  );
}
