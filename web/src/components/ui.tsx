import { AlertTriangle, LoaderCircle, Plus } from "lucide-react";
import type { PropsWithChildren, ReactNode } from "react";
import type { ApprovalStatus, TaskStatus } from "../api/types";

export function IconButton({
  label,
  children,
  onClick,
  active,
  controls,
  disabled = false,
  expanded,
  className = "",
}: PropsWithChildren<{
  label: string;
  onClick?: () => void;
  active?: boolean;
  controls?: string;
  disabled?: boolean;
  expanded?: boolean;
  className?: string;
}>) {
  return (
    <button
      aria-controls={controls}
      aria-expanded={expanded}
      aria-label={label}
      aria-pressed={active}
      className={`aw-icon-button ${className}`}
      disabled={disabled}
      onClick={onClick}
      type="button"
    >
      {children}
    </button>
  );
}

export function StatusPill({ status }: { status: TaskStatus | ApprovalStatus }) {
  const className =
    status === "succeeded" || status === "approved"
      ? "is-success"
      : status === "waiting_approval" || status === "pending"
        ? "is-warning"
        : ["failed", "cancelled", "dead_letter", "rejected"].includes(status)
          ? "is-danger"
          : "is-running";
  return <span className={`aw-status-pill ${className}`}>{formatStatus(status)}</span>;
}

export function formatStatus(status: string): string {
  return (
    {
      queued: "排队中",
      running: "运行中",
      waiting_approval: "等待批准",
      waiting_migration: "等待迁移",
      succeeded: "已完成",
      failed: "失败",
      cancelled: "已取消",
      // 不是「死信」。这个词是队列的，而它出现在两个地方：任务详情的状态
      // 药丸，以及 `任务{formatStatus(status)}` 拼出来的那句话——后者会
      // 拼成「任务死信」，不是一句中文。读者要知道的只有一件事：这一条
      // 结束了，没有人会再替他重试。
      dead_letter: "已放弃重试",
      pending: "待处理",
      approved: "已批准",
      rejected: "已拒绝",
    }[status] ?? status
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="aw-empty-state">
      <div className="aw-empty-icon">{icon}</div>
      <h2>{title}</h2>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function LoadingLine({ label = "正在加载" }: { label?: string }) {
  return (
    <span className="aw-loading-line" role="status">
      <LoaderCircle aria-hidden="true" className="aw-spin" size={15} />
      <span className="aw-loading-label">{label}</span>
    </span>
  );
}

export function ErrorNotice({ message }: { message: string }) {
  return (
    <div className="aw-notice is-danger" role="alert">
      <AlertTriangle aria-hidden="true" size={16} />
      <span>{message}</span>
    </div>
  );
}

export function InfoNotice({ children }: PropsWithChildren) {
  return <div className="aw-notice">{children}</div>;
}

export function KeyValue({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="aw-key-value">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

/**
 * A file size, for a line that also has a name on it.
 *
 * One decimal above a kilobyte and none below: "1.0 KB" beside "写入" reads as
 * a size, "1024 B" reads as a number the reader has to convert.
 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

/**
 * 「开一段新的」这个动作，在会话列这一栏里。
 *
 * 此前是标题右边一个 20px 的 `+` 图标。稿子把它画成整行描边按钮，理由在这一栏
 * 的用途里：一列会话回答的是「回到哪一段」，而开新的一段是这里唯一的另一件事
 * ——它和列表并列，不从属于列表的标题。一个图标按钮在视觉上是标题的附件，
 * 而且它没有名字，只有把指针停上去才知道它是什么。
 *
 * 两栏共用一个组件而不是各写一遍：Chat 与 Code 的会话列在稿子上是同一个形状，
 * 分开写的两份第二次改样式时会分叉，而它们分叉了没有人看得出来——两栏不会同时
 * 出现在一块屏幕上。
 */
export function NewSessionButton({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <button className="aw-new-session" onClick={onClick} type="button">
      <Plus aria-hidden="true" size={15} />
      <span>{label}</span>
    </button>
  );
}

export function shortId(value: string, length = 12): string {
  if (value.length <= length) return value;
  return `${value.slice(0, Math.max(4, length - 5))}…${value.slice(-4)}`;
}

export function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
