import {
  Check,
  ChevronRight,
  CircleDashed,
  LoaderCircle,
  Slash,
  X,
} from "lucide-react";

import { shortId } from "../../components/ui";
import { flattenRuns, totalSpend, type RunNode, type RunStatus } from "./runTree";

/**
 * Who is working on this Task, and how far each of them has got.
 *
 * The step stream beside this one answers "what happened, in order". Since a
 * run can delegate (ADR-082), that stream interleaves two agents' model calls
 * and tool calls, and no amount of ordering makes "how is the sub-agent doing"
 * readable from it -- the reader would have to hold the interleaving in their
 * head. This panel answers the other question: **one row per run, whatever the
 * order things happened in.**
 *
 * Three decisions worth stating, because each had an easier alternative.
 *
 * **A tree, not a list.** The rows are nested under the run that started them.
 * A flat list with a "parent" column would be smaller code and would make the
 * one thing this exists to show -- that this agent was started *by* that one --
 * something the reader reconstructs by matching ids.
 *
 * **Each run reports its own spend, and the total is separate.** Folding a
 * child's tokens into its parent's row would read as a statement about the
 * parent's budget, which never saw them. The total is shown once, at the top,
 * where it is a total and cannot be mistaken for anybody's ceiling.
 *
 * **No progress bar.** A run does not know how many steps it will take, so any
 * bar would be a fraction with an invented denominator. What is honest is what
 * it is doing *now* and what it has spent, and that is what each row carries.
 */

const STATUS_LABEL: Readonly<Record<RunStatus, string>> = {
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "等待中",
};

/**
 * What each row shows while it is doing something.
 *
 * Only the verbs a reader would recognise. An event type with no entry falls
 * back to nothing rather than to its own name: `ToolProposed` on a row would
 * tell somebody watching a Task exactly as much as a raw id does.
 */
const ACTIVITY_LABEL: Readonly<Record<string, string>> = {
  RunStarted: "刚开始",
  ModelStarted: "正在作答",
  ModelCompleted: "作答完成",
  ToolStarted: "正在调用工具",
  ToolCompleted: "工具已返回",
  ToolFailed: "工具失败",
  ToolProgress: "工具进行中",
  AgentDelegated: "正在等子代理",
  AgentCompleted: "子代理已回来",
  ContextCompacted: "已压缩上下文",
};

function StatusIcon({ status }: { status: RunStatus }) {
  const size = 14;
  if (status === "running")
    return (
      <LoaderCircle
        aria-hidden="true"
        className="aw-spin"
        size={size}
        strokeWidth={2.2}
      />
    );
  if (status === "completed")
    return <Check aria-hidden="true" size={size} strokeWidth={2.6} />;
  if (status === "failed")
    return <X aria-hidden="true" size={size} strokeWidth={2.6} />;
  if (status === "cancelled")
    return <Slash aria-hidden="true" size={size} strokeWidth={2.2} />;
  return <CircleDashed aria-hidden="true" size={size} strokeWidth={2} />;
}

function formatTokens(value: number): string {
  if (value < 1000) return String(value);
  return `${(value / 1000).toFixed(1)}k`;
}

function RunRow({
  node,
  depth,
  selectedRunId,
  onSelect,
}: {
  node: RunNode;
  depth: number;
  selectedRunId: string | null;
  onSelect: (runId: string | null) => void;
}) {
  const selected = selectedRunId === node.runId;
  // A delegated run is named by the sub-agent it was started as; a graph node's
  // run is named by its node. Neither is guessed from the id -- a run with
  // neither says so rather than being labelled with a truncated identifier
  // dressed up as a name.
  const name =
    node.definitionName ?? node.nodeId ?? `运行 ${shortId(node.runId, 8)}`;
  const activity =
    node.status === "running" && node.latestEventType !== null
      ? ACTIVITY_LABEL[node.latestEventType]
      : undefined;

  return (
    <>
      <li
        className={`aw-run-row${selected ? " is-selected" : ""} is-${node.status}`}
        style={{ "--aw-run-depth": depth } as React.CSSProperties}
      >
        <button
          aria-current={selected ? "true" : undefined}
          className="aw-run-row-button"
          onClick={() => {
            onSelect(selected ? null : node.runId);
          }}
          type="button"
        >
          <span className="aw-run-status" title={STATUS_LABEL[node.status]}>
            <StatusIcon status={node.status} />
          </span>
          <span className="aw-run-name">
            {node.definitionName !== null && (
              <span className="aw-run-badge">子代理</span>
            )}
            {name}
          </span>
          <span className="aw-run-meta">
            {activity !== undefined && (
              <span className="aw-run-activity">{activity}</span>
            )}
            {node.spend.steps > 0 && <span>{node.spend.steps} 步</span>}
            {node.spend.toolCalls > 0 && (
              <span>{node.spend.toolCalls} 次工具</span>
            )}
            {node.spend.inputTokens + node.spend.outputTokens > 0 && (
              <span>
                {formatTokens(node.spend.inputTokens)}↓{" "}
                {formatTokens(node.spend.outputTokens)}↑
              </span>
            )}
            {node.eventCount > 0 && (
              <span className="aw-run-events">{node.eventCount} 条事件</span>
            )}
          </span>
          <ChevronRight
            aria-hidden="true"
            className="aw-run-chevron"
            size={14}
          />
        </button>
      </li>
      {node.children.map((child) => (
        <RunRow
          depth={depth + 1}
          key={child.runId}
          node={child}
          onSelect={onSelect}
          selectedRunId={selectedRunId}
        />
      ))}
    </>
  );
}

export function RunPanel({
  roots,
  selectedRunId,
  onSelect,
}: {
  roots: RunNode[];
  /** Which run the step stream is currently narrowed to, if any. */
  selectedRunId: string | null;
  onSelect: (runId: string | null) => void;
}) {
  // Rendered only where there is something a flat stream cannot show. On a Task
  // that never delegated, every run is a graph node and the stage list above
  // already says all of this -- a second panel repeating it would be furniture.
  const all = flattenRuns(roots);
  const delegated = all.filter((node) => node.parentRunId !== null);
  if (delegated.length === 0) return null;

  const total = totalSpend(roots);
  const running = all.filter((node) => node.status === "running").length;

  return (
    <section aria-label="参与这次任务的 Agent" className="aw-run-panel">
      <header className="aw-run-panel-head">
        <h3>
          参与的 Agent
          <span className="aw-run-count">
            {all.length} 个运行 · 其中 {delegated.length} 个是子代理
            {running > 0 ? ` · ${running} 个进行中` : ""}
          </span>
        </h3>
        <span className="aw-run-total">
          合计 {formatTokens(total.inputTokens)}↓{" "}
          {formatTokens(total.outputTokens)}↑
        </span>
      </header>
      <ul className="aw-run-list">
        {roots.map((node) => (
          <RunRow
            depth={0}
            key={node.runId}
            node={node}
            onSelect={onSelect}
            selectedRunId={selectedRunId}
          />
        ))}
      </ul>
      {selectedRunId !== null && (
        <p className="aw-run-hint">
          下面的执行过程只显示这一个运行。
          <button
            className="aw-run-clear"
            onClick={() => {
              onSelect(null);
            }}
            type="button"
          >
            显示全部
          </button>
        </p>
      )}
    </section>
  );
}
