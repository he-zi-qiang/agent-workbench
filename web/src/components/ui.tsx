import { AlertTriangle, LoaderCircle } from "lucide-react";
import type { PropsWithChildren, ReactNode } from "react";
import type { ApprovalStatus, TaskStatus } from "../api/types";

export function IconButton({
  label,
  children,
  onClick,
  active = false,
  disabled = false,
  className = "",
}: PropsWithChildren<{
  label: string;
  onClick?: () => void;
  active?: boolean;
  disabled?: boolean;
  className?: string;
}>) {
  return (
    <button
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
      dead_letter: "死信",
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
