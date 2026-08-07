import { ChevronRight } from "lucide-react";
import type { EventEnvelope } from "../api/types";
import { StepDisclosure } from "./StepDisclosure";

/**
 * A run, as the stages it went through, each openable to its real steps.
 *
 * Shared between Work and Chat because the reader's question is the same in
 * both -- what did it do, and what actually went in and out of each step --
 * and answering it two different ways taught the same thing twice.
 *
 * What is *not* shared is where the stages come from. Work has a graph and
 * groups by node; a Chat turn has no `graph_node_id` at all and groups by what
 * its events mean. Those are genuinely different readings of different data,
 * so each caller derives its own stages and this renders them.
 */

export type StreamStageState =
  | "done"
  | "active"
  | "failed"
  | "waiting"
  | "pending"
  | "skipped";

export interface StreamStage {
  id: string;
  title: string;
  state: StreamStageState;
  /** Right-aligned text: a time once finished, a status while not. */
  note: string;
  events: EventEnvelope[];
}

export interface StreamMeta {
  title: string;
  events: EventEnvelope[];
}

export function StepStream({
  ariaLabel,
  eventTitle,
  isKnownEvent,
  meta,
  onOpenArtifact,
  running,
  stages,
}: {
  ariaLabel: string;
  eventTitle: (event: EventEnvelope) => string;
  /** Unknown event types stay visible, dimmed, rather than being dropped. */
  isKnownEvent?: (event: EventEnvelope) => boolean;
  /** Run- or task-level bookkeeping, folded away under the stages. */
  meta?: StreamMeta;
  onOpenArtifact?: (artifactId: string) => void;
  running: boolean;
  stages: StreamStage[];
}) {
  const step = (event: EventEnvelope) => (
    <li
      className={
        isKnownEvent === undefined || isKnownEvent(event) ? "" : "is-unknown"
      }
      key={event.event_id}
    >
      <StepDisclosure
        event={event}
        title={eventTitle(event)}
        {...(onOpenArtifact === undefined ? {} : { onOpenArtifact })}
      />
    </li>
  );

  return (
    <section className="aw-stream" aria-label={ariaLabel}>
      <ol className="aw-stream-steps">
        {stages.map((stage) => (
          <li className={`is-${stage.state}`} key={stage.id}>
            <span className="aw-stream-dot" aria-hidden="true" />
            {stage.events.length === 0 ? (
              <div className="aw-stream-head is-empty">
                <span className="aw-stream-title">{stage.title}</span>
                <span className="aw-stream-note">{stage.note}</span>
              </div>
            ) : (
              // Only the stage that is moving is open. A finished run collapses
              // to one line per stage, and opening one shows the prompts, tool
              // calls and outputs that produced it.
              <details
                className="aw-stream-step"
                open={running && stage.state === "active"}
              >
                <summary className="aw-stream-head">
                  <ChevronRight
                    aria-hidden="true"
                    className="aw-step-caret"
                    size={13}
                  />
                  <span className="aw-stream-title">{stage.title}</span>
                  <span className="aw-stream-count">
                    {stage.events.length} 步
                  </span>
                  <span className="aw-stream-note">{stage.note}</span>
                </summary>
                <ol className="aw-stream-events">{stage.events.map(step)}</ol>
              </details>
            )}
          </li>
        ))}
      </ol>
      {meta === undefined || meta.events.length === 0 ? null : (
        <details className="aw-stream-meta">
          <summary>
            <ChevronRight aria-hidden="true" className="aw-step-caret" size={13} />
            {meta.title}
            <span>{meta.events.length} 条</span>
          </summary>
          <ol className="aw-stream-events">{meta.events.map(step)}</ol>
        </details>
      )}
    </section>
  );
}
