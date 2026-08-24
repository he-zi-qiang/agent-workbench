import {
  FileText,
  LoaderCircle,
  Sparkles,
  TerminalSquare,
  Workflow,
} from "lucide-react";
import type { ReactNode } from "react";

export type LiveActivityKind = "thinking" | "tool" | "answer" | "workflow";

const ICONS = {
  thinking: Sparkles,
  tool: TerminalSquare,
  answer: FileText,
  workflow: Workflow,
} as const;

/**
 * The one visual sentence every surface uses while work is moving.
 *
 * It contains no execution logic. Callers name the current safe projection --
 * a stage, a command, or "writing the answer" -- and keep their different
 * transports and state machines. That makes the visual promise consistent
 * without pretending Task polling is the same thing as Code's live channel.
 */
export function LiveActivity({
  detail,
  kind,
  meta,
  title,
}: {
  detail?: ReactNode;
  kind: LiveActivityKind;
  meta?: ReactNode;
  title: string;
}) {
  const Icon = ICONS[kind];
  return (
    <div aria-atomic="true" className={`aw-live-activity is-${kind}`} role="status">
      <span className="aw-live-activity-mark" aria-hidden="true">
        <Icon size={15} />
        <LoaderCircle className="aw-live-activity-spinner" size={12} />
      </span>
      <span className="aw-live-activity-copy">
        <strong className="aw-live-sweep">{title}</strong>
        {detail === undefined ? null : <small>{detail}</small>}
      </span>
      {meta === undefined ? null : <span className="aw-live-activity-meta">{meta}</span>}
    </div>
  );
}
