import { AlertTriangle, ChevronDown, LoaderCircle, Plus } from "lucide-react";
import type { PropsWithChildren, ReactNode } from "react";
import type { ApprovalStatus, TaskStatus } from "../api/types";
import { useStoredState } from "../hooks/useStoredState";

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

export function StatusPill({
  status,
}: {
  status: TaskStatus | ApprovalStatus;
}) {
  const className =
    status === "succeeded" || status === "approved"
      ? "is-success"
      : status === "waiting_approval" || status === "pending"
        ? "is-warning"
        : ["failed", "cancelled", "dead_letter", "rejected"].includes(status)
          ? "is-danger"
          : "is-running";
  return (
    <span className={`aw-status-pill ${className}`}>
      {formatStatus(status)}
    </span>
  );
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

export function KeyValue({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
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
 * 侧栏里的一个可折叠分区：一行标题 + 右侧动作，底下是内容。
 *
 * 三栏共用一个组件而不是各写一遍：对话、任务、编码的会话列是同一个形状，分开
 * 写的三份第二次改样式时会分叉，而它们分叉了没有人看得出来——三栏不会同时出现
 * 在一块屏幕上。
 *
 * 折叠状态按 `storageKey` 各自记住。整栏收起而不是只收起列表：收起之后这一栏
 * 只剩标题那一行，另外两栏才拿得到高度——一个同时展开三份长列表的侧栏，等于
 * 三份都读不了。
 *
 * `hidden` 用条件渲染而不是 CSS：这一栏是 flex 列，`display:none` 与
 * `flex:1 1 auto` 打架，收起之后仍然会占住 min-height 撑开的那一段。
 */
export function SidebarSection({
  title,
  storageKey,
  actions,
  children,
}: PropsWithChildren<{
  title: string;
  storageKey: string;
  actions?: ReactNode;
}>) {
  const [open, setOpen] = useStoredState(storageKey, true);
  const bodyId = `${storageKey.replace(/[^a-zA-Z0-9-]/g, "-")}-body`;
  return (
    <section className={`aw-side-section ${open ? "is-open" : "is-collapsed"}`}>
      <div className="aw-side-section-head">
        <button
          aria-controls={bodyId}
          aria-expanded={open}
          className="aw-side-section-toggle"
          onClick={() => setOpen(!open)}
          type="button"
        >
          <ChevronDown aria-hidden="true" size={13} />
          <span>{title}</span>
        </button>
        {actions === undefined ? null : (
          <div className="aw-side-section-actions">{actions}</div>
        )}
      </div>
      {open ? (
        <div className="aw-side-section-body" id={bodyId}>
          {children}
        </div>
      ) : null}
    </section>
  );
}

/**
 * 分区标题右边的一个图标动作（新建、搜索、刷新）。
 *
 * 这里此前是一个整行的描边按钮，当时的理由是「一个图标按钮在视觉上是标题的
 * 附件，而且它没有名字，只有把指针停上去才知道它是什么」。前半句现在正是要的
 * ——三栏并排时，三个整行按钮会和三份列表抢同一屏的高度，而「开一段新的」不该
 * 比「回到哪一段」更响。
 *
 * 后半句是真问题，所以没有一起丢掉：`label` 同时进 `aria-label` 和 `title`，
 * 读屏软件念得出来，指针停上去看得见。一个没有名字的图标按钮仍然是错的，
 * 这里只是不再用整行的宽度去换那个名字。
 */
export function SidebarAction({
  label,
  onClick,
  active,
  children,
}: PropsWithChildren<{
  label: string;
  onClick: () => void;
  active?: boolean;
}>) {
  return (
    <button
      aria-label={label}
      aria-pressed={active}
      className="aw-side-action"
      onClick={onClick}
      title={label}
      type="button"
    >
      {children}
    </button>
  );
}

/** 一个只画加号的新建动作，省得三处各写一遍同样的 `<Plus />`。 */
export function NewSessionAction({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <SidebarAction label={label} onClick={onClick}>
      <Plus aria-hidden="true" size={16} />
    </SidebarAction>
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
